"""Endpoint fitur 6 — ringkasan dokumen panjang.

Dipisah dari fitur 1 dan 2 karena tiga hal: masukannya satu dokumen utuh,
bentuk keluarannya telaah bertahap, dan biayanya jauh lebih besar. Fitur 1 dan
2 memakai prompt penulis materi ajar; memaksakan dokumen 39 halaman ke jalur itu
menghasilkan ringkasan yang hanya menyebut halaman judulnya.

Karena biayanya membaca dokumen penuh, endpoint ini hanya untuk pengajar.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile

from app.api.deps import TeacherDep
from app.core.errors import AppError, InvalidRequest
from app.core.llm import prefer_free_model
from app.flows.documents_summary import DocumentRequest, run_summarize_job
from app.ingest.uploads import save_upload
from app.jobs import Job, job_runner, job_store

router = APIRouter(prefix="/v1/documents", tags=["dokumen"])

# `.doc` lama tidak ada di sini dengan sengaja, dengan alasan yang sama seperti
# pada endpoint materi: ditolak sebelum job dibuat, bukan sesudahnya.
_ALLOWED_SUFFIXES = (".pdf", ".docx", ".txt")


@router.post(
    "/summarize",
    status_code=202,
    summary="Ringkas dokumen panjang",
    response_description="Job yang sedang diproses",
)
async def summarize_document(
    caller: TeacherDep,
    berkas_dokumen: UploadFile = File(
        description="Dokumen yang akan ditelaah: PDF, DOCX, atau TXT"
    ),
    judul: str = Form(default="", description="Judul telaah (opsional)"),
    fokus: str = Form(
        default="", description="Hal yang ingin ditekankan (opsional)"
    ),
    jenjang: str = Form(default="", description="Mis. SMP kelas VII"),
    diagram: bool = Form(default=False),
    gunakan_model_gratis: bool = Form(
        default=False,
        description="Toggle: true untuk memakai model gratis, false untuk token berbayar",
    ),
) -> dict[str, object]:
    _validated(judul, fokus)

    job_runner.guard_backlog()
    job: Job = await job_store.create(kind="ringkasan_dokumen", owner=caller.fingerprint)

    try:
        materi_path = await save_upload(
            berkas_dokumen,
            job.tmp_dir / "unggahan",
            allowed_suffixes=_ALLOWED_SUFFIXES,
        )
    except AppError:
        # Job belum pernah berjalan, jadi tidak ada yang bisa dipantau.
        await job_store.discard(job.id)
        raise

    request = DocumentRequest(
        materi_path=Path(materi_path),
        judul=(judul or "").strip() or None,
        fokus=(fokus or "").strip() or None,
        jenjang=(jenjang or "").strip() or None,
        diagram=diagram,
    )

    async def kerja(running: Job) -> None:
        with prefer_free_model(gunakan_model_gratis):
            await run_summarize_job(
                running, request=request, owner=caller.fingerprint
            )

    job_runner.spawn(job, kerja)
    return job.to_dict()


def _validated(judul: str, fokus: str) -> None:
    """Hanya memeriksa masukan yang diberikan.

    Judul dan fokus sama-sama opsional. Aturan panjangnya tetap ada bila
    diisi, sekadar menangkap salah ketik yang sangat pendek.
    """
    if judul.strip() and len(judul.strip()) < 3:
        raise InvalidRequest("Judul terlalu pendek (minimal 3 karakter).")
    if fokus.strip() and len(fokus.strip()) < 3:
        raise InvalidRequest("Fokus terlalu pendek (minimal 3 karakter).")
