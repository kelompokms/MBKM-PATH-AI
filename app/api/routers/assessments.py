"""Endpoint asesmen: koreksi, kemiripan, dan indikasi penggunaan AI.

Ketiganya menerima kumpulan jawaban siswa dengan dua cara, dan hanya salah satu
yang dipakai dalam satu permintaan:

1. **Satu berkas per siswa.** Kolom `jawaban_siswa` diulang sebanyak siswa,
   dengan `email` dan `nama` sejajar menurut urutannya. Ini jalur utama: aplikasi
   pengajar sudah memegang daftar kelasnya, jadi identitas datang bersama
   berkasnya.
2. **Satu ZIP berisi manifest.csv** (kolom `email,nama,nama_file`), dikirim lewat
   kolom `jawaban_zip`. Berguna bila satu kelas ingin dikirim sekaligus.
"""

from __future__ import annotations

import asyncio
import json
import zipfile
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.routing import APIRoute
from starlette.datastructures import FormData

from app.api.deps import CallerDep, require_teacher
from app.core.errors import AppError, ArchiveInvalid, InvalidRequest
from app.domain.schemas import AnswerKeyDraft
from app.flows.ai_usage import IndicationContext, run_ai_usage_job
from app.flows.grading import GradingContext, run_grading_job
from app.flows.similarity import SimilarityContext, run_similarity_job
from app.ingest.batch import BerkasSiswa, JawabanMasuk, pastikan_identitas
from app.ingest.uploads import safe_filename, save_upload
from app.jobs import Job, job_runner, job_store
from app.store import answer_key_store

# Kolom berkas opsional yang sering dikirim sebagai string kosong ("") oleh
# Swagger UI ketika kotak "Send empty value" dibiarkan tercentang.
_OPTIONAL_FILE_FIELDS = frozenset(
    {"jawaban_siswa", "jawaban_zip", "jawaban_manifest", "berkas_kunci"}
)


class _AsesmenRoute(APIRoute):
    """Route khusus asesmen agar kolom berkas kosong dari Swagger UI diabaikan.

    Swagger UI mencentang 'Send empty value' secara bawaan pada kolom opsional
    multipart/form-data sehingga mengirim string kosong (""). Tanpa pembersihan
    ini, FastAPI menolaknya dengan 422 'Expected UploadFile, received: str'.
    """

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            content_type = request.headers.get("content-type", "")
            if "multipart/form-data" in content_type:
                form = await request.form()
                cleaned = [
                    (key, value)
                    for key, value in form.multi_items()
                    if not (
                        key in _OPTIONAL_FILE_FIELDS
                        and isinstance(value, str)
                        and not value.strip()
                    )
                ]
                request._form = FormData(cleaned)
            return await original(request)

        return handler


# Seluruh router ini hanya untuk pengajar. Dipasang di tingkat router, bukan
# per fungsi, supaya endpoint baru di sini ikut terlindungi tanpa perlu diingat.
router = APIRouter(
    prefix="/v1/assessments",
    tags=["asesmen"],
    dependencies=[Depends(require_teacher)],
    route_class=_AsesmenRoute,
)

_ALLOWED_ZIP = (".zip",)
_ALLOWED_JAWABAN = (".pdf", ".docx", ".txt")
_ALLOWED_KEY = (".pdf", ".docx", ".txt")
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024

_MANIFEST_NAME = "manifest.csv"


@dataclass
class _GradingInputs:
    """Jawaban dan kunci yang sudah siap dipakai menilai."""

    jawaban: JawabanMasuk
    answer_key: AnswerKeyDraft | None
    kunci_path: Path | None


@dataclass
class JawabanForm:
    """Kolom unggahan jawaban siswa, dipakai ketiga endpoint asesmen.

    Dikumpulkan di satu tempat supaya definisi kolomnya tidak ditulis tiga kali,
    dan supaya tidak ada endpoint yang ketinggalan kolom saat salah satunya
    diubah.
    """

    berkas: list[UploadFile]
    email: list[str]
    nama: list[str]
    zip_berkas: UploadFile | None
    manifest_berkas: UploadFile | None


