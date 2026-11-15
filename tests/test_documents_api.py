"""Test integrasi fitur 6 — ringkasan dokumen panjang, model disimulasikan.

Yang diuji adalah alurnya, bukan mutu ringkasan model. Inti perbaikan fitur ini
satu hal: dokumen panjang benar-benar dipotong, setiap potongan diringkas, dan
seluruh ringkasan potongan itu sampai ke tahap penggabungan. Sebelumnya dokumen
39 halaman hanya terkirim bagian awalnya, sehingga laporannya hanya menyebut
halaman judul, penulis, dan afiliasinya.

Potongan diperkecil lewat konfigurasi supaya test tidak perlu teks sepanjang
21.000 karakter untuk setiap bagian.
"""

from __future__ import annotations

import io
import re
from types import SimpleNamespace

import docx
import httpx
import pytest
from app.core.config import settings
from app.core.llm import Completion, LLMResponse, llm
from app.core.tasks import Task
from app.observability.ledger import ledger

from tests.conftest import HEADERS, tunggu_job


def _dokumen(baris: int = 15) -> bytes:
    """Dokumen tiruan; sekitar 700 karakter per bagian pada pengaturan test."""
    isi = "\n\n".join(
        f"Bagian ke-{index}. "
        + " ".join(f"kata{index}x{nomor}" for nomor in range(20))
        for index in range(baris)
    )
    return isi.encode("utf-8")


def _docx(baris: int = 3) -> bytes:
    """Dokumen DOCX tiruan, untuk memastikan jenis berkas ini ikut terbaca."""
    dokumen = docx.Document()

    for nomor in range(baris):
        dokumen.add_paragraph(
            f"Paragraf {nomor} membahas metode deteksi dan hasil pengujiannya."
        )

    buffer = io.BytesIO()
    dokumen.save(buffer)
    return buffer.getvalue()


def _fake_completion(task: str = "doc_synthesis") -> Completion:
    return Completion(
        response=LLMResponse(
            content="{}",
            model=settings.model_paid,
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
        ),
        task=task,
        route="paid",
        requested_model=settings.model_paid,
        degraded=False,
    )


# Tahap penggabungan hanya menghasilkan bagian di luar badan laporan. Badan
# laporan disusun dari catatan per bagian dokumen, sehingga keluaran di sini
# tidak memuat daftar bagian sama sekali.
_IKHTISAR = {
    "judul": "Telaah Dokumen Keamanan Siber",
    "ringkasan_eksekutif": "Dokumen ini meninjau banyak metode deteksi serangan.",
    "tabel_perbandingan": {
        "judul": "Perbandingan",
        "kolom": ["Metode", "Kelebihan"],
        "baris": [["Jaringan saraf", "Akurat"]],
    },
    "temuan_utama": ["Deteksi berbasis pembelajaran mesin makin lazim."],
    "keterbatasan": ["Data latih terbatas."],
    "istilah": [{"nama": "IDS", "arti": "sistem deteksi intrusi"}],
    "diagram": {
        "jenis": "alur",
        "judul": "Alur",
        "nodes": [{"id": "a", "label": "Data"}, {"id": "b", "label": "Model"}],
        "edges": [{"dari": "a", "ke": "b"}],
    },
}


