"""Test integrasi fitur 3, 4, dan 5 dengan model disimulasikan.

Yang diuji adalah aturan yang tidak boleh diserahkan ke model: aritmetika
nilai, pembuangan kutipan bukti yang tidak ada di jawaban siswa, penolakan
kunci yang belum diverifikasi, dan ketersediaan berkas hasil.

Dua jalur masukan diuji di sini: berkas per siswa (jalur utama), dan satu ZIP
berisi manifest (jalur alternatif).
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import httpx
import openpyxl
import pytest
from app.core.config import settings
from app.core.llm import Completion, LLMResponse, llm

from tests.conftest import HEADERS, tunggu_job

JAWABAN = {
    "001.txt": (
        "Fotosintesis terjadi di kloroplas dan menghasilkan glukosa. "
        "Proses ini membutuhkan cahaya matahari, air, dan karbon dioksida."
    ),
    "002.txt": (
        "Tumbuhan membuat makanan melalui fotosintesis. Daun menangkap cahaya "
        "lalu mengubahnya menjadi energi."
    ),
    "003.txt": (
        "Fotosintesis adalah proses pembuatan makanan pada tumbuhan dengan "
        "bantuan cahaya matahari yang terjadi di dalam kloroplas."
    ),
}

MANIFEST = (
    "email,nama,nama_file\n"
    "ani@sekolah.id,Ani Pratiwi,001.txt\n"
    "budi@sekolah.id,Budi Santoso,002.txt\n"
    "citra@sekolah.id,Citra Dewi,003.txt\n"
)

KUNCI_JSON = {
    "judul_tugas": "Ulangan Harian 1",
    "kelas": "VII A",
    "mata_pelajaran": "Biologi",
    "confidence": 0.92,
    "issues": [],
    "items": [
        {
            "nomor": 1,
            "butir_soal": "Di mana fotosintesis berlangsung?",
            "kunci_jawaban": "Di dalam kloroplas pada sel tumbuhan.",
            "jenis": "objektif",
            "bobot": 1,
            "kriteria": [],
        },
        {
            "nomor": 2,
            "butir_soal": "Jelaskan proses fotosintesis.",
            "kunci_jawaban": "Tumbuhan menangkap cahaya, mengubah air dan "
            "karbon dioksida menjadi glukosa serta oksigen.",
            "jenis": "esai",
            "bobot": 3,
            "kriteria": ["Menyebut cahaya", "Menyebut hasil glukosa"],
        },
    ],
}

IDENTITAS = {
    "pengajar": "Ratna Kusumawardani",
    "mata_pelajaran": "Biologi",
    "kelas": "VII A",
    "judul_tugas": "Ulangan Harian 1",
}


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.csv", MANIFEST.encode())
        for name, text in JAWABAN.items():
            archive.writestr(name, text.encode())
    return buffer.getvalue()


def _completion(task: str = "item_grade") -> Completion:
    return Completion(
        response=LLMResponse(
            content="{}",
            model=settings.model_paid,
            prompt_tokens=400,
            completion_tokens=120,
            total_tokens=520,
        ),
        task=task,
        route="paid",
        requested_model=settings.model_paid,
    )


@pytest.fixture
def model_asesmen(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Model tiruan yang menjawab sesuai jenis task."""
    stats = {"key_extract": 0, "item_grade": 0, "class_summary": 0}

    async def fake_structured(*, task, messages, owner, schema, **kwargs):
        task_name = str(task)

        if task_name == "key_extract":
            stats["key_extract"] += 1
            return schema.model_validate(KUNCI_JSON), _completion(task_name)

        if task_name == "item_grade":
            stats["item_grade"] += 1
            teks = str(messages[-1]["content"])

            # Satu kutipan dikarang untuk siswa yang tidak menulisnya, untuk
            # memastikan lapisan verifikasi benar-benar membuangnya.
            if "menghasilkan glukosa" in teks:
                kutipan = (
                    "Fotosintesis terjadi di kloroplas dan menghasilkan glukosa."
                )
            elif "pembuatan makanan pada tumbuhan" in teks:
                kutipan = (
                    "proses pembuatan makanan pada tumbuhan dengan bantuan "
                    "cahaya matahari"
                )
            else:
                kutipan = "kalimat ini tidak pernah ditulis siswa"

            return (
                schema.model_validate(
                    {
                        "nilai_per_item": [
                            {"nomor": 1, "skor": 100, "kutipan_bukti": "", "catatan": "Tepat."},
                            {
                                "nomor": 2,
                                "skor": 50,
                                "kutipan_bukti": kutipan,
                                "catatan": "Sebagian isi kunci belum disebut.",
                            },
                        ],
                        "saran": "Perdalam bagian hasil fotosintesis.",
                    }
                ),
                _completion(task_name),
            )

        if task_name == "class_summary":
            stats["class_summary"] += 1
            return (
                schema.model_validate(
                    {"deskripsi": ["Capaian kelas berada pada kategori cukup."]}
                ),
                _completion(task_name),
            )

        raise AssertionError(f"Task tak terduga: {task_name}")

    monkeypatch.setattr(llm, "structured", fake_structured)
    return stats