async def jawaban_form(
    jawaban_zip: UploadFile | None = File(
        default=None,
        description=(
            "[Cara 1 — ZIP, paling mudah] 1 berkas ZIP berisi manifest.csv dan "
            "berkas jawaban siswa (.pdf/.docx/.txt). Bila ini diisi, kolom Cara 2 "
            "di bawah tidak perlu diisi."
        ),
    ),
    jawaban_manifest: UploadFile | None = File(
        default=None,
        description=(
            "[Cara 1 — Opsional] Unggah manifest.csv terpisah hanya jika "
            "manifest.csv tidak berada di dalam ZIP."
        ),
    ),
    jawaban_siswa: list[UploadFile] = File(
        default=[],
        description=(
            "[Cara 2 — Per siswa] Satu berkas jawaban (.pdf/.docx/.txt) per siswa, "
            "sejajar dengan kolom email dan nama. Jangan diisi bersamaan dengan "
            "jawaban_zip."
        ),
    ),
    email: list[str] = Form(
        default=[],
        description="[Cara 2 — Per siswa] Email siswa, sejajar dengan jawaban_siswa.",
    ),
    nama: list[str] = Form(
        default=[],
        description="[Cara 2 — Per siswa] Nama siswa, sejajar dengan jawaban_siswa.",
    ),
) -> JawabanForm:
    """Kumpulkan kolom jawaban siswa menjadi satu nilai."""
    return JawabanForm(
        berkas=jawaban_siswa,
        email=email,
        nama=nama,
        zip_berkas=jawaban_zip,
        manifest_berkas=jawaban_manifest,
    )


JawabanDep = Annotated[JawabanForm, Depends(jawaban_form)]


async def _siapkan_job(kind: str, owner: str) -> Job:
    job_runner.guard_backlog()
    return await job_store.create(kind=kind, owner=owner)


async def _simpan_zip(job: Job, berkas: UploadFile) -> Path:
    path = await save_upload(
        berkas, job.tmp_dir / "unggahan", allowed_suffixes=_ALLOWED_ZIP
    )
    # Periksa tanda tangan ZIP sebelum job dibuat. Job yang gagal seketika
    # hanya membingungkan pengajar; penolakan langsung jauh lebih jelas.
    if not await asyncio.to_thread(zipfile.is_zipfile, path):
        raise ArchiveInvalid("Berkas yang diunggah bukan ZIP yang valid atau rusak.")
    return path


async def _simpan_manifest(job: Job, berkas: UploadFile | None) -> Path | None:
    """Simpan manifest terpisah ke direktori job.

    Alur membaca manifest dari disk, bukan dari memori request, karena
    pekerjaan berjalan setelah respons dikirim.
    """
    if berkas is None:
        return None

    data = await berkas.read()
    await berkas.close()

    if not data:
        return None
    if len(data) > _MAX_MANIFEST_BYTES:
        raise InvalidRequest(
            "Berkas manifest terlalu besar (maksimal 2 MB).", http_status=413
        )

    target = job.tmp_dir / safe_filename(_MANIFEST_NAME, _MANIFEST_NAME)
    target.write_bytes(data)
    return target


def _parse_kunci_json(raw: str) -> AnswerKeyDraft:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidRequest(
            "Kolom kunci_jawaban bukan JSON yang valid.",
            details={"kesalahan": str(exc)[:160]},
        ) from exc

    # Guru boleh mengirim hanya daftar butir, tanpa judul/kelas.
    if isinstance(payload, list):
        payload = {"items": payload}

    try:
        return AnswerKeyDraft.model_validate(payload)
    except Exception as exc:  # pesan disederhanakan untuk guru
        raise InvalidRequest(
            "Struktur kunci_jawaban tidak sesuai format yang diharapkan.",
            details={"kesalahan": str(exc)[:200]},
        ) from exc


def _validasi_identitas(
    pengajar: str, mata_pelajaran: str, kelas: str, judul_tugas: str
) -> None:
    kosong = [
        nama
        for nama, nilai in (
            ("pengajar", pengajar),
            ("mata_pelajaran", mata_pelajaran),
            ("kelas", kelas),
            ("judul_tugas", judul_tugas),
        )
        if not nilai.strip()
    ]
    if kosong:
        raise InvalidRequest(
            "Kolom berikut wajib diisi: " + ", ".join(kosong) + ".",
            details={"kosong": kosong},
        )