@pytest.fixture
def model_palsu(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Model tiruan dua tahap: meringkas potongan, lalu menggabungkannya."""
    catatan: dict = {"potongan": [], "gabungan": None, "gagal_pada": None}

    async def fake_structured(*, task, messages, owner, schema, **_):
        isi = " ".join(str(message.get("content", "")) for message in messages)

        if task == Task.DOC_CHUNK_SUMMARY:
            nomor = int(re.search(r"Ini potongan (\d+) dari", isi).group(1))

            if catatan["gagal_pada"] == nomor:
                raise RuntimeError("model sedang tidak tersedia")

            catatan["potongan"].append(nomor)
            payload = {
                "judul_bagian": f"Bagian {nomor}",
                "ringkasan": [f"penanda-{nomor}"],
                "poin": [f"butir-{nomor}"],
                "metode": [f"metode-{nomor}"],
                "istilah": [{"nama": f"istilah-{nomor}", "arti": "arti"}],
            }
            return schema.model_validate(payload), _fake_completion("doc_chunk_summary")

        catatan["gabungan"] = isi
        return schema.model_validate(_IKHTISAR), _fake_completion()

    monkeypatch.setattr(llm, "structured", fake_structured)
    monkeypatch.setattr(settings, "doc_chunk_tokens", 200)
    monkeypatch.setattr(settings, "doc_chunk_overlap_tokens", 20)
    return catatan


async def _minta(client: httpx.AsyncClient, berkas: tuple, data: dict | None = None):
    return await client.post(
        "/v1/documents/summarize",
        headers=HEADERS,
        files={"berkas_dokumen": berkas},
        data=data or {},
    )


async def test_dokumen_panjang_diringkas_bertahap(
    client: httpx.AsyncClient, model_palsu
) -> None:
    respons = await _minta(
        client,
        ("tinjauan.txt", _dokumen(), "text/plain"),
        {"judul": "Tinjauan Keamanan Siber"},
    )

    assert respons.status_code == 202, respons.text
    job = await tunggu_job(client, respons.json()["job_id"])

    assert job["status"] == "done", job

    # Dokumen benar-benar dipotong, bukan dikirim sekaligus.
    assert job["result"]["potongan_total"] > 1
    assert len(model_palsu["potongan"]) == job["result"]["potongan_total"]

    # Setiap ringkasan potongan sampai ke tahap penggabungan. Inilah yang gagal
    # pada percobaan dokumen 39 halaman sebelumnya.
    for nomor in range(1, job["result"]["potongan_total"] + 1):
        assert f"penanda-{nomor}" in model_palsu["gabungan"]


async def test_setiap_bagian_dokumen_menjadi_bagian_laporan(
    client: httpx.AsyncClient, model_palsu
) -> None:
    """Badan laporan disusun dari catatan tiap potongan, tanpa dipadatkan.

    Sebelumnya tujuh potongan digabung menjadi lima atau enam bagian oleh tahap
    penggabungan, sehingga isinya dipadatkan dua kali dan laporannya jadi
    pendek. Sekarang satu potongan menjadi satu bagian, jadi panjang laporan
    mengikuti panjang dokumennya.
    """
    respons = await _minta(client, ("tinjauan.txt", _dokumen(), "text/plain"))
    job_id = respons.json()["job_id"]
    job = await tunggu_job(client, job_id)

    assert job["result"]["jumlah_bagian"] == job["result"]["potongan_total"]
    assert job["result"]["jumlah_paragraf"] >= job["result"]["jumlah_bagian"]

    berkas = await client.get(
        f"/v1/jobs/{job_id}/artifact?format=json", headers=HEADERS
    )
    rincian = berkas.json()

    # Berkas .txt tidak punya halaman, jadi penandanya nomor bagian, bukan
    # rentang halaman. Menyebut "(halaman 1)" untuk semuanya akan menyesatkan.
    judul_bagian = [bagian["judul"] for bagian in rincian["bagian"]]
    assert all("(bagian " in judul for judul in judul_bagian)

    # Seluruh nama metode dari catatan ikut terbawa ke badan laporan.
    semua_paragraf = " ".join(
        paragraf
        for bagian in rincian["bagian"]
        for paragraf in bagian["paragraf"]
    )
    for nomor in range(1, job["result"]["potongan_total"] + 1):
        assert f"metode-{nomor}" in semua_paragraf


async def test_diagram_dibuang_bila_tidak_diminta(
    client: httpx.AsyncClient, model_palsu
) -> None:
    respons = await _minta(client, ("tinjauan.txt", _dokumen(), "text/plain"))
    job = await tunggu_job(client, respons.json()["job_id"])

    # Model tetap mengembalikan diagram; keputusan akhir tetap milik server.
    assert job["result"]["elemen"]["diagram"] == 0


async def test_menghasilkan_pdf_dan_json(
    client: httpx.AsyncClient, model_palsu
) -> None:
    respons = await _minta(client, ("tinjauan.txt", _dokumen(), "text/plain"))
    job_id = respons.json()["job_id"]
    job = await tunggu_job(client, job_id)

    pdf = await client.get(f"/v1/jobs/{job_id}/artifact", headers=HEADERS)
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")

    rincian = await client.get(
        f"/v1/jobs/{job_id}/artifact?format=json", headers=HEADERS
    )
    assert rincian.status_code == 200

    isi = rincian.json()
    assert isi["bagian"]
    assert len(isi["cakupan"]["potongan"]) == job["result"]["potongan_total"]
    assert isi["cakupan"]["potongan"][0]["judul_bagian"] == "Bagian 1"


async def test_bagian_yang_gagal_tidak_menggagalkan_pekerjaan(
    client: httpx.AsyncClient, model_palsu
) -> None:
    """Satu potongan gagal bukan alasan membuang seluruh pekerjaan.

    Yang penting: laporannya menyebutkan bagian mana yang tidak terbaca,
    supaya tidak ada telaah yang terlihat lengkap padahal ada bagian hilang.
    """
    model_palsu["gagal_pada"] = 2

    respons = await _minta(client, ("tinjauan.txt", _dokumen(), "text/plain"))
    job = await tunggu_job(client, respons.json()["job_id"])

    assert job["status"] == "done", job
    assert job["result"]["potongan_gagal"] == 1
    assert job["result"]["degraded"] is True
    assert "TIDAK TERBACA" in model_palsu["gabungan"]


async def test_anggaran_kurang_ditolak_sebelum_memanggil_model(
    client: httpx.AsyncClient, model_palsu, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anggaran diperiksa lebih dulu, bukan setelah tujuh bagian terlanjur dibaca.

    Tanpa pemeriksaan awal, token untuk seluruh bagian sudah terpakai lebih
    dulu, lalu penggabungannya gagal, dan tidak ada hasil apa pun yang bisa
    dipakai. Percobaan sungguhan pernah menghabiskan hampir seluruh anggaran
    sehari dengan cara seperti itu.
    """

    async def status_palsu(owner: str) -> SimpleNamespace:
        return SimpleNamespace(used=0, reserved=0, limit=200_000, remaining=100)

    monkeypatch.setattr(ledger, "status", status_palsu)

    respons = await _minta(client, ("tinjauan.txt", _dokumen(), "text/plain"))
    job = await tunggu_job(client, respons.json()["job_id"])

    assert job["status"] == "failed"
    assert job["error"]["code"] == "budget_exhausted"
    assert job["error"]["details"]["perkiraan_token"] > 100
    # Yang paling penting: tidak ada satu pun bagian yang sempat diringkas.
    assert model_palsu["potongan"] == []


async def test_dokumen_tanpa_teks_ditolak(
    client: httpx.AsyncClient, model_palsu
) -> None:
    respons = await _minta(client, ("kosong.txt", b"   \n  ", "text/plain"))
    job = await tunggu_job(client, respons.json()["job_id"])

    assert job["status"] == "failed"
    assert job["error"]["code"] == "invalid_request"


async def test_pelajar_ditolak(client: httpx.AsyncClient) -> None:
    """Membaca satu dokumen penuh itu mahal, jadi hanya untuk pengajar."""
    respons = await client.post(
        "/v1/documents/summarize",
        headers={**HEADERS, "X-Actor-Id": "siswa:budi@sekolah.id"},
        files={"berkas_dokumen": ("a.txt", b"isi dokumen", "text/plain")},
    )

    assert respons.status_code == 403
    assert respons.json()["error"]["code"] == "forbidden_for_student"


async def test_berkas_docx_ikut_dibaca(
    client: httpx.AsyncClient, model_palsu
) -> None:
    """Jenis berkas lain harus berjalan tanpa perlakuan khusus."""
    respons = await _minta(
        client,
        (
            "catatan.docx",
            _docx(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    )

    assert respons.status_code == 202, respons.text
    job = await tunggu_job(client, respons.json()["job_id"])

    assert job["status"] == "done", job
    assert job["result"]["potongan_total"] >= 1


async def test_dokumen_pendek_menjadi_satu_bagian(
    client: httpx.AsyncClient, model_palsu
) -> None:
    """Berkas dua paragraf tidak perlu dipecah, dan tidak boleh gagal."""
    pendek = "Catatan singkat.\n\nIsinya hanya dua paragraf pendek saja."

    respons = await _minta(
        client, ("pendek.txt", pendek.encode("utf-8"), "text/plain")
    )
    job = await tunggu_job(client, respons.json()["job_id"])

    assert job["status"] == "done", job
    assert job["result"]["potongan_total"] == 1
    assert job["result"]["jumlah_bagian"] == 1


async def test_judul_terlalu_pendek_ditolak(client: httpx.AsyncClient) -> None:
    respons = await _minta(
        client,
        ("a.txt", b"isi dokumen", "text/plain"),
        {"judul": "ab"},
    )

    assert respons.status_code == 400
    assert respons.json()["error"]["code"] == "invalid_request"
