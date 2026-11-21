"""Titik masuk aplikasi PATH-AI."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.handlers import register_error_handlers
from app.api.middleware import RequestSizeLimitMiddleware, SecurityHeadersMiddleware
from app.api.routers import (
    answer_keys,
    assessments,
    chat,
    documents,
    health,
    jobs,
    materials,
    ops,
)
from app.core.config import settings
from app.core.llm import llm
from app.ingest.ocr import info_ocr
from app.jobs import job_runner, job_store
from app.observability.logger import get_logger, setup_logging
from app.store import answer_key_store

logger = get_logger(__name__)

_SWEEP_INTERVAL_SECONDS = 60


async def _sweep_forever() -> None:
    """Bersihkan job kedaluwarsa secara berkala.

    Tanpa ini, direktori sementara job akan menumpuk sampai disk penuh —
    dan pada layanan tanpa database, tidak ada yang lain yang akan
    membersihkannya.
    """
    while True:
        try:
            await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
            await job_store.sweep()
            await answer_key_store.sweep()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pembersih tidak boleh mati
            logger.warning("Pembersihan gagal", error=str(exc)[:160])


async def _laporkan_ocr() -> None:
    """Catat keadaan mesin OCR satu kali saat menyala.

    Pemeriksaan ini memanggil berkas tesseract, jadi dijalankan di thread dan
    hanya sekali. Gunanya satu: membuat kegagalan OCR terbaca di log pada menit
    pertama, bukan berhari-hari kemudian saat guru mengunggah berkas pindai.
    """
    info = await asyncio.to_thread(info_ocr)

    if info.siap:
        logger.info("OCR siap dipakai", keterangan=info.ringkas())
        return

    logger.warning(
        "OCR tidak siap, halaman hasil pindai tidak akan terbaca",
        keterangan=info.ringkas(),
        pengganti=settings.vision_model or "tidak ada",
        urutan_pembaca=settings.image_reader_order,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings.job_root.mkdir(parents=True, exist_ok=True)
    logger.info(
        "PATH-AI mulai",
        model_paid=settings.model_paid,
        model_free=settings.model_free,
        model_image=settings.model_image,
        budget=settings.budget_tokens_per_day,
    )
    await _laporkan_ocr()
    sweeper = asyncio.create_task(_sweep_forever(), name="job-sweeper")
    try:
        yield
    finally:
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper
        await job_runner.shutdown()
        await llm.aclose()
        logger.info("PATH-AI berhenti")


def _build_app() -> FastAPI:
    """Buat instance FastAPI beserta metadata dan pengaturan dokumentasi."""
    return FastAPI(
        title="PATH-AI",
        version="0.1.0",
        description=(
            "Layanan AI untuk pengajar: penjelasan materi, pembuatan materi, "
            "koreksi jawaban siswa, pemeriksaan kemiripan jawaban, dan "
            "pemeriksaan indikasi penggunaan AI."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )


def _configure_middleware(app: FastAPI) -> None:
    """Pasang middleware. Urutan pemasangannya sengaja dipertahankan."""
    # CORS dibatasi ke origin yang didaftarkan. Tidak pernah "*", karena
    # kunci API yang dikirim dari halaman asing adalah kebocoran kunci.
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            # `X-Actor-Id` sengaja TIDAK ada di sini. Header itu hanya
            # boleh datang dari backend; kalau browser diizinkan mengirimnya,
            # siapa pun yang sudah login bisa mengaku sebagai pengajar lain.
            allow_headers=["X-API-Key", "Content-Type"],
            expose_headers=["Content-Disposition"],
            max_age=600,
        )

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestSizeLimitMiddleware)


def _register_routes(app: FastAPI) -> None:
    """Daftarkan penangan galat, seluruh router, lalu endpoint akar."""
    register_error_handlers(app)

    # Urutan pendaftaran router dipertahankan seperti semula.
    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(materials.router)
    app.include_router(documents.router)
    app.include_router(answer_keys.router)
    app.include_router(assessments.router)
    app.include_router(jobs.router)
    app.include_router(ops.router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": "PATH-AI",
            "docs": "/docs",
            "health": "/health",
            "api_version": "v1",
        }


def _tambah_format_binary(node: object) -> None:
    """Tambahkan `format: binary` agar Swagger UI menampilkan tombol pilih berkas.

    FastAPI (OpenAPI 3.1) hanya menulis `contentMediaType: application/octet-stream`
    untuk `UploadFile`. Pada kolom array seperti `jawaban_siswa`, Swagger UI tidak
    mengenalinya sebagai unggahan berkas dan malah menampilkan input teks biasa
    ('Add string item') kecuali `format: binary` ikut dicantumkan.
    """
    if isinstance(node, dict):
        if (
            node.get("type") == "string"
            and node.get("contentMediaType") == "application/octet-stream"
        ):
            node.setdefault("format", "binary")
        for value in node.values():
            _tambah_format_binary(value)
    elif isinstance(node, list):
        for item in node:
            _tambah_format_binary(item)


def _configure_openapi(app: FastAPI) -> None:
    """Perkaya skema OpenAPI agar tombol unggah berkas tampil di Swagger UI."""
    original_openapi = app.openapi

    def custom_openapi() -> dict[str, object]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = original_openapi()
        _tambah_format_binary(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


def create_app() -> FastAPI:
    """Rakit aplikasi FastAPI dari komponen-komponennya."""
    setup_logging()
    app = _build_app()
    _configure_middleware(app)
    _register_routes(app)
    _configure_openapi(app)
    return app


app = create_app()