async def _resolve_answer_key(
    job: Job,
    *,
    owner: str,
    kunci_jawaban: str | None,
    answer_key_id: str | None,
    berkas_kunci: UploadFile | None,
) -> tuple[AnswerKeyDraft | None, Path | None]:
    """Tentukan kunci yang dipakai menilai, dari salah satu dari tiga sumber.

    Urutan prioritas: kunci JSON yang sudah diperiksa guru, kunci tersimpan
    hasil `/answer-keys/extract`, lalu berkas kunci yang perlu diekstraksi
    lebih dulu. Kunci dari guru menang karena ia sudah melewati pemeriksaan.
    """
    if kunci_jawaban and kunci_jawaban.strip():
        return _parse_kunci_json(kunci_jawaban), None

    if answer_key_id and answer_key_id.strip():
        tersimpan = await answer_key_store.get(answer_key_id.strip(), owner)
        return tersimpan.draft, None

    if berkas_kunci is not None:
        path = await save_upload(
            berkas_kunci, job.tmp_dir / "unggahan", allowed_suffixes=_ALLOWED_KEY
        )
        return None, path

    raise InvalidRequest(
        "Salah satu dari berkas_kunci, answer_key_id, atau kunci_jawaban "
        "wajib dikirim."
    )


async def _rangkai_jawaban(job: Job, jawaban: JawabanForm) -> JawabanMasuk:
    """Simpan jawaban siswa ke direktori job, lalu rangkai masukannya.

    Berkas harus disalin sebelum respons dikirim: aliran `UploadFile` hanya hidup
    selama permintaan berlangsung, sedangkan pekerjaannya berjalan setelahnya.
    """
    if jawaban.zip_berkas is not None and jawaban.berkas:
        raise InvalidRequest(
            "Kirim jawaban lewat salah satu cara saja: satu berkas per siswa "
            "pada jawaban_siswa, atau satu ZIP pada jawaban_zip."
        )

    if jawaban.zip_berkas is not None:
        return JawabanMasuk(
            zip_path=await _simpan_zip(job, jawaban.zip_berkas),
            manifest_path=await _simpan_manifest(job, jawaban.manifest_berkas),
        )

    if not jawaban.berkas:
        raise InvalidRequest(
            "Jawaban siswa belum dikirim. Kirim satu berkas per siswa pada "
            "jawaban_siswa beserta email dan namanya, atau satu ZIP pada "
            "jawaban_zip."
        )

    return JawabanMasuk(berkas=await _simpan_berkas_siswa(job, jawaban))


async def _simpan_berkas_siswa(
    job: Job, jawaban: JawabanForm
) -> tuple[BerkasSiswa, ...]:
    """Simpan satu berkas per siswa, masing-masing di foldernya sendiri."""
    if not len(jawaban.berkas) == len(jawaban.email) == len(jawaban.nama):
        raise InvalidRequest(
            "Jumlah berkas, email, dan nama harus sama banyak.",
            details={
                "berkas": len(jawaban.berkas),
                "email": len(jawaban.email),
                "nama": len(jawaban.nama),
            },
        )

    # Diperiksa sebelum berkasnya disimpan: permintaan yang daftarnya salah
    # tidak perlu menyentuh disk sama sekali.
    pastikan_identitas(jawaban.email, jawaban.nama)

    tersimpan: list[BerkasSiswa] = []

    for posisi, (berkas_siswa, alamat, nama_siswa) in enumerate(
        zip(jawaban.berkas, jawaban.email, jawaban.nama, strict=True), start=1
    ):
        # Tiap siswa disimpan di foldernya sendiri. Tanpa itu, dua siswa yang
        # menamai berkasnya sama akan saling menimpa, dan yang tertimpa justru
        # hilang tanpa jejak.
        path = await save_upload(
            berkas_siswa,
            job.tmp_dir / "jawaban" / f"{posisi:03d}",
            allowed_suffixes=_ALLOWED_JAWABAN,
        )
        tersimpan.append(BerkasSiswa(email=alamat, nama=nama_siswa, path=path))

    return tuple(tersimpan)


