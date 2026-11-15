"""Handler galat global.

Tujuannya satu: setiap kegagalan, dari mana pun asalnya, keluar dengan
bentuk yang sama dan pesan Bahasa Indonesia yang bisa dibaca pengajar.
Tanpa ini, FastAPI akan membocorkan pesan internal atau membalas dalam
bahasa Inggris yang tidak konsisten dengan sisa aplikasi.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError
from app.observability.logger import get_logger

logger = get_logger(__name__)

_HTTP_CODE_MAP: dict[int, str] = {
    400: "invalid_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    413: "file_too_large",
    415: "unsupported_format",
    422: "invalid_request",
    429: "too_many_requests",
    500: "internal_error",
    503: "service_unavailable",
}

# Pesan bawaan Starlette/FastAPI dalam bahasa Inggris. Bila `detail` masih
# salah satu dari ini, kita ganti dengan pesan Indonesia; bila pemanggil
# sengaja mengirim pesan sendiri, pesan itu yang dipakai.
_GENERIC_DETAILS = frozenset(
    {
        "Not Found",
        "Method Not Allowed",
        "Bad Request",
        "Internal Server Error",
        "Service Unavailable",
        "Unauthorized",
        "Forbidden",
    }
)

_INDONESIAN_DEFAULTS: dict[int, str] = {
    400: "Permintaan tidak valid.",
    401: "Kunci API tidak valid atau tidak dikirim.",
    403: "Akses ditolak.",
    404: "Sumber daya tidak ditemukan.",
    405: "Metode HTTP tidak diizinkan untuk alamat ini.",
    413: "Ukuran permintaan terlalu besar.",
    415: "Format berkas tidak didukung.",
    422: "Data permintaan tidak sesuai format.",
    429: "Terlalu banyak permintaan. Coba lagi nanti.",
    500: "Terjadi kesalahan internal.",
    503: "Layanan sedang tidak tersedia.",
}


def _json(status: int, payload: dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=status, content=payload)


async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return _json(exc.http_status, exc.to_dict())


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    problems = []
    for error in exc.errors()[:10]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "(akar)"
        problems.append(f"{location}: {error.get('msg', 'tidak valid')}")

    logger.info("Validasi permintaan gagal", path=request.url.path, problems=problems)

    return _json(
        422,
        {
            "error": {
                "code": "invalid_request",
                "message": "Data permintaan tidak lengkap atau tidak sesuai format.",
                "details": {"problems": problems},
            }
        },
    )


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    code = _HTTP_CODE_MAP.get(exc.status_code, "http_error")
    raw_detail = exc.detail if isinstance(exc.detail, str) else ""

    if raw_detail and raw_detail not in _GENERIC_DETAILS:
        message = raw_detail
    else:
        message = _INDONESIAN_DEFAULTS.get(exc.status_code, "Permintaan gagal.")

    return _json(
        exc.status_code,
        {
            "error": {
                "code": code,
                "message": message,
                "details": {"path": request.url.path},
            }
        },
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Galat tak tertangani", path=request.url.path)
    return _json(
        500,
        {
            "error": {
                "code": "internal_error",
                "message": "Terjadi kesalahan tak terduga di server.",
            }
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    # `add_exception_handler` menuntut tipe handler yang lebih sempit
    # daripada fungsi kita. Ini pola resmi di dokumentasi FastAPI.
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)