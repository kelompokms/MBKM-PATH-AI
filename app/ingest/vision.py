"""Pembacaan halaman bergambar memakai model penglihatan.

Sebagian berkas yang diunggah guru berupa gambar, bukan teks: hasil pindai, foto
halaman buku, dan foto lembar jawaban siswa. Halaman seperti itu harus dibaca
dengan pengenalan gambar, dan ada dua cara:

1. **Model penglihatan** lewat `PATH_AI_MODEL_VISION`. Tidak perlu memasang apa
   pun di server, dan mutunya lebih baik untuk tulisan tangan serta foto miring.
   Biayanya dihitung per halaman.
2. **OCR lokal (tesseract)** bila model penglihatan tidak diatur. Gratis, tetapi
   harus dipasang di server lebih dulu.

Modul ini memilih cara pertama bila tersedia, dan menyerahkan urusan cara kedua
kepada lapisan pembaca berkas seperti semula.

Alur tidak memanggil modul ini langsung. Alur memakai `buat_pembaca(owner)` untuk
mendapat fungsi pembaca, lalu menyerahkannya ke lapisan pembaca berkas. Pemilik
diikat di situ karena setiap halaman yang dibaca memakai token orang yang
mengunggah berkasnya.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pymupdf

from app.core.config import settings
from app.core.errors import AppError, BudgetExhausted, OcrUnavailable
from app.core.llm import llm
from app.core.tasks import Task
from app.ingest.documents import VisionReader
from app.ingest.ocr import ocr_pdf_pages
from app.observability.logger import get_logger

logger = get_logger(__name__)

# Kerapatan gambar saat halaman dirasterkan. 150 cukup untuk teks berukuran
# normal, sedangkan gambar yang lebih besar hanya menambah token tanpa menambah
# mutu pembacaan.
_DPI = 150

# Jumlah halaman yang dikirim ke model bersamaan. Terlalu tinggi berisiko kena
# pembatasan laju dari penyedia model.
_KONKUREN = 2

_INSTRUKSI = """\
Tuliskan kembali SELURUH teks yang terlihat pada gambar ini, apa adanya.

Aturan:
1. Tulis apa adanya. Jangan meringkas, jangan memperbaiki ejaan, dan jangan
   menambahkan penjelasan atau komentar apa pun.
2. Pertahankan urutan baris dan pemisahan paragraf sebisanya.
3. Bila ada tabel, tulis tiap barisnya dengan pemisah " | ".
4. Bila ada lambang, angka, atau rumus, tulis sedekat mungkin dengan bentuk
   aslinya. Jangan menghitung ulang dan jangan menebak yang tidak terbaca.
5. Bila ada tulisan tangan yang tidak terbaca, tulis (tidak terbaca) pada
   bagian itu.
6. Bila gambar tidak memuat teks sama sekali, tulis: (tidak ada teks)

