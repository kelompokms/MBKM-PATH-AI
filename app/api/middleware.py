"""Middleware tingkat aplikasi."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import settings


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Tolak unggahan terlalu besar sebelum isinya dibaca.

    Tanpa ini, berkas 2 GB akan masuk ke memori dulu baru ditolak — padahal
    penolakan bisa dilakukan dari header `Content-Length` tanpa menyentuh
    isi request sama sekali.

    `Content-Length` tidak selalu ada (mis. chunked transfer), jadi ini
    penyaring awal, bukan jaminan. Pemeriksaan sebenarnya tetap dilakukan
    di lapisan ingest setelah berkas terbaca sebagian.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method in ("POST", "PUT", "PATCH"):
            raw_length = request.headers.get("content-length")
            too_big = (
                raw_length is not None
                and raw_length.isdigit()
                and int(raw_length) > settings.max_request_bytes
            )
            if too_big:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "file_too_large",
                            "message": (
                                "Ukuran unggahan melebihi batas "
                                f"{settings.max_request_mb} MB."
                            ),
                        }
                    },
                )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Header keamanan minimal untuk respons API."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response