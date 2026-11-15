"""Kesalahan aplikasi dengan kode stabil.

Satu kelas dasar supaya handler global bisa memetakan semuanya ke bentuk
respons yang seragam:

    {"error": {"code": "...", "message": "...", "details": {...}}}

`message` selalu Bahasa Indonesia karena dibaca langsung oleh pengajar.
`code` stabil dan boleh dipakai klien untuk bercabang logika.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Kesalahan yang sudah dipahami dan aman ditampilkan ke pengguna."""

    code: str = "internal_error"
    http_status: int = 500
    message: str = "Terjadi kesalahan internal."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        http_status: int | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details or {}
        if http_status is not None:
            self.http_status = http_status
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return {"error": payload}


# --- Autentikasi ------------------------------------------------------------


class Unauthorized(AppError):
    code = "unauthorized"
    http_status = 401
    message = "Kunci API tidak valid atau tidak dikirim pada header X-API-Key."


class ForbiddenForStudent(AppError):
    code = "forbidden_for_student"
    http_status = 403
    message = (
        "Fitur ini hanya untuk pengajar. Akun pelajar tidak dapat memakainya."
    )


# --- Anggaran ---------------------------------------------------------------


class BudgetExhausted(AppError):
    code = "budget_exhausted"
    http_status = 402
    message = (
        "Anggaran token untuk kunci API ini sudah habis. "
        "Permintaan ini tidak dapat dilanjutkan."
    )


# --- Berkas & arsip ---------------------------------------------------------


class InvalidManifest(AppError):
    code = "invalid_manifest"
    http_status = 400
    message = "Berkas manifest.csv tidak valid."


class ArchiveInvalid(AppError):
    code = "archive_invalid"
    http_status = 400
    message = "Berkas ZIP tidak dapat diproses."


class FileTooLarge(AppError):
    code = "file_too_large"
    http_status = 413
    message = "Ukuran berkas melebihi batas yang diizinkan."


class UnsupportedFormat(AppError):
    code = "unsupported_format"
    http_status = 415
    message = "Format berkas tidak didukung."


class OcrUnavailable(AppError):
    code = "ocr_unavailable"
    http_status = 503
    message = (
        "Berkas ini berisi hasil pindai yang perlu dibaca dengan OCR, "
        "tetapi mesin OCR di server tidak tersedia."
    )


class InvalidRequest(AppError):
    code = "invalid_request"
    http_status = 400
    message = "Permintaan tidak valid."


# --- Kunci jawaban ----------------------------------------------------------


class AnswerKeyNotFound(AppError):
    code = "answer_key_not_found"
    http_status = 404
    message = "Kunci jawaban tidak ditemukan atau sudah kedaluwarsa."


class AnswerKeyNeedsReview(AppError):
    code = "answer_key_needs_review"
    http_status = 409
    message = (
        "Kunci jawaban belum diverifikasi. Periksa hasil ekstraksi terlebih "
        "dahulu, atau kirim on_low_confidence=continue untuk melanjutkan."
    )


# --- Job --------------------------------------------------------------------


class JobNotFound(AppError):
    code = "job_not_found"
    http_status = 404
    message = "Job tidak ditemukan."


class JobNotReady(AppError):
    code = "job_not_ready"
    http_status = 409
    message = "Job belum selesai."


class TooManyJobs(AppError):
    code = "too_many_jobs"
    http_status = 429
    message = "Terlalu banyak job berjalan. Coba lagi setelah job sebelumnya selesai."


# --- LLM --------------------------------------------------------------------


class LLMUnavailable(AppError):
    code = "llm_unavailable"
    http_status = 503
    message = "Layanan AI sedang tidak dapat dihubungi. Silakan coba lagi."


class LLMAuthError(AppError):
    code = "llm_auth_error"
    http_status = 500
    message = (
        "Layanan AI menolak kredensial yang dikonfigurasi. "
        "Hubungi administrator sistem."
    )


class LLMCreditsExhausted(AppError):
    code = "llm_credits_exhausted"
    http_status = 402
    message = (
        "Saldo OpenRouter tidak mencukupi untuk permintaan ini. "
        "Hubungi administrator sistem."
    )


class LLMBadResponse(AppError):
    code = "llm_bad_response"
    http_status = 502
    message = "Model AI mengembalikan jawaban yang tidak sesuai format yang diminta."