Keluaran hanya teks hasil pembacaan, tanpa pembuka dan tanpa penutup.
"""


def buat_pembaca(owner: str) -> VisionReader | None:
    """Pembaca halaman bergambar untuk satu pemilik, atau `None` bila tidak perlu.

    `None` bukan berarti halaman bergambar tidak bisa dibaca. Lapisan pembaca
    berkas akan memakai OCR lokal, seperti sebelum jalur ini ada. Yang dimatikan
    hanyalah jalur model penglihatan.

    Ada dua urutan, dan bedanya nyata bagi biaya:

    - `local` (bawaan): OCR lokal lebih dulu. Model penglihatan hanya dipakai
      bila OCR lokal tidak tersedia. Tidak pernah mengeluarkan biaya model,
      tetapi memakai CPU server dan lebih kasar pada tulisan tangan.
    - `model`: model penglihatan lebih dulu, sehingga CPU server tetap lega.

    Tanpa model penglihatan yang diatur, keduanya berakhir sama: OCR lokal.
    """
    if not settings.vision_model:
        return None

    if settings.image_reader_order == "model":

        async def baca(path: Path, halaman: list[int]) -> str:
            return await _baca_halaman(path, halaman, owner=owner)

        return baca

    async def baca(path: Path, halaman: list[int]) -> str:
        return await _baca_lokal_dulu(path, halaman, owner=owner)

    return baca


async def _baca_lokal_dulu(path: Path, halaman: list[int], *, owner: str) -> str:
    """Coba OCR lokal lebih dulu, dan pakai model hanya bila OCR tidak tersedia.

    Hasil kosong tidak dianggap kegagalan yang perlu ditebus dengan panggilan
    berbayar. Halaman bisa saja memang kosong, dan urutan `local` berarti tidak
    ada biaya model. Kalau tesseract memang tidak sanggup, log memberi tahu
    bahwa urutan bisa diganti.
    """
    try:
        hasil = await ocr_pdf_pages(path, halaman)
    except OcrUnavailable as galat:
        logger.warning(
            "OCR lokal tidak tersedia, halaman dibaca memakai model penglihatan",
            alasan=galat.message[:120],
        )
        return await _baca_halaman(path, halaman, owner=owner)

    teks = [hasil.get(nomor, "") for nomor in halaman]
    bersih = "\n\n".join(item.strip() for item in teks if item and item.strip())

    if not bersih:
        logger.warning(
            "OCR lokal tidak menemukan teks pada halaman bergambar",
            halaman=len(halaman),
            saran="isi PATH_AI_IMAGE_READER_ORDER=model bila tulisannya tangan",
        )

    return bersih


async def _baca_halaman(path: Path, halaman: list[int], *, owner: str) -> str:
    """Baca halaman-halaman bergambar menjadi satu teks.

    Halaman yang gagal dibaca tidak menggagalkan dokumen. Halaman itu diberi
    tanda di dalam teksnya, supaya laporan atau penilaian tahu ada bagian yang
    tidak terbaca, bukan diam-diam kehilangan isinya.
    """
    if not halaman:
        return ""

    gambar = await asyncio.to_thread(_rasterkan, path, halaman)
    if not gambar:
        return ""

    semaphore = asyncio.Semaphore(_KONKUREN)
    anggaran_habis = False

    async def satu(nomor: int, png: bytes) -> str:
        nonlocal anggaran_habis

        if anggaran_habis:
            return _tanda_anggaran(nomor)

        async with semaphore:
            try:
                return await _baca_satu(nomor, png, owner=owner)
            except BudgetExhausted:
                # Anggaran habis di tengah dokumen: halaman berikutnya tidak
                # perlu dicoba, karena hasilnya pasti sama.
                anggaran_habis = True
                return _tanda_anggaran(nomor)

    hasil = await asyncio.gather(*(satu(nomor, png) for nomor, png in gambar))
    return "\n\n".join(item.strip() for item in hasil if item and item.strip())


async def _baca_satu(nomor: int, png: bytes, *, owner: str) -> str:
    """Baca satu halaman. Kegagalan dikembalikan sebagai penanda, bukan galat.

    Satu halaman yang gagal tidak boleh mematikan seluruh dokumen, tetapi juga
    tidak boleh hilang tanpa jejak: penandanya ikut ke dalam teks, sehingga
    laporan dan penilaian tahu ada bagian yang tidak terbaca.
    """
    try:
        teks = await llm.read_image(
            task=Task.PAGE_OCR,
            messages=_pesan(png),
            owner=owner,
            model=str(settings.vision_model),
        )
    except BudgetExhausted:
        raise
    except AppError as galat:
        logger.warning(
            "Halaman gagal dibaca model penglihatan",
            halaman=nomor + 1,
            kode=galat.code,
        )
        return _tanda(nomor, "teks tidak dapat dibaca dari gambar")
    except Exception as galat:  # satu halaman tidak boleh mematikan dokumen
        logger.warning(
            "Halaman gagal dibaca model penglihatan",
            halaman=nomor + 1,
            error=str(galat)[:160],
        )
        return _tanda(nomor, "teks tidak dapat dibaca dari gambar")

    # Teks kosong dari model jarang terjadi. Yang muncul justru spasi atau baris
    # kosong, dan itu sama-sama berarti tidak ada teks yang terbaca.
    bersih = teks.strip()
    return bersih or _tanda(nomor, "halaman kosong")


def _tanda_anggaran(nomor: int) -> str:
    return _tanda(nomor, "teks tidak dibaca karena anggaran token habis")


def _tanda(nomor: int, alasan: str) -> str:
    """Penanda di dalam teks untuk halaman yang tidak terbaca."""
    return f"[halaman {nomor + 1}: {alasan}]"


def _rasterkan(path: Path, halaman: list[int]) -> list[tuple[int, bytes]]:
    """Ubah halaman PDF menjadi PNG. Sinkron, karena itu dijalankan di thread."""
    hasil: list[tuple[int, bytes]] = []

    with pymupdf.open(path) as dokumen:
        for nomor in halaman:
            if nomor >= dokumen.page_count:
                continue
            piksel = dokumen.load_page(nomor).get_pixmap(dpi=_DPI)
            hasil.append((nomor, piksel.tobytes("png")))

    return hasil


def _pesan(png: bytes) -> list[dict[str, object]]:
    """Pesan berisi satu gambar, sesuai bentuk yang dipahami OpenRouter."""
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _INSTRUKSI},
                {
                    "type": "image_url",
                    "image_url": {"url": _data_uri(png)},
                },
            ],
        }
    ]


def _data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
