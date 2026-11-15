"""OCR untuk halaman PDF yang tidak memiliki lapisan teks.

Tesseract bersifat memblokir CPU. Menjalankannya langsung di dalam handler async
akan menghentikan seluruh event loop — termasuk permintaan lain yang sedang
diproses dan heartbeat job. Karena itu semua pekerjaan berat di sini dijalankan
lewat `asyncio.to_thread`.

Satu hal yang sering membuat OCR gagal tanpa alasan yang jelas: berkas tesseract
tidak ada di PATH. Sejak versi 5, pemasangan lewat winget di Windows menaruh
semuanya di folder pengguna (`%LOCALAPPDATA%\\Programs\\Tesseract-OCR`), dan
folder itu tidak pernah ditambahkan ke PATH. Karena itu modul ini mencari sendiri
di lokasi yang lazim sebelum menyerah, sehingga OCR bisa dipakai tanpa mengubah
konfigurasi apa pun.

Kalau berkasnya memang tidak ada, atau paket bahasanya kurang, kegagalannya
disebut apa adanya. Pesan "berkas tidak menghasilkan teks" tidak memberi tahu
apa pun kepada orang yang memasang layanan ini.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pymupdf  # PyMuPDF
import pytesseract
from PIL import Image

from app.core.config import settings
from app.core.errors import OcrUnavailable
from app.observability.logger import get_logger

logger = get_logger(__name__)

# Bahasa yang dipakai membaca dokumen sekolah di Indonesia. Data bahasa Inggris
# tetap ikut karena istilah teknis sering muncul dalam bahasa Inggris.
_OCR_BAHASA = ("ind", "eng")
_OCR_LANG = "+".join(_OCR_BAHASA)
_RENDER_DPI = 300


@dataclass(frozen=True)
class OcrInfo:
    """Keadaan mesin OCR di server ini."""

    biner: Path | None = None
    versi: str | None = None
    bahasa: tuple[str, ...] = ()
    masalah: str | None = None

    @property
    def siap(self) -> bool:
        return self.biner is not None and self.masalah is None

    def ringkas(self) -> str:
        """Satu baris untuk log dan untuk panel pemakaian."""
        if self.biner is None:
            return "tidak ditemukan"

        versi = self.versi or "versi tidak diketahui"
        dipakai = ", ".join(b for b in _OCR_BAHASA if b in self.bahasa)

        if self.masalah:
            return f"{versi} di {self.biner} — {self.masalah}"

        return f"{versi} di {self.biner}, bahasa {dipakai}"


def _cari_biner() -> Path | None:
    """Berkas tesseract yang akan dipakai, atau `None` bila tidak ditemukan.

    `TESSERACT_CMD` dihormati lebih dulu. Bila diisi tetapi berkasnya tidak ada,
    pencarian tetap dilanjutkan dan ketidakcocokan itu dicatat di log: nilai
    konfigurasi yang salah tidak boleh membuat OCR mati padahal mesinnya ada.
    """
    if settings.tesseract_cmd:
        jalur = Path(settings.tesseract_cmd)
        if jalur.exists():
            return jalur
        logger.warning(
            "PATH_AI TESSERACT_CMD menunjuk berkas yang tidak ada, mencari ulang",
            dikonfigurasi=str(jalur),
        )

    di_path = shutil.which("tesseract")
    if di_path:
        return Path(di_path)

    for jalur in _lokasi_lazim():
        if jalur.exists():
            return jalur

    return None


def _lokasi_lazim() -> list[Path]:
    """Lokasi pemasangan Tesseract yang lazim, berurutan.

    Di Linux hampir selalu sudah ada di PATH, sehingga daftar ini praktis hanya
    berguna di Windows.
    """
    kandidat: list[Path] = []

    lokal = os.environ.get("LOCALAPPDATA")
    if lokal:
        kandidat.append(Path(lokal) / "Programs" / "Tesseract-OCR" / "tesseract.exe")
        kandidat.append(Path(lokal) / "Tesseract-OCR" / "tesseract.exe")

    # Nama variabelnya huruf besar, sesuai kebiasaan Windows. Pencarian di
    # `os.environ` tetap bekerja walau hurufnya berbeda.
    program_files = os.environ.get("PROGRAMFILES")
    if program_files:
        kandidat.append(Path(program_files) / "Tesseract-OCR" / "tesseract.exe")

    program_files_x86 = os.environ.get("PROGRAMFILES(X86)")
    if program_files_x86:
        kandidat.append(Path(program_files_x86) / "Tesseract-OCR" / "tesseract.exe")

    # Lokasi khas Linux dan macOS, untuk pemasangan yang tidak masuk PATH.
    kandidat.append(Path("/usr/bin/tesseract"))
    kandidat.append(Path("/usr/local/bin/tesseract"))
    kandidat.append(Path("/opt/homebrew/bin/tesseract"))

    return kandidat


@lru_cache(maxsize=1)
def info_ocr() -> OcrInfo:
    """Keadaan mesin OCR. Diperiksa sekali saja, lalu diingat.

    Pemeriksaan ini memanggil berkas tesseract, jadi harganya tidak nol. Karena
    itu hasilnya disimpan: keadaan OCR tidak berubah selama proses berjalan.
    """
    biner = _cari_biner()

    if biner is None:
        return OcrInfo(masalah="berkas tesseract tidak ditemukan")

    pytesseract.pytesseract.tesseract_cmd = str(biner)

    try:
        versi = str(pytesseract.get_tesseract_version())
        bahasa = tuple(sorted(pytesseract.get_languages(config="")))
    except Exception as exc:  # pemasangan yang rusak, bukan berkas yang salah
        return OcrInfo(biner=biner, masalah=str(exc)[:160])

    kurang = [nama for nama in _OCR_BAHASA if nama not in bahasa]
    if kurang:
        return OcrInfo(
            biner=biner,
            versi=versi,
            bahasa=bahasa,
            masalah=(
                "data bahasa belum ada: "
                + ", ".join(kurang)
                + ". Pasang paket bahasanya, misalnya tesseract-ocr-ind."
            ),
        )

    return OcrInfo(biner=biner, versi=versi, bahasa=bahasa)


def bersihkan_cache_info() -> None:
    """Lupakan hasil pemeriksaan. Dipakai oleh test dan oleh pengaturan ulang."""
    info_ocr.cache_clear()


def cetak_keadaan() -> None:
    """Cetak keadaan OCR dalam satu baris siap dibaca skrip.

    Bentuknya `siap|keterangan` atau `belum|keterangan`. Dipakai oleh start.ps1
    dan start.sh.

    Sengaja dipanggil lewat `python -m app.ingest.ocr`, bukan `python -c "..."`.
    Di PowerShell, tanda kutip di dalam argumen `-c` ikut terpotong, sehingga
    kode Python yang dikirim rusak. Perintah modul tidak punya masalah itu, dan
    keterangan OCR tetap berasal dari satu tempat saja.
    """
    info = info_ocr()
    print(f"{'siap' if info.siap else 'belum'}|{info.ringkas()}")


def _pastikan_siap() -> OcrInfo:
    """Hentikan dengan pesan yang menyebut penyebabnya, bukan pesan samar."""
    info = info_ocr()

    if info.biner is None:
        raise OcrUnavailable(
            "Mesin OCR (tesseract) tidak ditemukan di server, sehingga halaman "
            "hasil pindai tidak dapat dibaca. Pasang tesseract, atau isi "
            "PATH_AI_MODEL_VISION supaya halaman bergambar dibaca memakai model "
            "penglihatan."
        )

    if info.masalah:
        raise OcrUnavailable(f"Mesin OCR tidak dapat dipakai: {info.masalah}")

    return info


def _ocr_pages_sync(pdf_path: Path, page_numbers: list[int], dpi: int) -> dict[int, str]:
    results: dict[int, str] = {}
    with pymupdf.open(pdf_path) as document:
        for page_number in page_numbers:
            if page_number < 0 or page_number >= document.page_count:
                continue
            page = document.load_page(page_number)
            pixmap = page.get_pixmap(dpi=dpi)
            image = Image.open(io.BytesIO(pixmap.tobytes("png")))
            try:
                results[page_number] = pytesseract.image_to_string(
                    image, lang=_OCR_LANG
                )
            except pytesseract.TesseractNotFoundError as exc:
                raise OcrUnavailable(
                    "Mesin OCR (tesseract) tidak ditemukan di server."
                ) from exc
            except pytesseract.TesseractError as exc:
                logger.warning(
                    "OCR gagal pada satu halaman",
                    page=page_number,
                    error=str(exc)[:160],
                )
                results[page_number] = ""
    return results


async def ocr_pdf_pages(
    pdf_path: Path, page_numbers: list[int], *, dpi: int = _RENDER_DPI
) -> dict[int, str]:
    """OCR halaman terpilih. Mengembalikan {nomor_halaman: teks}."""
    if not page_numbers:
        return {}

    # Pemeriksaan keadaan ikut dijalankan di thread: isinya memanggil berkas
    # tesseract, dan itu pekerjaan CPU.
    await asyncio.to_thread(_pastikan_siap)

    try:
        return await asyncio.to_thread(_ocr_pages_sync, pdf_path, page_numbers, dpi)
    except OcrUnavailable:
        raise
    except Exception as exc:  # OCR tidak boleh mematikan batch
        logger.warning("OCR halaman gagal seluruhnya", error=str(exc)[:200])
        return {}


if __name__ == "__main__":  # pragma: no cover - dipakai skrip start
    cetak_keadaan()
