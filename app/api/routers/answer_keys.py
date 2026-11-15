"""Endpoint ekstraksi kunci jawaban (tahap pertama fitur 3)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.api.deps import CallerDep, require_teacher
from app.core.errors import AppError
from app.flows.answer_keys import run_answer_key_job
from app.ingest.uploads import save_upload
from app.jobs import Job, job_runner, job_store

# Kunci jawaban adalah bagian dari penilaian, jadi hanya untuk pengajar.
router = APIRouter(
    prefix="/v1/answer-keys",
    tags=["kunci jawaban"],
    dependencies=[Depends(require_teacher)],
)

_ALLOWED = (".pdf", ".docx", ".txt", ".doc")


@router.post(
    "/extract",
    status_code=202,
    summary="Ekstrak kunci jawaban menjadi struktur terperinci",
    response_description="Job yang sedang diproses",
)
async def extract_answer_key(
    caller: CallerDep,
    berkas_kunci: UploadFile = File(
        description="PDF, DOCX, atau TXT berisi kunci jawaban"
    ),
    judul_tugas: str | None = Form(default=None),
    kelas: str | None = Form(default=None),
    mata_pelajaran: str | None = Form(default=None),
) -> dict[str, object]:
    job_runner.guard_backlog()
    job: Job = await job_store.create(
        kind="ekstraksi_kunci", owner=caller.fingerprint
    )

    try:
        berkas = await save_upload(
            berkas_kunci, job.tmp_dir / "unggahan", allowed_suffixes=_ALLOWED
        )
    except AppError:
        await job_store.discard(job.id)
        raise

    owner = caller.fingerprint
    async def kerja(running: Job) -> None:
        await run_answer_key_job(
            running,
            berkas=berkas,
            owner=owner,
            judul_tugas=(judul_tugas or "").strip() or None,
            kelas=(kelas or "").strip() or None,
            mata_pelajaran=(mata_pelajaran or "").strip() or None,
        )

    job_runner.spawn(job, kerja)
    return job.to_dict()