async def _prepare_inputs(
    job: Job,
    *,
    owner: str,
    jawaban: JawabanForm,
    kunci_jawaban: str | None,
    answer_key_id: str | None,
    berkas_kunci: UploadFile | None,
) -> _GradingInputs:
    """Simpan unggahan lalu tentukan kunci yang akan dipakai."""
    rangkaian = await _rangkai_jawaban(job, jawaban)
    answer_key, kunci_path = await _resolve_answer_key(
        job,
        owner=owner,
        kunci_jawaban=kunci_jawaban,
        answer_key_id=answer_key_id,
        berkas_kunci=berkas_kunci,
    )
    return _GradingInputs(rangkaian, answer_key, kunci_path)


def _spawn_grading(
    job: Job,
    *,
    owner: str,
    inputs: _GradingInputs,
    key_verified: bool,
    pengajar: str,
    mata_pelajaran: str,
    kelas: str,
    judul_tugas: str,
    on_low_confidence: str,
) -> None:
    """Rakit konteks penilaian, lalu jalankan job-nya di latar belakang."""
    context = GradingContext(
        answer_key=inputs.answer_key,
        key_verified=key_verified,
        pengajar=pengajar.strip(),
        mata_pelajaran=mata_pelajaran.strip(),
        kelas=kelas.strip(),
        judul_tugas=judul_tugas.strip(),
    )

    async def kerja(running: Job) -> None:
        await run_grading_job(
            running,
            owner=owner,
            context=context,
            jawaban=inputs.jawaban,
            on_low_confidence=on_low_confidence,
            kunci_path=inputs.kunci_path,
        )

    job_runner.spawn(job, kerja)


@router.post(
    "/grade",
    status_code=202,
    summary="Koreksi jawaban siswa dengan AI",
    description=(
        "Isi 4 kolom identitas wajib (`pengajar`, `mata_pelajaran`, `kelas`, "
        "`judul_tugas`), lalu:\n\n"
        "1. **Pilih salah satu cara kirim jawaban:**\n"
        "   - **Cara 1 (ZIP):** unggah `jawaban_zip` berisi `manifest.csv` + berkas "
        "siswa.\n"
        "   - **Cara 2 (Per siswa):** unggah `jawaban_siswa` beserta `email` dan `nama`.\n"
        "2. **Pilih salah satu sumber kunci jawaban:**\n"
        "   - `berkas_kunci` (PDF/DOCX/TXT), **atau**\n"
        "   - `answer_key_id` (dari `/v1/answer-keys/extract`), **atau**\n"
        "   - `kunci_jawaban` (JSON terverifikasi)."
    ),
    response_description="Job yang sedang diproses",
)
async def grade(
    caller: CallerDep,
    jawaban: JawabanDep,
    pengajar: str = Form(description="Nama pengajar (wajib)"),
    mata_pelajaran: str = Form(description="Nama mata pelajaran (wajib)"),
    kelas: str = Form(description="Nama kelas, mis. VII A (wajib)"),
    judul_tugas: str = Form(description="Judul tugas/ulangan (wajib)"),
    berkas_kunci: UploadFile | None = File(
        default=None,
        description="[Kunci — pilih salah satu] Berkas kunci jawaban PDF/DOCX/TXT",
    ),
    answer_key_id: str | None = Form(
        default=None,
        description="[Kunci — pilih salah satu] ID hasil /v1/answer-keys/extract",
    ),
    kunci_jawaban: str | None = Form(
        default=None,
        description="[Kunci — pilih salah satu] Kunci jawaban terverifikasi dalam JSON",
    ),
    on_low_confidence: Literal["stop", "continue"] = Form(
        default="stop",
        description="Tindakan bila keyakinan ekstraksi kunci rendah: stop atau continue",
    ),
) -> dict[str, object]:
    _validasi_identitas(pengajar, mata_pelajaran, kelas, judul_tugas)

    owner = caller.fingerprint
    job = await _siapkan_job("koreksi_jawaban", owner)

    try:
        inputs = await _prepare_inputs(
            job,
            owner=owner,
            jawaban=jawaban,
            kunci_jawaban=kunci_jawaban,
            answer_key_id=answer_key_id,
            berkas_kunci=berkas_kunci,
        )
    except AppError:
        await job_store.discard(job.id)
        raise

    _spawn_grading(
        job,
        owner=owner,
        inputs=inputs,
        key_verified=bool(kunci_jawaban and kunci_jawaban.strip()),
        pengajar=pengajar,
        mata_pelajaran=mata_pelajaran,
        kelas=kelas,
        judul_tugas=judul_tugas,
        on_low_confidence=on_low_confidence,
    )
    return job.to_dict()


