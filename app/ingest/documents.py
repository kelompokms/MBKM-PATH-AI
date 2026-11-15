"""Pembacaan berkas menjadi teks, berlapis sesuai biayanya.

Urutannya dari yang termurah:

1. `.txt` dibaca langsung. Nol biaya.
2. `.docx` dibaca dengan python-docx. Nol biaya.
3. `.pdf` dengan lapisan teks dibaca langsung per halaman. Nol biaya.
4. Halaman PDF yang hanya berisi gambar (hasil pindai/foto) baru dikirim ke
   OCR. Ini bagian yang mahal, jadi hanya halaman yang benar-benar butuh
   yang diproses, dan jumlahnya dibatasi.

`.doc` lama sengaja DITOLAK dengan pesan yang menyebut jalan keluarnya.
Di aplikasi sebelumnya ekstensi itu diterima lalu gagal dengan galat internal
yang tidak menjelaskan apa pun.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePath

import docx
import pymupdf

from app.core.config import settings
from app.core.errors import UnsupportedFormat
from app.domain.models import ExtractedDocument
from app.ingest.ocr import ocr_pdf_pages
from app.observability.logger import get_logger

logger = get_logger(__name__)

SUPPORTED_SUFFIXES = (".pdf", ".docx", ".txt")

# Di bawah jumlah karakter ini, halaman PDF dianggap tidak punya teks nyata.
# Angka 40 cukup untuk membedakan "" dari halaman yang benar-benar berisi.
_TEXT_LAYER_MIN_CHARS = 40

# Pembaca alternatif untuk halaman bergambar, mis. model vision.
# Disuntikkan oleh pemanggil agar lapisan ingest tidak bergantung pada LLM.
VisionReader = Callable[[Path, list[int]], Awaitable[str]]

_DOC_HINT = (
    "Format .doc lama tidak didukung. Simpan ulang berkas sebagai .docx, "
    "PDF, atau .txt lalu unggah kembali."
)


def detect_suffix(path: Path) -> str:
    return PurePath(path.name).suffix.lower()


def ensure_supported(path: Path) -> str:
    suffix = detect_suffix(path)
    if suffix == ".doc":
        raise UnsupportedFormat(_DOC_HINT, details={"berkas": path.name})
    if suffix not in SUPPORTED_SUFFIXES:
        raise UnsupportedFormat(
            "Format berkas tidak didukung. Gunakan PDF, DOCX, atau TXT.",
            details={"berkas": path.name, "didukung": list(SUPPORTED_SUFFIXES)},
        )
    return suffix


def _read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _read_docx_sync(path: Path) -> tuple[str, int]:
    document = docx.Document(str(path))
    blocks: list[str] = []

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            blocks.append(text)

    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                blocks.append(" | ".join(cells))

    return "\n".join(blocks), 1


def _collect_pdf_pages(path: Path) -> tuple[list[str], list[int], int]:
    """Baca lapisan teks tiap halaman dan catat halaman yang perlu OCR."""
    text_parts: list[str] = []
    image_pages: list[int] = []

    with pymupdf.open(path) as document:
        page_count = document.page_count
        for page_number in range(page_count):
            page = document.load_page(page_number)
            page_text = (page.get_text("text") or "").strip()
            if len(page_text) >= _TEXT_LAYER_MIN_CHARS:
                text_parts.append(page_text)
                continue

            has_images = bool(page.get_images(full=True))
            if not has_images:
                # Halaman kosong: tidak ada gunanya di-OCR.
                continue

            image_pages.append(page_number)

    return text_parts, image_pages, page_count


async def _read_image_pages(
    path: Path,
    ocr_pages: list[int],
    vision_reader: VisionReader | None,
) -> str:
    """Ambil teks halaman bergambar lewat vision reader atau OCR lokal."""
    if vision_reader is not None:
        return await vision_reader(path, ocr_pages)
    results = await ocr_pdf_pages(path, ocr_pages)
    return "\n".join(results.get(page, "") for page in ocr_pages)


async def _read_pdf(
    path: Path,
    *,
    max_ocr_pages: int,
    vision_reader: VisionReader | None,
) -> ExtractedDocument:
    """Ekstrak PDF: pakai lapisan teks dulu, OCR hanya untuk halaman gambar."""
    text_parts, image_pages, page_count = _collect_pdf_pages(path)

    ocr_pages = image_pages[:max_ocr_pages]
    overflow = len(image_pages) - len(ocr_pages)

    if ocr_pages:
        ocr_text = await _read_image_pages(path, ocr_pages, vision_reader)
        if ocr_text.strip():
            text_parts.append(ocr_text.strip())

    if overflow > 0:
        logger.warning(
            "Sebagian halaman tidak dibaca karena batas OCR",
            dilewati=overflow,
            batas=max_ocr_pages,
        )

    return ExtractedDocument(
        text="\n\n".join(text_parts).strip(),
        pages=page_count,
        ocr_pages=len(ocr_pages),
        ocr_skipped=max(0, overflow),
    )


async def extract_document(
    path: Path,
    *,
    max_ocr_pages: int | None = None,
    vision_reader: VisionReader | None = None,
) -> ExtractedDocument:
    """Baca satu berkas menjadi teks."""
    suffix = ensure_supported(path)
    page_cap = max_ocr_pages if max_ocr_pages is not None else settings.max_ocr_pages

    if suffix == ".txt":
        return ExtractedDocument(text=_read_text_file(path).strip(), pages=1)

    if suffix == ".docx":
        text, pages = await asyncio.to_thread(_read_docx_sync, path)
        return ExtractedDocument(text=text.strip(), pages=pages)

    return await _read_pdf(path, max_ocr_pages=page_cap, vision_reader=vision_reader)