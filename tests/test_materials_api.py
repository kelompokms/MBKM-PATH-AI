"""Test integrasi fitur 1 dan 2 dengan model disimulasikan.

Model tidak dipanggil sungguhan: yang diuji adalah pipeline-nya — validasi
masukan, penegakan flag di sisi server, penyusunan PDF, dan pengiriman
artefak. Perilaku model sendiri tidak bisa diuji secara deterministik.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging

import httpx
import pytest
from app.core.config import settings
from app.core.llm import Completion, LLMResponse, llm
from PIL import Image as PilImage

from tests.conftest import HEADERS, tunggu_job

MATERI_JSON = {
    "judul": "Fotosintesis pada Tumbuhan Hijau",
    "deskripsi_singkat": "Materi pengantar fotosintesis untuk kelas VII.",
    "isi": [
        {
            "judul": "Pengertian Fotosintesis",
            "paragraf": [
                "Fotosintesis mengubah energi cahaya menjadi energi kimia."
            ],
            "poin": ["Berlangsung di kloroplas"],
            "tabel": {
                "judul": "Komponen",
                "kolom": ["Komponen", "Peran"],
                "baris": [["Klorofil", "Menangkap cahaya"]],
            },
        }
    ],
    "kesimpulan": ["Fotosintesis adalah sumber energi rantai makanan."],
    "diagram": {
        "jenis": "alur",
        "judul": "Alur",
        "nodes": [{"id": "a", "label": "Cahaya"}, {"id": "b", "label": "Glukosa"}],
        "edges": [{"dari": "a", "ke": "b"}],
    },
    "ilustrasi": [
        {"judul": "Kloroplas", "prompt_gambar": "a detailed chloroplast cutaway", "keterangan": "Struktur kloroplas."}
    ],
}


def _png() -> bytes:
    buffer = io.BytesIO()
    PilImage.new("RGB", (320, 180), (43, 95, 140)).save(buffer, format="PNG")
    return buffer.getvalue()


def _fake_completion(content: str = "{}") -> Completion:
    return Completion(
        response=LLMResponse(
            content=content,
            model=settings.model_paid,
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
        ),
        task="material_content",
        route="paid",
        requested_model=settings.model_paid,
        degraded=False,
    )


@pytest.fixture
def model_palsu(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    catatan: dict[str, list] = {"messages": [], "gambar": 0}

    async def fake_structured(*, task, messages, owner, schema, **_):
        catatan["messages"].append(messages)
        payload = json.dumps(MATERI_JSON)
        return schema.model_validate(MATERI_JSON), _fake_completion(payload)

    async def fake_image(*, prompt, owner, aspect="16:9"):
        catatan["gambar"] += 1
        return _png()

    monkeypatch.setattr(llm, "structured", fake_structured)
    monkeypatch.setattr(llm, "generate_image", fake_image)
    return catatan


async def test_explain_menghasilkan_pdf(client: httpx.AsyncClient, model_palsu) -> None:
    response = await client.post(
        "/v1/materials/explain",
        headers=HEADERS,
        data={
            "pertanyaan": "Mengapa tumbuhan butuh cahaya?",
            "deskripsi_materi": "Materi pengantar fotosintesis untuk kelas VII",
            "jenjang": "SMP kelas VII",
        },
    )

    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    job = await tunggu_job(client, job_id)
    assert job["status"] == "done", job
    assert job["result"]["halaman_pdf"] >= 1
    assert job["artifact"]["media_type"] == "application/pdf"

    artifact = await client.get(f"/v1/jobs/{job_id}/artifact", headers=HEADERS)
    assert artifact.status_code == 200
    assert artifact.content.startswith(b"%PDF")


async def test_tanpa_flag_tidak_ada_tabel_diagram_ilustrasi(
    client: httpx.AsyncClient, model_palsu
) -> None:
    response = await client.post(
        "/v1/materials/generate",
        headers=HEADERS,
        data={
            "judul": "Siklus Air",
            "deskripsi_materi": "Materi siklus air untuk kelas V",
        },
    )

    job = await tunggu_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    assert job["result"]["elemen"]["tabel"] == 0
    assert job["result"]["elemen"]["diagram"] == 0
    assert job["result"]["elemen"]["ilustrasi"] == 0


async def test_kombinasi_menyalakan_ketiga_elemen(
    client: httpx.AsyncClient, model_palsu
) -> None:
    response = await client.post(
        "/v1/materials/generate",
        headers=HEADERS,
        data={
            "judul": "Fotosintesis",
            "deskripsi_materi": "Materi fotosintesis lengkap untuk kelas VII",
            "kombinasi": "true",
        },
    )

    job = await tunggu_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    elemen = job["result"]["elemen"]
    assert elemen["tabel"] == 1
    assert elemen["diagram"] == 1
    assert elemen["ilustrasi"] == 1
    assert model_palsu["gambar"] == 1


async def test_ilustrasi_gagal_tidak_menggagalkan_job(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_structured(*, task, messages, owner, schema, **_):
        return schema.model_validate(MATERI_JSON), _fake_completion()

    async def fake_image_tidak_ada(*, prompt, owner, aspect="16:9"):
        return None

    monkeypatch.setattr(llm, "structured", fake_structured)
    monkeypatch.setattr(llm, "generate_image", fake_image_tidak_ada)

    response = await client.post(
        "/v1/materials/generate",
        headers=HEADERS,
        data={
            "judul": "Fotosintesis",
            "deskripsi_materi": "Materi fotosintesis untuk kelas VII",
            "kombinasi": "true",
        },
    )

    job = await tunggu_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    assert job["result"]["elemen"]["ilustrasi"] == 0
    assert job["result"]["elemen"]["ilustrasi_diminta"] is True


async def test_log_job_mencatat_pemakaian_token(
    client: httpx.AsyncClient, model_palsu, caplog: pytest.LogCaptureFixture
) -> None:
    """Setiap job selesai meninggalkan jejak token.

    Angka inilah yang dipakai menetapkan anggaran: berapa token yang
    sebenarnya dihabiskan satu batch, bukan perkiraan dari katalog task.
    """
    caplog.set_level(logging.INFO, logger="app.jobs")

    response = await client.post(
        "/v1/materials/generate",
        headers=HEADERS,
        data={
            "judul": "Siklus Air",
            "deskripsi_materi": "Materi siklus air untuk kelas V",
        },
    )
    await tunggu_job(client, response.json()["job_id"])

    catatan = [
        record.context
        for record in caplog.records
        if record.name == "app.jobs" and record.getMessage() == "Job selesai"
    ]
    assert catatan, "log 'Job selesai' tidak ditemukan"

    terakhir = catatan[-1]
    assert "tokens_delta" in terakhir
    assert "tokens_used_hari_ini" in terakhir
    assert "tokens_remaining" in terakhir
    assert terakhir["tokens_delta"] >= 0


async def test_pertanyaan_terlalu_pendek_ditolak(
    client: httpx.AsyncClient, model_palsu
) -> None:
    response = await client.post(
        "/v1/materials/explain",
        headers=HEADERS,
        data={"pertanyaan": "apa", "deskripsi_materi": "Materi apa saja yang penting"},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"


async def test_format_berkas_tidak_didukung_ditolak(
    client: httpx.AsyncClient, model_palsu
) -> None:
    response = await client.post(
        "/v1/materials/generate",
        headers=HEADERS,
        data={
            "judul": "Siklus Air",
            "deskripsi_materi": "Materi siklus air untuk kelas V",
        },
        files={"berkas_materi": ("materi.md", b"# catatan", "text/markdown")},
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_format"


async def test_berkas_rujukan_ikut_terkirim_ke_model(
    client: httpx.AsyncClient, model_palsu
) -> None:
    response = await client.post(
        "/v1/materials/generate",
        headers=HEADERS,
        data={
            "judul": "Siklus Air",
            "deskripsi_materi": "Materi siklus air untuk kelas V",
        },
        files={
            "berkas_materi": (
                "rujukan.txt",
                b"Penguapan terjadi karena panas matahari.",
                "text/plain",
            )
        },
    )

    job = await tunggu_job(client, response.json()["job_id"])
    assert job["status"] == "done", job

    terkirim = model_palsu["messages"][0]
    isi_pesan = " ".join(m["content"] for m in terkirim)
    assert "Penguapan terjadi karena panas matahari." in isi_pesan


async def test_job_milik_kunci_lain_tidak_terlihat(
    client: httpx.AsyncClient,
    model_palsu,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Kunci kedua yang sah, untuk memastikan pemisahan kepemilikan job.
    monkeypatch.setattr(settings, "api_keys", "test-key,kunci-lain")

    response = await client.post(
        "/v1/materials/generate",
        headers=HEADERS,
        data={
            "judul": "Siklus Air",
            "deskripsi_materi": "Materi siklus air untuk kelas V",
        },
    )
    job_id = response.json()["job_id"]
    await tunggu_job(client, job_id)

    lain = await client.get(
        f"/v1/jobs/{job_id}", headers={"X-API-Key": "kunci-lain"}
    )

    # Bukan 403: membalas 403 akan mengonfirmasi keberadaan job tersebut.
    assert lain.status_code == 404
    assert lain.json()["error"]["code"] == "job_not_found"


async def test_jalur_pelajar_memakai_prompt_siswa_dan_materi_guru(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uji ujung ke ujung: dari header siswa sampai pesan yang diterima model.

    Yang dibuktikan dua hal: prompt yang dipakai adalah prompt pelajar, dan
    materi yang dikirim guru lewat `isi_materi` benar-benar menjadi sumber.
    """
    tertangkap: dict[str, str] = {}

    async def model_palsu(*, task, messages, owner, schema, **kwargs):
        tertangkap["system"] = messages[0]["content"]
        tertangkap["user"] = messages[-1]["content"]
        raise RuntimeError("berhenti setelah pesan diperiksa")

    monkeypatch.setattr(llm, "structured", model_palsu)
    monkeypatch.setattr(settings, "model_image", "")

    headers = {"X-API-Key": HEADERS["X-API-Key"], "X-Actor-Id": "siswa:budi@sekolah.id"}
    response = await client.post(
        "/v1/materials/explain",
        headers=headers,
        data={
            "pertanyaan": "Di mana fotosintesis terjadi?",
            "deskripsi_materi": "Materi fotosintesis kelas VII",
            "isi_materi": "Fotosintesis berlangsung di kloroplas sel tumbuhan.",
            "jenjang": "SMP kelas VII",
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    for _ in range(500):
        status = await client.get(f"/v1/jobs/{job_id}", headers=headers)
        if status.json()["status"] in {"done", "failed", "needs_review"}:
            break
        await asyncio.sleep(0.02)

    assert tertangkap, "model seharusnya menerima pesan sebelum gagal"
    assert "ATURAN SUMBER" in tertangkap["system"]
    assert "MATERI DARI GURU" in tertangkap["user"]
    # Teks dari `isi_materi` benar-benar diteruskan sebagai sumber.
    assert "kloroplas" in tertangkap["user"]


async def test_deskripsi_materi_boleh_kosong(
    client: httpx.AsyncClient, model_palsu
) -> None:
    """Deskripsi hanya keterangan tambahan, bukan sumber bahan.

    Tanpa deskripsi, pertanyaan pengajar tetap harus bisa dijawab, dan pesan
    ke model tidak boleh menyisakan baris menggantung.
    """
    response = await client.post(
        "/v1/materials/explain",
        headers=HEADERS,
        data={
            "pertanyaan": "Mengapa tumbuhan butuh cahaya?",
            "jenjang": "SMP kelas VII",
        },
    )

    assert response.status_code == 202, response.text
    job = await tunggu_job(client, response.json()["job_id"])
    assert job["status"] == "done", job

    isi_pesan = " ".join(m["content"] for m in model_palsu["messages"][0])
    assert "Deskripsi materi yang diminta:" not in isi_pesan


async def test_membuat_materi_tanpa_deskripsi(
    client: httpx.AsyncClient, model_palsu
) -> None:
    """Judul saja sudah cukup untuk membuat materi."""
    response = await client.post(
        "/v1/materials/generate",
        headers=HEADERS,
        data={"judul": "Siklus Air"},
    )

    assert response.status_code == 202, response.text
    job = await tunggu_job(client, response.json()["job_id"])
    assert job["status"] == "done", job


async def test_deskripsi_pendek_diterima(
    client: httpx.AsyncClient, model_palsu
) -> None:
    """Tidak ada aturan panjang untuk deskripsi, termasuk yang sangat pendek."""
    response = await client.post(
        "/v1/materials/generate",
        headers=HEADERS,
        data={"judul": "Siklus Air", "deskripsi_materi": "air"},
    )

    assert response.status_code == 202, response.text
    job = await tunggu_job(client, response.json()["job_id"])
    assert job["status"] == "done", job


async def test_pelajar_tanpa_materi_guru_ditolak(client: httpx.AsyncClient) -> None:
    """Pelajar tidak boleh dijawab dari pengetahuan umum.

    Bila materi guru kosong, aturan "hanya dari materi guru" tidak bisa
    dipegang. Lebih baik ditolak dengan pesan jelas daripada diberi jawaban
    yang tidak bisa dipertanggungjawabkan ke guru.
    """
    headers = {
        "X-API-Key": HEADERS["X-API-Key"],
        "X-Actor-Id": "siswa:budi@sekolah.id",
    }

    response = await client.post(
        "/v1/materials/explain",
        headers=headers,
        data={"pertanyaan": "Apa itu fotosintesis?"},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert "Materi dari guru belum ada isinya" in body["error"]["message"]