@router.post(
    "/similarity",
    status_code=202,
    summary="Periksa kemiripan jawaban antarsiswa",
    description=(
        "Isi 4 kolom identitas wajib (`pengajar`, `mata_pelajaran`, `kelas`, "
        "`judul_tugas`), lalu pilih **salah satu** cara kirim jawaban:\n\n"
        "- **Cara 1 (ZIP):** unggah `jawaban_zip` berisi `manifest.csv` + berkas siswa "
        "(minimal 2 siswa).\n"
        "- **Cara 2 (Per siswa):** unggah `jawaban_siswa` beserta `email` dan `nama`."
    ),
    response_description="Job yang sedang diproses",
)
async def similarity(
    caller: CallerDep,
    jawaban: JawabanDep,
    pengajar: str = Form(description="Nama pengajar (wajib)"),
    mata_pelajaran: str = Form(description="Nama mata pelajaran (wajib)"),
    kelas: str = Form(description="Nama kelas, mis. VII A (wajib)"),
    judul_tugas: str = Form(description="Judul tugas/ulangan (wajib)"),
) -> dict[str, object]:
    _validasi_identitas(pengajar, mata_pelajaran, kelas, judul_tugas)

    owner = caller.fingerprint
    job = await _siapkan_job("kemiripan_jawaban", owner)

    try:
        rangkaian = await _rangkai_jawaban(job, jawaban)
    except AppError:
        await job_store.discard(job.id)
        raise

    context = SimilarityContext(
        pengajar=pengajar.strip(),
        mata_pelajaran=mata_pelajaran.strip(),
        kelas=kelas.strip(),
        judul_tugas=judul_tugas.strip(),
    )

    async def kerja(running: Job) -> None:
        await run_similarity_job(
            running, owner=owner, context=context, jawaban=rangkaian
        )

    job_runner.spawn(job, kerja)
    return job.to_dict()


@router.post(
    "/ai-usage",
    status_code=202,
    summary="Periksa indikasi penggunaan AI pada jawaban siswa",
    description=(
        "Isi 4 kolom identitas wajib (`pengajar`, `mata_pelajaran`, `kelas`, "
        "`judul_tugas`), lalu pilih **salah satu** cara kirim jawaban:\n\n"
        "- **Cara 1 (ZIP):** unggah `jawaban_zip` berisi `manifest.csv` + berkas siswa.\n"
        "- **Cara 2 (Per siswa):** unggah `jawaban_siswa` beserta `email` dan `nama`."
    ),
    response_description="Job yang sedang diproses",
)
async def ai_usage(
    caller: CallerDep,
    jawaban: JawabanDep,
    pengajar: str = Form(description="Nama pengajar (wajib)"),
    mata_pelajaran: str = Form(description="Nama mata pelajaran (wajib)"),
    kelas: str = Form(description="Nama kelas, mis. VII A (wajib)"),
    judul_tugas: str = Form(description="Judul tugas/ulangan (wajib)"),
    jenjang: str | None = Form(
        default=None, description="Jenjang pendidikan, mis. SMP kelas VII (opsional)"
    ),
) -> dict[str, object]:
    _validasi_identitas(pengajar, mata_pelajaran, kelas, judul_tugas)

    owner = caller.fingerprint
    job = await _siapkan_job("indikasi_ai", owner)

    try:
        rangkaian = await _rangkai_jawaban(job, jawaban)
    except AppError:
        await job_store.discard(job.id)
        raise

    context = IndicationContext(
        pengajar=pengajar.strip(),
        mata_pelajaran=mata_pelajaran.strip(),
        kelas=kelas.strip(),
        judul_tugas=judul_tugas.strip(),
        jenjang=(jenjang or "").strip() or None,
    )

    async def kerja(running: Job) -> None:
        await run_ai_usage_job(
            running, owner=owner, context=context, jawaban=rangkaian
        )

    job_runner.spawn(job, kerja)
    return job.to_dict()