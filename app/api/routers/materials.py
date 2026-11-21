"""Endpoint fitur 1 dan 2 — materi.

Keduanya mengembalikan 202 dengan job yang bisa dipantau, karena penyusunan
materi melibatkan beberapa panggilan model dan pembuatan gambar.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, File, Form, UploadFile

from app.api.deps import CallerDep, CleanMultipartRoute, TeacherDep
from app.core.errors import AppError, InvalidRequest
from app.core.llm import prefer_free_model
from app.flows.materials import MaterialRequest, run_material_job
from app.ingest.uploads import save_upload
from app.jobs import Job, job_runner, job_store

router = APIRouter(
    prefix="/v1/materials",
    tags=["materi"],
    route_class=CleanMultipartRoute,
)

# `.doc` lama tidak ada di sini dengan sengaja. Lapisan pembaca berkas memang
# menolaknya dengan pesan yang jelas, tetapi menolaknya di sini membuat job
# tidak pernah dibuat, sehingga tidak ada pekerjaan kosong yang gagal.
_ALLOWED_MATERIAL_SUFFIXES = (".pdf", ".docx", ".txt")


async def _start(
    *,
    owner: str,
    request: MaterialRequest,
    berkas_materi: UploadFile | None,
    kind: str,
    gunakan_model_gratis: bool = False,
) -> dict[str, object]:
    """Buat job, simpan unggahan, lalu jalankan pekerjaannya.

    Berkas harus disalin ke direktori job sebelum respons dikirim: aliran
    `UploadFile` hanya hidup selama permintaan berlangsung.
    """
    job_runner.guard_backlog()
    job: Job = await job_store.create(kind=kind, owner=owner)

    try:
        if berkas_materi is not None:
            request.materi_path = await save_upload(
                berkas_materi,
                job.tmp_dir / "unggahan",
                allowed_suffixes=_ALLOWED_MATERIAL_SUFFIXES,
            )
    except AppError:
        # Job belum pernah berjalan, jadi tidak ada yang bisa dipantau.
        # Hapus jejaknya agar tidak menunggu TTL tanpa guna.
        await job_store.discard(job.id)
        raise

    async def kerja(running: Job) -> None:
        with prefer_free_model(gunakan_model_gratis):
            await run_material_job(running, request=request, owner=owner)

    job_runner.spawn(job, kerja)
    return job.to_dict()


def _pastikan_materi_pelajar(
    role: str, materi: str, berkas: UploadFile | None
) -> None:
    """Pelajar hanya boleh dijawab dari materi guru.

    Kalau materinya kosong, aturan itu tidak bisa dipegang: model akan menjawab
    dari pengetahuan umum, dan guru kehilangan alat untuk memeriksa apakah
    kelasnya memahami materi yang diberikannya.
    """
    if role != "student" or materi or berkas is not None:
        return

    raise InvalidRequest(
        "Materi dari guru belum ada isinya. Minta pengajar menyiapkan materi "
        "lebih dulu, lalu gunakan nomor materi yang diberikan."
    )


def _validated(judul: str | None, pertanyaan: str | None) -> None:
    """Periksa masukan yang wajib sebelum pekerjaan dimulai.

    Deskripsi materi tidak diperiksa panjangnya dan tidak wajib diisi. Nilai
    yang sangat pendek sekalipun dibiarkan lewat: pemanggil yang tahu maksudnya
    lebih baik daripada aturan panjang yang menebak-nebak. Isi materi tetap
    menjadi sumber jawaban, bukan deskripsi.
    """
    if pertanyaan is not None and len(pertanyaan) < 5:
        raise InvalidRequest("Pertanyaan terlalu pendek (minimal 5 karakter).")
    if judul is not None and len(judul) < 3:
        raise InvalidRequest("Judul materi terlalu pendek (minimal 3 karakter).")


@router.post(
    "/explain",
    status_code=202,
    summary="Tanya materi dengan AI",
    response_description="Job yang sedang diproses",
)
async def explain_material(
    caller: CallerDep,
    pertanyaan: str = Form(description="Pertanyaan pengajar atau pelajar"),
    deskripsi_materi: str = Form(
        default="", description="Deskripsi materi yang diminta (opsional)"
    ),
    berkas_materi: UploadFile | None = File(
        default=None, description="PDF, DOCX, atau TXT (opsional)"
    ),
    isi_materi: str = Form(
        default="",
        description=(
            "Teks materi yang sudah dibaca sebelumnya. Dipakai jalur pelajar "
            "supaya berkas yang sama tidak perlu diunggah berulang."
        ),
    ),
    jenjang: str = Form(default="", description="Mis. SMP kelas VII"),
    tabel: bool = Form(default=False),
    diagram: bool = Form(default=False),
    ilustrasi: bool = Form(default=False),
    kombinasi: bool = Form(
        default=False, description="Menyalakan tabel+diagram+ilustrasi"
    ),
    gunakan_model_gratis: bool = Form(
        default=False,
        description="Toggle: true untuk memakai model gratis, false untuk token berbayar",
    ),
) -> dict[str, object]:
    pertanyaan = pertanyaan.strip()
    deskripsi = (deskripsi_materi or "").strip()
    materi = (isi_materi or "").strip()
    _validated(None, pertanyaan)
    _pastikan_materi_pelajar(caller.role, materi, berkas_materi)

    request = MaterialRequest(
        pertanyaan=pertanyaan,
        judul=None,
        deskripsi_materi=deskripsi,
        materi_text=materi or None,
        # Pelajar mendapat prompt yang menjawab pertanyaan, bukan menyusun
        # materi. Endpoint ini satu-satunya yang terbuka untuk pelajar.
        audience="student" if caller.role == "student" else "teacher",
        jenjang=(jenjang or "").strip() or None,
        tabel=tabel,
        diagram=diagram,
        ilustrasi=ilustrasi,
        kombinasi=kombinasi,
    )

    return await _start(
        owner=caller.fingerprint,
        request=request,
        berkas_materi=berkas_materi,
        kind="materi_penjelasan",
        gunakan_model_gratis=gunakan_model_gratis,
    )


@router.post(
    "/generate",
    status_code=202,
    summary="Membuat materi dengan AI",
    response_description="Job yang sedang diproses",
)
async def generate_material(
    caller: TeacherDep,
    judul: str = Form(description="Judul materi yang ditentukan pengajar"),
    deskripsi_materi: str = Form(
        default="", description="Deskripsi materi yang diminta (opsional)"
    ),
    berkas_materi: UploadFile | None = File(
        default=None, description="PDF, DOCX, atau TXT (opsional)"
    ),
    jenjang: str = Form(default="", description="Mis. SMP kelas VII"),
    tabel: bool = Form(default=False),
    diagram: bool = Form(default=False),
    ilustrasi: bool = Form(default=False),
    kombinasi: bool = Form(
        default=False, description="Menyalakan tabel+diagram+ilustrasi"
    ),
    format_keluaran: Literal["pdf", "pptx"] = Form(
        default="pdf",
        description="Format berkas hasil: pdf (bawaan) atau pptx (PowerPoint)",
    ),
    gunakan_model_gratis: bool = Form(
        default=False,
        description="Toggle: true untuk memakai model gratis, false untuk token berbayar",
    ),
) -> dict[str, object]:
    judul = judul.strip()
    deskripsi = (deskripsi_materi or "").strip()
    _validated(judul, None)

    request = MaterialRequest(
        pertanyaan=None,
        judul=judul,
        deskripsi_materi=deskripsi,
        jenjang=(jenjang or "").strip() or None,
        tabel=tabel,
        diagram=diagram,
        ilustrasi=ilustrasi,
        kombinasi=kombinasi,
        format_keluaran=format_keluaran,
    )

    return await _start(
        owner=caller.fingerprint,
        request=request,
        berkas_materi=berkas_materi,
        kind="materi_pembuatan",
        gunakan_model_gratis=gunakan_model_gratis,
    )