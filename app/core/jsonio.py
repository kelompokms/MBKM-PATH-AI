"""Pembacaan JSON dari keluaran model, dengan pemulihan bertingkat.

Model bahasa kadang membungkus JSON dalam blok kode, memakai tanda kutip
pintar, meninggalkan koma di ujung, atau memotong jawaban sebelum kurung
penutup. Modul ini mencoba memulihkan semuanya sebelum menyatakan gagal.

Bila pemulihan pun gagal, pemanggil mendapat daftar masalah yang bisa
dibaca manusia — daftar itu yang dikirim balik ke model pada putaran
perbaikan, bukan pesan "JSON tidak valid" yang tidak memberi petunjuk.
"""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

SchemaT = TypeVar("SchemaT", bound=BaseModel)

_CODE_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?([\s\S]*?)\n?```")
_FIRST_BRACE_RE = re.compile(r"[\[{]")


def extract_json_block(text: str) -> str:
    """Ambil bagian yang paling mungkin berisi JSON."""
    if not text:
        return ""

    fence = _CODE_FENCE_RE.search(text)
    if fence:
        return fence.group(1).strip()

    start = _FIRST_BRACE_RE.search(text)
    if not start:
        return text.strip()

    candidate = text[start.start() :]
    balanced, complete = _balance(candidate)
    return balanced if complete else balanced


def _balance(fragment: str) -> tuple[str, bool]:
    """Tutup kurung/kurung siku yang belum tertutup, sadar string & escape."""
    closers: list[str] = []
    in_string = False
    escaped = False
    last_significant = ""

    for char in fragment:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            closers.append("}")
        elif char == "[":
            closers.append("]")
        elif char in "}]" and closers and closers[-1] == char:
            closers.pop()

        if not char.isspace():
            last_significant = char

    fixed = fragment
    if in_string:
        fixed += '"'
    if last_significant == ",":
        fixed = fixed.rstrip()
        fixed = fixed[:-1]

    fixed += "".join(reversed(closers))
    return fixed, not closers and not in_string


def repair(text: str) -> str:
    """Perbaikan mekanis yang aman sebelum parsing ulang."""
    fixed = extract_json_block(text)

    # Tanda kutip & tanda pisah tipografis dari model.
    fixed = (
        fixed.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )

    # Literal gaya Python yang bukan JSON.
    fixed = re.sub(r"\bTrue\b", "true", fixed)
    fixed = re.sub(r"\bFalse\b", "false", fixed)
    fixed = re.sub(r"\bNone\b", "null", fixed)

    # Koma di ujung sebelum penutup.
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)

    # Baris baru/CR di dalam string yang belum di-escape.
    fixed = _escape_raw_newlines(fixed)

    balanced, _ = _balance(fixed)
    return balanced


def _escape_raw_newlines(text: str) -> str:
    out: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
                out.append(char)
            elif char == "\\":
                escaped = True
                out.append(char)
            elif char == '"':
                in_string = False
                out.append(char)
            elif char in "\r\n":
                out.append("\\n")
            elif char == "\t":
                out.append("\\t")
            else:
                out.append(char)
        else:
            if char == '"':
                in_string = True
            out.append(char)
    return "".join(out)


def loads_lenient(text: str) -> Any:
    """Parse JSON dengan beberapa tingkat pemulihan. Raise ValueError bila gagal."""
    attempts = [text, extract_json_block(text), repair(text)]

    seen: set[str] = set()
    for attempt in attempts:
        if attempt in seen or not attempt:
            continue
        seen.add(attempt)
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            continue

    raise ValueError("Tidak ada JSON yang bisa dibaca dari keluaran model.")


def validate_into(
    text: str, schema: type[SchemaT]
) -> tuple[SchemaT | None, list[str]]:
    """Parse dan validasi terhadap skema Pydantic.

    Mengembalikan (model, []) bila berhasil, atau (None, masalah) bila gagal.
    """
    try:
        payload = loads_lenient(text)
    except ValueError as exc:
        return None, [str(exc)]

    try:
        return schema.model_validate(payload), []
    except ValidationError as exc:
        problems: list[str] = []
        for error in exc.errors()[:12]:
            location = ".".join(str(part) for part in error.get("loc", ())) or "(akar)"
            problems.append(f"{location}: {error.get('msg', 'tidak valid')}")
        return None, problems