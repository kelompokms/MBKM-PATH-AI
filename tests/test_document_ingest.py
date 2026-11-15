"""Test pembacaan berkas menjadi teks.

Yang dijaga di sini adalah kejujuran cakupan. Berkas yang diunggah guru
bermacam-macam: PDF berlapis teks, PDF hasil pindai, foto halaman buku, DOCX,
dan TXT. Berkas hasil pindai dibaca lewat pengenalan gambar, dan jumlah
halamannya dibatasi. Kalau halaman yang dilewati tidak dicatat, laporan akan
terlihat membahas seluruh dokumen padahal sebagian halamannya tidak pernah
dibaca.
"""

from __future__ import annotations

import io
from pathlib import Path

import docx
import pymupdf
import pytest
from app.core.errors import UnsupportedFormat
from app.ingest.documents import extract_document
from PIL import Image as PilImage


def _png() -> bytes:
    buffer = io.BytesIO()
    PilImage.new("RGB", (240, 240), (60, 90, 130)).save(buffer, format="PNG")
    return buffer.getvalue()


def _pdf_bergambar(jumlah_halaman: int) -> bytes:
    """PDF tanpa lapisan teks, seperti hasil pindai atau foto halaman."""
    dokumen = pymupdf.open()

    for _ in range(jumlah_halaman):
        halaman = dokumen.new_page()
        halaman.insert_image(pymupdf.Rect(40, 40, 280, 280), stream=_png())

    data = dokumen.tobytes()
    dokumen.close()
    return data


def _pdf_berteks(jumlah_halaman: int) -> bytes:
    dokumen = pymupdf.open()

    for nomor in range(jumlah_halaman):
        halaman = dokumen.new_page()
        halaman.insert_text(
            (60, 80),
            f"Halaman {nomor + 1}. Metode deteksi serangan siber dibahas di sini.",
        )

    data = dokumen.tobytes()
    dokumen.close()
    return data


async def test_pdf_berteks_dibaca_tanpa_pengenalan_gambar(tmp_path: Path) -> None:
    berkas = tmp_path / "artikel.pdf"
    berkas.write_bytes(_pdf_berteks(4))

    hasil = await extract_document(berkas)

    assert hasil.pages == 4
    assert hasil.ocr_pages == 0
    assert hasil.ocr_skipped == 0
    assert "Metode deteksi serangan siber" in hasil.text


async def test_halaman_bergambar_dibaca_lewat_pengenalan_gambar(
    tmp_path: Path,
) -> None:
    berkas = tmp_path / "pindai.pdf"
    berkas.write_bytes(_pdf_bergambar(3))

    halaman_diminta: list[int] = []

    async def pembaca(path: Path, halaman: list[int]) -> str:
        halaman_diminta.extend(halaman)
        return "isi halaman hasil pengenalan gambar"

    hasil = await extract_document(berkas, vision_reader=pembaca)

    assert hasil.pages == 3
    assert hasil.ocr_pages == 3
    assert hasil.ocr_skipped == 0
    assert halaman_diminta == [0, 1, 2]
    assert "hasil pengenalan gambar" in hasil.text


async def test_halaman_yang_dilewati_karena_batas_dicatat(tmp_path: Path) -> None:
    """Batas pengenalan gambar tidak boleh menyembunyikan halaman yang dilewati."""
    berkas = tmp_path / "pindai-panjang.pdf"
    berkas.write_bytes(_pdf_bergambar(5))

    async def pembaca(path: Path, halaman: list[int]) -> str:
        return "teks hasil pengenalan gambar"

    hasil = await extract_document(berkas, max_ocr_pages=2, vision_reader=pembaca)

    assert hasil.pages == 5
    assert hasil.ocr_pages == 2
    assert hasil.ocr_skipped == 3


async def test_docx_dibaca_berikut_tabelnya(tmp_path: Path) -> None:
    dokumen = docx.Document()
    dokumen.add_paragraph("Pembahasan metode deteksi serangan siber.")
    tabel = dokumen.add_table(rows=1, cols=2)
    tabel.rows[0].cells[0].text = "Metode"
    tabel.rows[0].cells[1].text = "Akurasi"

    berkas = tmp_path / "catatan.docx"
    dokumen.save(str(berkas))

    hasil = await extract_document(berkas)

    assert "Pembahasan metode deteksi" in hasil.text
    assert "Metode | Akurasi" in hasil.text


async def test_berkas_txt_dibaca_apa_adanya(tmp_path: Path) -> None:
    berkas = tmp_path / "catatan.txt"
    berkas.write_text("Catatan pertama.\nCatatan kedua.\n", encoding="utf-8")

    hasil = await extract_document(berkas)

    assert "Catatan pertama." in hasil.text
    assert hasil.pages == 1


async def test_format_doc_lama_ditolak_dengan_pesan_jelas(tmp_path: Path) -> None:
    """`.doc` lama tidak bisa dibaca, dan pesannya harus menyebut jalan keluar."""
    berkas = tmp_path / "lama.doc"
    berkas.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")

    with pytest.raises(UnsupportedFormat) as galat:
        await extract_document(berkas)

    assert "docx" in galat.value.message.lower()
