"""Penyimpanan job dan penjalan pekerjaan latar.

Arsitektur tanpa database: job hidup di memori dan berkasnya di direktori
sementara per job. Konsekuensi yang harus disadari dan sudah didokumentasikan:

- job hilang saat proses restart,
- server WAJIB dijalankan dengan satu worker, karena worker lain tidak bisa
  melihat job yang dibuat worker pertama.

`JobRunner` menahan pekerjaan berat dengan semaphore, sehingga job ke-N
menunggu dalam status `queued` alih-alih membuat semua job berjalan
bersamaan dan menabrak batas laju penyedia model.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, JobNotFound, TooManyJobs
from app.observability.ledger import ledger
from app.observability.logger import get_logger

logger = get_logger(__name__)


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"

    @property
    def terminal(self) -> bool:
        return self in (JobState.DONE, JobState.FAILED, JobState.NEEDS_REVIEW)


def utc_now() -> datetime:
    """Waktu sekarang dalam UTC.

    Dipakai sebagai nilai bawaan dataclass. Fungsi bernama dipilih daripada
    lambda supaya mudah dicari dan disebut di pesan galat.
    """
    return datetime.now(UTC)


@dataclass
class Job:
    id: str
    kind: str
    owner: str
    tmp_dir: Path
    state: JobState = JobState.QUEUED
    progress: int = 0
    message: str = "Menunggu giliran diproses."
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    artifact_path: Path | None = None
    artifact_name: str | None = None
    artifact_media_type: str = "application/pdf"
    # Berkas pendamping berformat JSON untuk fitur yang menghasilkan keduanya.
    json_path: Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": self.id,
            "kind": self.kind,
            "status": self.state.value,
            "progress": self.progress,
            "message": self.message,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "result": self.result,
            "error": self.error,
            "meta": self.meta,
        }
        if self.artifact_path and self.artifact_path.exists():
            payload["artifact"] = {
                "name": self.artifact_name or self.artifact_path.name,
                "media_type": self.artifact_media_type,
                "availability": f"/v1/jobs/{self.id}/artifact",
                "size_bytes": self.artifact_path.stat().st_size,
            }
            if self.json_path and self.json_path.exists():
                payload["artifact"]["json"] = {
                    "name": self.json_path.name,
                    "media_type": "application/json",
                    "availability": f"/v1/jobs/{self.id}/artifact?format=json",
                    "size_bytes": self.json_path.stat().st_size,
                }
        else:
            payload["artifact"] = None
        return payload


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def create(self, *, kind: str, owner: str) -> Job:
        job_id = uuid.uuid4().hex
        tmp_dir = settings.job_root / job_id
        tmp_dir.mkdir(parents=True, exist_ok=True)
        job = Job(id=job_id, kind=kind, owner=owner, tmp_dir=tmp_dir)
        async with self._lock:
            self._jobs[job_id] = job
        logger.info("Job dibuat", job_id=job_id, kind=kind, owner=owner)
        return job

    async def get(self, job_id: str, owner: str | None = None) -> Job:
        async with self._lock:
            job = self._jobs.get(job_id)

        # Job milik kunci lain diperlakukan sebagai tidak ada, bukan 403.
        # Membalas 403 akan mengonfirmasi keberadaan job tersebut.
        if job is None or (owner is not None and job.owner != owner):
            raise JobNotFound()
        return job

    async def update(
        self,
        job_id: str,
        *,
        state: JobState | None = None,
        progress: int | None = None,
        message: str | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        artifact_path: Path | None = None,
        artifact_name: str | None = None,
        artifact_media_type: str | None = None,
        json_path: Path | None = None,
        merge_meta: dict[str, Any] | None = None,
    ) -> Job:
        job = await self.get(job_id)
        if state is not None:
            job.state = state
            if state.terminal and job.finished_at is None:
                job.finished_at = datetime.now(UTC)
        if progress is not None:
            job.progress = max(0, min(100, progress))
        if message is not None:
            job.message = message
        if result is not None:
            job.result = result
        if error is not None:
            job.error = error
        if artifact_path is not None:
            job.artifact_path = artifact_path
        if artifact_name is not None:
            job.artifact_name = artifact_name
        if artifact_media_type is not None:
            job.artifact_media_type = artifact_media_type
        if json_path is not None:
            job.json_path = json_path
        if merge_meta:
            job.meta.update(merge_meta)
        job.touch()
        return job

    async def count_active(self) -> int:
        async with self._lock:
            return sum(1 for job in self._jobs.values() if not job.state.terminal)

    async def sweep(self) -> int:
        """Buang job kedaluwarsa dan bersihkan direktori sementaranya."""
        cutoff = datetime.now(UTC) - timedelta(minutes=settings.job_ttl_minutes)
        removed: list[Job] = []

        async with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.state.terminal and job.updated_at < cutoff:
                    removed.append(self._jobs.pop(job_id))

        for job in removed:
            _remove_dir(job.tmp_dir)

        if removed:
            logger.info("Job kedaluwarsa dibersihkan", count=len(removed))
        return len(removed)

    async def purge_all(self) -> None:
        async with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            _remove_dir(job.tmp_dir)

    async def discard(self, job_id: str) -> None:
        """Hapus job yang tidak pernah berjalan beserta direktori kerjanya."""
        async with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is not None:
            _remove_dir(job.tmp_dir)


def _remove_dir(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError as exc:  # pragma: no cover - kegagalan sistem berkas
        logger.warning("Gagal menghapus direktori job", path=str(path), error=str(exc))


# Jenis fungsi kerja: menerima job yang sedang berjalan, mengisi hasilnya.
WorkFn = Callable[[Job], Awaitable[None]]


class JobRunner:
    def __init__(self, store: JobStore, max_concurrent: int | None = None) -> None:
        self._store = store
        self._semaphore = asyncio.Semaphore(
            max_concurrent if max_concurrent is not None else settings.max_concurrent_jobs
        )
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    def spawn(self, job: Job, work: WorkFn) -> None:
        """Mulai pekerjaan untuk job yang sudah dibuat.

        Dipisah dari pembuatan job supaya pemanggil bisa menyiapkan berkas
        di dalam direktori job SEBELUM pekerjaan mulai berjalan:

            job_runner.guard_backlog()
            job = await job_store.create(kind=..., owner=...)
            await save_upload(...)          # butuh job.tmp_dir
            job_runner.spawn(job, work)     # baru dijalankan
        """
        task = asyncio.create_task(self._run(job, work), name=f"job:{job.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def guard_backlog(self) -> None:
        """Tolak permintaan baru bila antrean sudah terlalu panjang.

        Tanpa batas ini, pengajar bisa mengirim puluhan job yang semuanya
        menunggu dan menghabiskan memori proses.
        """
        # `_tasks` sudah memuat job yang antre maupun yang sedang jalan,
        # karena setiap job langsung mendapat task-nya sendiri.
        if len(self._tasks) >= settings.max_concurrent_jobs * 4:
            raise TooManyJobs(
                "Antrean job penuh. Tunggu hingga beberapa job selesai."
            )

    async def _run(self, job: Job, work: WorkFn) -> None:
        """Jalankan satu pekerjaan sambil menjaga status job tetap akurat."""
        try:
            async with self._semaphore:
                await self._begin(job)
                tokens_before = (await ledger.status(job.owner)).used
                await work(job)
                await self._settle(job, tokens_before=tokens_before)
        except asyncio.CancelledError:
            await self._fail_cancelled(job)
            raise
        except Exception as exc:  # batas terluar job
            await self._fail(job, exc)

    async def _begin(self, job: Job) -> None:
        """Tandai job berjalan sebelum fungsi kerja dipanggil."""
        await self._store.update(
            job.id,
            state=JobState.RUNNING,
            progress=1,
            message="Sedang diproses.",
        )
        logger.info("Job mulai diproses", job_id=job.id, kind=job.kind)

    async def _settle(self, job: Job, *, tokens_before: int) -> None:
        """Tetapkan status akhir bila fungsi kerja tidak menentukannya."""
        if job.state in (JobState.QUEUED, JobState.RUNNING):
            # Fungsi kerja tidak menyelesaikan status: anggap sukses.
            await self._store.update(
                job.id,
                state=JobState.DONE,
                progress=100,
                message="Selesai.",
            )
        elif job.state is JobState.DONE:
            await self._store.update(
                job.id, progress=100, message=job.message or "Selesai."
            )

        # Pemakaian token job ini, dihitung dari selisih buku besar pemiliknya.
        # Tujuannya satu: menyediakan angka nyata untuk menetapkan anggaran.
        # Angka ini perkiraan bila pemilik yang sama menjalankan dua job
        # bersamaan, karena buku besar mencatat per pemilik, bukan per job.
        status = await ledger.status(job.owner)
        logger.info(
            "Job selesai",
            job_id=job.id,
            kind=job.kind,
            tokens_delta=max(0, status.used - tokens_before),
            tokens_used_hari_ini=status.used,
            tokens_remaining=status.remaining,
        )

    async def _fail_cancelled(self, job: Job) -> None:
        """Catat job yang dibatalkan karena layanan berhenti."""
        await self._store.update(
            job.id,
            state=JobState.FAILED,
            message="Job dibatalkan karena layanan berhenti.",
            error={"code": "cancelled", "message": "Job dibatalkan."},
        )

    async def _fail(self, job: Job, exc: Exception) -> None:
        """Catat kegagalan job beserta kode galat yang sesuai."""
        if isinstance(exc, AppError):
            code, message, details = exc.code, exc.message, exc.details
            logger.warning("Job gagal", job_id=job.id, code=code, message=message)
        else:
            code, message, details = (
                "internal_error",
                "Terjadi kesalahan tak terduga saat memproses job.",
                {"type": type(exc).__name__},
            )
            logger.exception("Job gagal tak terduga", job_id=job.id)

        await self._store.update(
            job.id,
            state=JobState.FAILED,
            message=message,
            error={
                "code": code,
                "message": message,
                **({"details": details} if details else {}),
            },
        )
        # Direktori sementara tetap ada sampai TTL agar berkas yang sudah
        # sempat dibuat (misalnya hasil sebagian) tidak langsung lenyap.

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


job_store = JobStore()
job_runner = JobRunner(job_store)