async def _ekstrak_kunci(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/v1/answer-keys/extract",
        headers=HEADERS,
        data={"judul_tugas": "Ulangan Harian 1", "kelas": "VII A"},
        files={
            "berkas_kunci": (
                "kunci.txt",
                b"1. Kloroplas. 2. Cahaya diubah menjadi glukosa.",
                "text/plain",
            )
        },
    )
    assert response.status_code == 202, response.text
    return await tunggu_job(client, response.json()["job_id"])


async def test_ekstraksi_kunci_menghasilkan_butir(client: httpx.AsyncClient, model_asesmen) -> None:
    job = await _ekstrak_kunci(client)

    assert job["status"] in {"done", "needs_review"}
    assert job["result"]["jumlah_butir"] == 2
    assert job["result"]["answer_key_id"]
    assert job["result"]["items"][1]["bobot"] == 3

    artifact = await client.get(f"/v1/jobs/{job['job_id']}/artifact", headers=HEADERS)
    assert artifact.status_code == 200
    assert artifact.json()["answer_key_id"] == job["result"]["answer_key_id"]


async def test_kunci_keyakinan_rendah_menandai_perlu_review(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    rendah = dict(KUNCI_JSON)
    rendah["confidence"] = 0.31
    rendah["issues"] = ["Butir 3 tidak terbaca"]

    async def fake_structured(*, task, messages, owner, schema, **kwargs):
        if str(task) == "key_extract":
            return schema.model_validate(rendah), _completion("key_extract")
        raise AssertionError("Task lain tidak diharapkan")

    monkeypatch.setattr(llm, "structured", fake_structured)

    job = await _ekstrak_kunci(client)

    assert job["status"] == "needs_review"
    assert job["result"]["needs_review"] is True
    assert job["result"]["issues"] == ["Butir 3 tidak terbaca"]


async def test_koreksi_menghitung_nilai_dan_membuang_kutipan_palsu(
    client: httpx.AsyncClient, model_asesmen
) -> None:
    kunci = await _ekstrak_kunci(client)
    answer_key_id = kunci["result"]["answer_key_id"]

    response = await client.post(
        "/v1/assessments/grade",
        headers=HEADERS,
        data={**IDENTITAS, "answer_key_id": answer_key_id},
        files={
            "jawaban_zip": ("jawaban.zip", _zip_bytes(), "application/zip"),
        },
    )
    assert response.status_code == 202, response.text
    job = await tunggu_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    hasil = job["result"]

    # (100*1 + 50*3) / 4 = 62.5 → Cukup
    ani = next(s for s in hasil["siswa"] if s["nama"] == "Ani Pratiwi")
    assert ani["nilai"] == 62.5
    assert ani["label"] == "Cukup"

    # Kutipan yang benar-benar ada di jawaban dipertahankan.
    butir2 = next(i for i in ani["nilai_per_butir"] if i["nomor"] == 2)
    assert butir2["kutipan_terverifikasi"] is True
    assert butir2["kutipan"].startswith("Fotosintesis terjadi")

    # Kutipan karangan untuk Budi dibuang, skor tetap dihitung.
    budi = next(s for s in hasil["siswa"] if s["nama"] == "Budi Santoso")
    budi_butir2 = next(i for i in budi["nilai_per_butir"] if i["nomor"] == 2)
    assert budi_butir2["kutipan_terverifikasi"] is False
    assert budi_butir2["kutipan"] == ""
    assert budi["nilai"] == 62.5

    assert hasil["deskripsi_hasil_analisa"]
    assert hasil["jumlah_baris"] == 3


async def test_koreksi_menyediakan_excel_dan_json(
    client: httpx.AsyncClient, model_asesmen
) -> None:
    kunci = await _ekstrak_kunci(client)
    response = await client.post(
        "/v1/assessments/grade",
        headers=HEADERS,
        data={**IDENTITAS, "answer_key_id": kunci["result"]["answer_key_id"]},
        files={"jawaban_zip": ("jawaban.zip", _zip_bytes(), "application/zip")},
    )
    job = await tunggu_job(client, response.json()["job_id"])
    assert job["status"] == "done", job

    excel = await client.get(f"/v1/jobs/{job['job_id']}/artifact", headers=HEADERS)
    # Berkas Excel adalah arsip ZIP, jadi isinya selalu diawali tanda tangan PK.
    assert excel.content.startswith(b"PK")
    assert "spreadsheetml" in excel.headers["content-type"]

    js = await client.get(
        f"/v1/jobs/{job['job_id']}/artifact?format=json", headers=HEADERS
    )
    assert js.status_code == 200
    payload = js.json()
    assert payload["judul_laporan"] == "Analisa Capaian Hasil Belajar Siswa"
    assert len(payload["siswa"]) == 3

    # PDF tidak lagi dibuat untuk koreksi, dan permintaan itu dijawab dengan
    # keterangan, bukan dengan berkas yang salah jenis.
    pdf = await client.get(
        f"/v1/jobs/{job['job_id']}/artifact?format=pdf", headers=HEADERS
    )
    assert pdf.status_code == 409
    assert "tidak menghasilkan berkas PDF" in pdf.json()["error"]["message"]


async def test_kunci_belum_diverifikasi_ditolak_saat_stop(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    rendah = dict(KUNCI_JSON)
    rendah["confidence"] = 0.2

    async def fake_structured(*, task, messages, owner, schema, **kwargs):
        if str(task) == "key_extract":
            return schema.model_validate(rendah), _completion("key_extract")
        raise AssertionError("Penilaian seharusnya tidak berjalan")

    monkeypatch.setattr(llm, "structured", fake_structured)

    kunci = await _ekstrak_kunci(client)
    response = await client.post(
        "/v1/assessments/grade",
        headers=HEADERS,
        data={**IDENTITAS, "answer_key_id": kunci["result"]["answer_key_id"]},
        files={"jawaban_zip": ("jawaban.zip", _zip_bytes(), "application/zip")},
    )
    job = await tunggu_job(client, response.json()["job_id"])

    assert job["status"] == "failed"
    assert job["error"]["code"] == "answer_key_needs_review"


async def test_kunci_hasil_perbaikan_guru_dipakai(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    dipakai: list[str] = []

    async def fake_structured(*, task, messages, owner, schema, **kwargs):
        task_name = str(task)
        if task_name == "item_grade":
            dipakai.append(messages[-1]["content"])
            assert "Jahe" in messages[-1]["content"]  # kunci perbaikan guru
            return (
                schema.model_validate(
                    {
                        "nilai_per_item": [
                            {"nomor": 1, "skor": 100, "kutipan_bukti": "", "catatan": ""},
                            {"nomor": 2, "skor": 100, "kutipan_bukti": "", "catatan": ""},
                        ],
                        "saran": "Bagus.",
                    }
                ),
                _completion(task_name),
            )
        if task_name == "class_summary":
            return (
                schema.model_validate({"deskripsi": ["Baik."]}),
                _completion(task_name),
            )
        raise AssertionError(f"Task tak terduga: {task_name}")

    monkeypatch.setattr(llm, "structured", fake_structured)

    kunci_guru = json.dumps(
        {
            "items": [
                {
                    "nomor": 1,
                    "butir_soal": "Apa itu jahe?",
                    "kunci_jawaban": "Jahe adalah rempah.",
                    "jenis": "objektif",
                    "bobot": 1,
                },
                {
                    "nomor": 2,
                    "butir_soal": "Sebutkan manfaat jahe.",
                    "kunci_jawaban": "Menghangatkan badan.",
                    "jenis": "esai",
                    "bobot": 1,
                },
            ]
        }
    )

    response = await client.post(
        "/v1/assessments/grade",
        headers=HEADERS,
        data={**IDENTITAS, "kunci_jawaban": kunci_guru},
        files={"jawaban_zip": ("jawaban.zip", _zip_bytes(), "application/zip")},
    )
    job = await tunggu_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    assert dipakai
    assert all(s["nilai"] == 100.0 for s in job["result"]["siswa"])


async def test_kemiripan_menghasilkan_laporan(
    client: httpx.AsyncClient, model_asesmen
) -> None:
    response = await client.post(
        "/v1/assessments/similarity",
        headers=HEADERS,
        data=IDENTITAS,
        files={"jawaban_zip": ("jawaban.zip", _zip_bytes(), "application/zip")},
    )
    assert response.status_code == 202, response.text
    job = await tunggu_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    hasil = job["result"]
    assert len(hasil["siswa"]) == 3
    assert all("persentase_kemiripan" in s for s in hasil["siswa"])
    # Ani dan Budi menulis jawaban yang mirip.
    ani = next(s for s in hasil["siswa"] if "Ani" in s["nama"])
    assert ani["persentase_kemiripan"] > 0

    excel = await client.get(f"/v1/jobs/{job['job_id']}/artifact", headers=HEADERS)
    assert excel.content.startswith(b"PK")
    assert "spreadsheetml" in excel.headers["content-type"]


async def test_kemiripan_butuh_minimal_dua_siswa(
    client: httpx.AsyncClient, model_asesmen
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.csv", "email,nama,nama_file\nani@x.id,Ani,001.txt\n")
        archive.writestr("001.txt", "jawaban ani")

    response = await client.post(
        "/v1/assessments/similarity",
        headers=HEADERS,
        data=IDENTITAS,
        files={"jawaban_zip": ("satu.zip", buffer.getvalue(), "application/zip")},
    )
    job = await tunggu_job(client, response.json()["job_id"])

    assert job["status"] == "failed"
    assert job["error"]["code"] == "invalid_request"


async def test_indikasi_ai_menghasilkan_laporan(
    client: httpx.AsyncClient, model_asesmen
) -> None:
    response = await client.post(
        "/v1/assessments/ai-usage",
        headers=HEADERS,
        data={**IDENTITAS, "jenjang": "SMP kelas VII"},
        files={"jawaban_zip": ("jawaban.zip", _zip_bytes(), "application/zip")},
    )
    assert response.status_code == 202, response.text
    job = await tunggu_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    hasil = job["result"]
    assert len(hasil["siswa"]) == 3
    assert all(0 <= s["persentase_indikasi_ai"] <= 100 for s in hasil["siswa"])
    assert all(s["band"] in {"Rendah", "Sedang", "Tinggi"} for s in hasil["siswa"])
    assert "bukan bukti" in hasil["disclaimer"]

    pdf = await client.get(f"/v1/jobs/{job['job_id']}/artifact", headers=HEADERS)
    assert pdf.content.startswith(b"%PDF")


async def test_kolom_wajib_kosong_ditolak(
    client: httpx.AsyncClient, model_asesmen
) -> None:
    response = await client.post(
        "/v1/assessments/similarity",
        headers=HEADERS,
        data={**IDENTITAS, "kelas": "   "},
        files={"jawaban_zip": ("jawaban.zip", _zip_bytes(), "application/zip")},
    )

    assert response.status_code == 400
    assert "kelas" in response.json()["error"]["details"]["kosong"]


async def test_zip_bukan_zip_ditolak(client: httpx.AsyncClient, model_asesmen) -> None:
    response = await client.post(
        "/v1/assessments/similarity",
        headers=HEADERS,
        data=IDENTITAS,
        files={"jawaban_zip": ("bukan.zip", b"ini teks biasa", "application/zip")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "archive_invalid"


# --------------------------------------------------------------------------- #
# Ketika model gagal di tengah jalan
#
# Bukan skenario teoretis. Pada pengujian dengan model sungguhan, model gratis
# mengembalikan 429 dari kolam bersama penyedia — dan CLASS_SUMMARY, PAIR_JUDGE
# serta AI_DETECT justru task yang boleh memakai model gratis itu.
# --------------------------------------------------------------------------- #


def _penilaian_contoh() -> dict[str, object]:
    return {
        "nilai_per_item": [
            {"nomor": 1, "skor": 100, "kutipan_bukti": "", "catatan": "Tepat."},
            {"nomor": 2, "skor": 50, "kutipan_bukti": "", "catatan": "Sebagian."},
        ],
        "saran": "Perdalam bagian hasil fotosintesis.",
    }


async def test_ringkasan_kelas_gagal_tidak_menghapus_hasil_penilaian(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hasil penilaian tetap utuh walau ringkasan naratif gagal dibuat."""

    async def fake_structured(*, task, messages, owner, schema, **kwargs):
        nama = str(task)
        if nama == "class_summary":
            raise RuntimeError("429 dari penyedia")
        if nama == "key_extract":
            return schema.model_validate(KUNCI_JSON), _completion("key_extract")
        return schema.model_validate(_penilaian_contoh()), _completion(nama)

    monkeypatch.setattr(llm, "structured", fake_structured)

    kunci = await _ekstrak_kunci(client)
    response = await client.post(
        "/v1/assessments/grade",
        headers=HEADERS,
        data={**IDENTITAS, "answer_key_id": kunci["result"]["answer_key_id"]},
        files={"jawaban_zip": ("jawaban.zip", _zip_bytes(), "application/zip")},
    )
    job = await tunggu_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    hasil = job["result"]

    # Angka pada tabel tetap ada dan tetap hasil perhitungan server.
    assert len(hasil["siswa"]) == 3
    assert all(s["nilai"] == 62.5 for s in hasil["siswa"])

    # Narasi diganti keterangan pengganti, bukan dikosongkan.
    narasi = " ".join(hasil["deskripsi_hasil_analisa"])
    assert "Ringkasan naratif tidak dapat dibuat" in narasi
    assert "62.5" in narasi


async def test_wasit_kemiripan_gagal_memakai_angka_deterministik(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kegagalan wasit tidak mengubah sifat deterministik pemeriksaan."""

    async def fake_structured(*, task, messages, owner, schema, **kwargs):
        raise RuntimeError("429 dari penyedia")

    monkeypatch.setattr(llm, "structured", fake_structured)

    response = await client.post(
        "/v1/assessments/similarity",
        headers=HEADERS,
        data=IDENTITAS,
        files={"jawaban_zip": ("jawaban.zip", _zip_bytes(), "application/zip")},
    )
    job = await tunggu_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    hasil = job["result"]
    assert len(hasil["siswa"]) == 3
    # Tidak ada pasangan yang tercatat "dinilai_ai" karena wasit tidak jalan.
    assert all(pair["dinilai_ai"] is False for pair in hasil["pasangan"])


async def test_penilai_indikasi_gagal_memakai_heuristik(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kegagalan penilai indikasi menyisakan angka heuristik, bukan kekosongan."""

    async def fake_structured(*, task, messages, owner, schema, **kwargs):
        raise RuntimeError("429 dari penyedia")

    monkeypatch.setattr(llm, "structured", fake_structured)

    response = await client.post(
        "/v1/assessments/ai-usage",
        headers=HEADERS,
        data=IDENTITAS,
        files={"jawaban_zip": ("jawaban.zip", _zip_bytes(), "application/zip")},
    )
    job = await tunggu_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    hasil = job["result"]
    assert len(hasil["siswa"]) == 3
    assert hasil["statistik"]["dinilai_model"] == 0
    assert all(0 <= s["persentase_indikasi_ai"] <= 100 for s in hasil["siswa"])


# --------------------------------------------------------------------------- #
# Jawaban per siswa tanpa ZIP, dan rekap Excel
# --------------------------------------------------------------------------- #

SISWA = (
    ("ani@sekolah.id", "Ani Pratiwi", "001.txt"),
    ("budi@sekolah.id", "Budi Santoso", "002.txt"),
    ("citra@sekolah.id", "Citra Dewi", "003.txt"),
)


def _berkas_per_siswa(nama_file: list[str] | None = None):
    """Berkas jawaban per siswa, siap dikirim sebagai satu permintaan."""
    dipakai = nama_file or [item[2] for item in SISWA]

    return [
        ("jawaban_siswa", (nama, JAWABAN[nama].encode(), "text/plain"))
        for nama in dipakai
    ]


def _identitas_bersama() -> dict[str, object]:
    return {
        "email": [item[0] for item in SISWA],
        "nama": [item[1] for item in SISWA],
    }


def _buka_rekap(isi: bytes) -> dict[str, Any]:
    """Buka berkas Excel hasil, dikenali dari judulnya masing-masing."""
    buku = openpyxl.load_workbook(io.BytesIO(isi))
    return {lembar["A2"].value or lembar.title: lembar for lembar in buku.worksheets}


async def test_koreksi_dari_berkas_per_siswa(
    client: httpx.AsyncClient, model_asesmen
) -> None:
    """Jalur utama: satu berkas per siswa, tanpa ZIP dan tanpa manifest."""
    kunci = await _ekstrak_kunci(client)

    response = await client.post(
        "/v1/assessments/grade",
        headers=HEADERS,
        data={
            **IDENTITAS,
            "answer_key_id": kunci["result"]["answer_key_id"],
            **_identitas_bersama(),
        },
        files=_berkas_per_siswa(),
    )
    assert response.status_code == 202, response.text
    job = await tunggu_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    assert job["result"]["jumlah_baris"] == 3

    isi = await client.get(f"/v1/jobs/{job['job_id']}/artifact", headers=HEADERS)
    semua = _buka_rekap(isi.content)
    assert len(semua) == 1
    lembar = semua["Analisa Capaian Hasil Belajar Siswa"]

    # Blok identitas terisi, bukan lagi tulisan contoh.
    assert lembar["A4"].value == "Pengajar : Ratna Kusumawardani"
    assert lembar["A6"].value == "Mapel Biologi"
    assert lembar["A7"].value == "Kelas VII A"
    assert lembar["A8"].value == "Tugas Ulangan Harian 1"
    assert "Deskripsi hasil analisa dari AI" in lembar["A9"].value

    # Tabel mulai di baris yang sama seperti template, dengan email ikut terisi.
    assert [lembar.cell(row=10, column=kolom).value for kolom in range(1, 7)] == [
        "No",
        "Nama Siswa",
        "Email",
        "Nilai",
        "Keterangan Hasil Capaian",
        "Saran Untuk Siswa",
    ]

    assert lembar["A11"].value == 1
    assert lembar["B11"].value == "Ani Pratiwi"
    assert lembar["C11"].value == "ani@sekolah.id"
    # (100*1 + 50*3) / 4 = 62,5 dan ditulis sebagai angka, bukan teks.
    assert lembar["D11"].value == 62.5
    assert lembar["E11"].value == "Cukup"
    assert lembar["F11"].value


async def test_siswa_yang_tidak_dinilai_ditulis_nol_dengan_alasan(
    client: httpx.AsyncClient, model_asesmen
) -> None:
    """Berkas kosong tetap muncul di rekap, dengan nilai 0 dan alasannya."""
    kunci = await _ekstrak_kunci(client)

    berkas = [
        ("jawaban_siswa", ("001.txt", JAWABAN["001.txt"].encode(), "text/plain")),
        ("jawaban_siswa", ("kosong.txt", b"   ", "text/plain")),
    ]

    response = await client.post(
        "/v1/assessments/grade",
        headers=HEADERS,
        data={
            **IDENTITAS,
            "answer_key_id": kunci["result"]["answer_key_id"],
            "email": ["ani@sekolah.id", "budi@sekolah.id"],
            "nama": ["Ani Pratiwi", "Budi Santoso"],
        },
        files=berkas,
    )
    job = await tunggu_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    assert job["result"]["jumlah_baris"] == 2

    isi = await client.get(f"/v1/jobs/{job['job_id']}/artifact", headers=HEADERS)
    semua = _buka_rekap(isi.content)
    assert len(semua) == 1
    lembar = semua["Analisa Capaian Hasil Belajar Siswa"]

    assert lembar["B11"].value == "Ani Pratiwi"
    assert lembar["B12"].value == "Budi Santoso"
    assert lembar["D12"].value == 0
    assert "teks" in str(lembar["E12"].value).lower()
    assert lembar["F12"].value


async def test_kemiripan_dari_berkas_per_siswa(
    client: httpx.AsyncClient, model_asesmen
) -> None:
    """Rekap kemiripan memakai kemiripan tertinggi tiap siswa."""
    response = await client.post(
        "/v1/assessments/similarity",
        headers=HEADERS,
        data={**IDENTITAS, **_identitas_bersama()},
        files=_berkas_per_siswa(),
    )
    assert response.status_code == 202, response.text
    job = await tunggu_job(client, response.json()["job_id"])

    assert job["status"] == "done", job

    isi = await client.get(f"/v1/jobs/{job['job_id']}/artifact", headers=HEADERS)
    semua = _buka_rekap(isi.content)
    assert len(semua) == 1
    lembar = semua["Analisa Plagiasi  Siswa"]

    assert lembar["A4"].value == "Pengajar : Ratna Kusumawardani"
    assert [lembar.cell(row=10, column=kolom).value for kolom in range(1, 5)] == [
        "No",
        "Nama Siswa",
        "Email",
        "Presentase Plagiasi",
    ]

    persentase = [lembar.cell(row=baris, column=4).value for baris in (11, 12, 13)]
    assert all(isinstance(nilai, (int, float)) for nilai in persentase)
    assert all(0 <= nilai <= 100 for nilai in persentase)
    # Ani dan Citra menulis jawaban yang mirip, jadi ada angka di atas nol.
    assert max(persentase) > 0


async def test_jumlah_berkas_dan_identitas_harus_sama(
    client: httpx.AsyncClient, model_asesmen
) -> None:
    response = await client.post(
        "/v1/assessments/similarity",
        headers=HEADERS,
        data={
            **IDENTITAS,
            "email": ["ani@sekolah.id", "budi@sekolah.id"],
            "nama": ["Ani Pratiwi"],
        },
        files=_berkas_per_siswa(),
    )

    assert response.status_code == 400
    rincian = response.json()["error"]["details"]
    assert rincian == {"berkas": 3, "email": 2, "nama": 1}


async def test_email_ganda_ditolak(client: httpx.AsyncClient, model_asesmen) -> None:
    """Satu email dua kali hampir pasti salah kirim, jadi ditolak di depan."""
    response = await client.post(
        "/v1/assessments/similarity",
        headers=HEADERS,
        data={
            **IDENTITAS,
            "email": ["ani@sekolah.id", "ani@sekolah.id", "citra@sekolah.id"],
            "nama": ["Ani Pratiwi", "Ani lagi", "Citra Dewi"],
        },
        files=_berkas_per_siswa(),
    )

    assert response.status_code == 400
    assert "ani@sekolah.id" in response.json()["error"]["details"]["email"]


async def test_dua_cara_pengiriman_sekaligus_ditolak(
    client: httpx.AsyncClient, model_asesmen
) -> None:
    response = await client.post(
        "/v1/assessments/similarity",
        headers=HEADERS,
        data={**IDENTITAS, **_identitas_bersama()},
        files=[
            *_berkas_per_siswa(),
            ("jawaban_zip", ("jawaban.zip", _zip_bytes(), "application/zip")),
        ],
    )

    assert response.status_code == 400
    assert "salah satu cara" in response.json()["error"]["message"]


async def test_swagger_send_empty_value_diabaikan(
    client: httpx.AsyncClient, model_asesmen
) -> None:
    """Swagger UI mengirim string kosong untuk kolom berkas opsional; harus tetap jalan."""
    response = await client.post(
        "/v1/assessments/similarity",
        headers=HEADERS,
        data={
            **IDENTITAS,
            "jawaban_siswa": "",
            "jawaban_manifest": "",
            "email": "",
            "nama": "",
        },
        files={"jawaban_zip": ("jawaban.zip", _zip_bytes(), "application/zip")},
    )
    assert response.status_code == 202, response.text
    job = await tunggu_job(client, response.json()["job_id"])
    assert job["status"] == "done", job
    assert len(job["result"]["siswa"]) == 3


async def test_openapi_jawaban_siswa_berformat_binary(
    client: httpx.AsyncClient,
) -> None:
    """Swagger UI butuh `format: binary` agar jawaban_siswa tampil sebagai pemilih berkas."""
    schema = (await client.get("/openapi.json")).json()
    body = schema["components"]["schemas"]["Body_similarity_v1_assessments_similarity_post"]
    jawaban_siswa = body["properties"]["jawaban_siswa"]

    assert jawaban_siswa["type"] == "array"
    assert jawaban_siswa["items"]["format"] == "binary"