"""Endpoint pemantauan job.

Klien memulai pekerjaan lewat endpoint fitur, lalu memantau di sini sampai
statusnya terminal. Semua respons memakai bentuk yang sama dengan yang
dikembalikan saat job dibuat, sehingga klien bisa memakai satu tipe data.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.api.deps import CallerDep
from app.core.errors import JobNotReady
from app.jobs import Job, job_store
from app.render.excel import EXCEL_MEDIA_TYPE
from app.render.pptx import PPTX_MEDIA_TYPE

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


@router.get("/{job_id}", summary="Status job")
async def get_job(job_id: str, caller: CallerDep) -> dict[str, object]:
    job = await job_store.get(job_id, owner=caller.fingerprint)
    return job.to_dict()


@router.get(
    "/{job_id}/artifact",
    summary="Unduh berkas hasil",
    responses={
        200: {
            "content": {
                "application/pdf": {},
                "application/json": {},
                EXCEL_MEDIA_TYPE: {},
                PPTX_MEDIA_TYPE: {},
            }
        }
    },
)
async def get_artifact(
    job_id: str,
    caller: CallerDep,
    format: str = "",  # nama parameter mengikuti query string
) -> FileResponse:
    """Unduh berkas hasil: berkas utama, atau JSON rinciannya.

    `format` menentukan jenis berkas yang diminta. Berkas utama bisa berupa PDF
    atau Excel, tergantung fiturnya: materi menghasilkan PDF, sedangkan rekap
    asesmen menghasilkan Excel. Permintaan yang tidak cocok dijawab dengan
    keterangan apa yang sebenarnya tersedia, bukan dengan berkas yang salah
    jenis.
    """
    job = await job_store.get(job_id, owner=caller.fingerprint)

    if job.state.value == "failed":
        raise JobNotReady(
            "Job gagal, sehingga tidak ada berkas hasil yang bisa diunduh.",
            details={"status": job.state.value, "error": job.error},
        )

    path, media_type = _pilih_berkas(job, format)
    return FileResponse(path=path, media_type=media_type, filename=path.name)


def _pilih_berkas(job: Job, format: str) -> tuple[Path, str]:
    """Tentukan berkas yang diminta, atau jelaskan kenapa tidak ada."""
    diminta = (format or "").strip().lower()

    if diminta == "json":
        if job.json_path and job.json_path.exists():
            return job.json_path, "application/json"

        raise JobNotReady(
            "Job ini tidak menghasilkan berkas JSON.",
            details={"status": job.state.value, "progress": job.progress},
        )

    berkas = job.artifact_path
    if not berkas or not berkas.exists():
        raise JobNotReady(
            "Berkas hasil belum tersedia.",
            details={"status": job.state.value, "progress": job.progress},
        )

    jenis = job.artifact_media_type

    if diminta in {"xlsx", "excel"} and jenis != EXCEL_MEDIA_TYPE:
        raise JobNotReady(
            "Job ini tidak menghasilkan berkas Excel.",
            details={"jenis_tersedia": jenis},
        )

    if diminta == "pdf" and jenis != "application/pdf":
        raise JobNotReady(
            "Job ini tidak menghasilkan berkas PDF.",
            details={"jenis_tersedia": jenis},
        )

    if diminta in {"pptx", "ppt"} and jenis != PPTX_MEDIA_TYPE:
        raise JobNotReady(
            "Job ini tidak menghasilkan berkas PowerPoint (PPTX).",
            details={"jenis_tersedia": jenis},
        )

    return berkas, jenis