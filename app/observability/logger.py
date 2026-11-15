"""Log terstruktur JSON dengan penyaringan data pribadi.

Aturan privasi (spec §7.4): email dan nama siswa TIDAK PERNAH masuk log.
Modul ini menegakkannya di satu tempat, bukan diserahkan pada disiplin
pemanggil:

- `redact()` membuang pola email dan memotong teks panjang.
- Pemanggil disarankan hanya mengirim indeks siswa (`student_index`), bukan
  identitasnya. Alamat email yang terlanjur dikirim akan tersamar, tapi
  nama bebas tidak bisa dikenali otomatis — karena itu indeks yang dipakai.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_MAX_VALUE_CHARS = 200

# Kunci yang isinya tidak boleh muncul di log sama sekali.
_DROP_KEYS = frozenset(
    {
        "api_key",
        "x-api-key",
        "authorization",
        "token",
        "answer_key_text",
        "student_text",
        "jawaban",
        "nama",
        "name",
        "email",
    }
)


def redact(value: Any) -> Any:
    """Samarkan email dan potong nilai panjang agar log tetap aman & terbaca."""
    if isinstance(value, str):
        masked = _EMAIL_RE.sub("<email>", value)
        if len(masked) > _MAX_VALUE_CHARS:
            masked = masked[:_MAX_VALUE_CHARS] + f"…(+{len(masked) - _MAX_VALUE_CHARS})"
        return masked
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if k.lower() in _DROP_KEYS else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value))


class _JsonStreamHandler(logging.StreamHandler):
    """Handler stdout kita sendiri.

    Kelas terpisah dipakai supaya `setup_logging` bisa mengenali handler
    yang dipasangnya pada pemanggilan sebelumnya, tanpa menempelkan
    atribut tambahan pada objek logging.
    """


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        context = getattr(record, "context", None)
        if isinstance(context, dict) and context:
            entry["ctx"] = redact(context)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


class ContextLogger:
    """Pembungkus tipis: `logger.info("...", job_id="abc")` → field `ctx`."""

    __slots__ = ("_log",)

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    def _emit(self, level: int, msg: str, context: dict[str, Any]) -> None:
        self._log.log(level, msg, extra={"context": context} if context else None)

    def debug(self, msg: str, **ctx: Any) -> None:
        if self._log.isEnabledFor(logging.DEBUG):
            self._emit(logging.DEBUG, msg, ctx)

    def info(self, msg: str, **ctx: Any) -> None:
        if self._log.isEnabledFor(logging.INFO):
            self._emit(logging.INFO, msg, ctx)

    def warning(self, msg: str, **ctx: Any) -> None:
        if self._log.isEnabledFor(logging.WARNING):
            self._emit(logging.WARNING, msg, ctx)

    def error(self, msg: str, **ctx: Any) -> None:
        self._emit(logging.ERROR, msg, ctx)

    def exception(self, msg: str, **ctx: Any) -> None:
        self._emit(logging.ERROR, msg, ctx)


def setup_logging(level: str | None = None) -> None:
    """Pasang formatter JSON ke root logger, sekali saja."""
    resolved = getattr(logging, (level or settings.log_level).upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(resolved)

    # Hindari handler ganda saat reload / beberapa kali pemanggilan.
    for handler in list(root.handlers):
        if isinstance(handler, _JsonStreamHandler):
            root.removeHandler(handler)

    handler = _JsonStreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)

    # uvicorn memasang handler sendiri; biarkan propagate ke root agar seragam.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).handlers = []
        logging.getLogger(noisy).propagate = True

    # httpx mencatat setiap request; terlalu berisik pada level INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> ContextLogger:
    return ContextLogger(name)