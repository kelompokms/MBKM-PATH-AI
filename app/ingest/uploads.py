"""Penyimpanan berkas unggahan ke direktori kerja job.

UploadFile hanya hidup selama permintaan berlangsung. Karena pekerjaan
dijalankan sebagai job latar, berkas harus disalin ke disk SEBELUM respons
202 dikirim. Bila tidak, job akan membaca aliran yang sudah ditutup.

Pemeriksaan ukuran dilakukan saat menyalin, bukan setelah, sehingga unggahan
besar tidak pernah sepenuhnya masuk ke memori.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path, PurePath

from fastapi import UploadFile

from app.core.config import settings
from app.core.errors import FileTooLarge, UnsupportedFormat

_CHUNK = 1024 * 256
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str | None, fallback: str = "berkas") -> str:
    """Nama berkas yang aman dipakai di sistem berkas."""
    base = PurePath(name or "").name
    cleaned = _UNSAFE_NAME.sub("_", base).strip("._")
    return cleaned[:80] or fallback


def _discard_partial(path: Path) -> None:
    """Buang berkas sementara yang gagal atau kosong.

    Fungsi sinkron terpisah agar operasi pathlib tidak diletakkan langsung di
    dalam fungsi `async` (aturan ASYNC240), tanpa mengubah perilaku.
    """
    path.unlink(missing_ok=True)


def _ensure_allowed_suffix(
    filename: str, allowed_suffixes: tuple[str, ...] | None
) -> None:
    """Tolak ekstensi di luar daftar yang diizinkan pemanggil."""
    suffix = PurePath(filename).suffix.lower()
    if allowed_suffixes is not None and suffix not in allowed_suffixes:
        raise UnsupportedFormat(
            f"Format berkas '{suffix or '(tanpa ekstensi)'}' tidak didukung.",
            details={"berkas": filename, "didukung": list(allowed_suffixes)},
        )


async def _copy_upload(
    upload: UploadFile,
    target: Path,
    limit: int,
    filename: str,
) -> int:
    """Salin aliran unggahan ke disk sambil menegakkan batas ukuran."""
    written = 0
    try:
        with target.open("wb") as sink:
            while True:
                chunk = await upload.read(_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise FileTooLarge(
                        "Ukuran berkas melebihi batas yang diizinkan.",
                        details={
                            "berkas": filename,
                            "batas_mb": round(limit / 1_048_576, 2),
                        },
                    )
                sink.write(chunk)
    except BaseException:
        # Berkas separuh jadi tidak boleh tertinggal dan ikut terbaca.
        _discard_partial(target)
        raise
    finally:
        await upload.close()
    return written


async def save_upload(
    upload: UploadFile,
    dest_dir: Path,
    *,
    allowed_suffixes: tuple[str, ...] | None = None,
    max_bytes: int | None = None,
) -> Path:
    """Simpan unggahan ke direktori job dan kembalikan path tujuannya."""
    limit = max_bytes if max_bytes is not None else settings.max_file_size_bytes
    filename = safe_filename(upload.filename)
    _ensure_allowed_suffix(filename, allowed_suffixes)

    await asyncio.to_thread(dest_dir.mkdir, parents=True, exist_ok=True)
    target = dest_dir / filename

    written = await _copy_upload(upload, target, limit, filename)
    if written == 0:
        _discard_partial(target)
        raise UnsupportedFormat(
            "Berkas yang diunggah kosong.", details={"berkas": filename}
        )

    return target