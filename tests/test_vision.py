"""Test pembacaan halaman bergambar.

Halaman yang berupa gambar bisa dibaca dua cara: OCR lokal (tesseract) atau
model penglihatan. Yang diuji di sini bukan mutu keduanya, melainkan perilaku di
sekitarnya:

- urutan mana yang dipakai lebih dulu, dan kapan yang satu beralih ke yang lain;
- halaman yang gagal dibaca harus ditandai, bukan dihilangkan diam-diam;
- anggaran yang habis harus menghentikan pembacaan, bukan menghasilkan teks
  setengah jadi tanpa keterangan.
"""

from __future__ import annotations

import io
from pathlib import Path

import pymupdf
import pytest
from app.core.config import settings
from app.core.errors import BudgetExhausted, OcrUnavailable
from app.core.llm import llm
from app.flows import answer_keys, grading
from app.ingest.batch import JawabanMasuk
from app.ingest.documents import extract_document
from app.ingest.vision import buat_pembaca
from PIL import Image as PilImage

OWNER = "guru:ani@sekolah.id"

# Nama model penglihatan dipakai apa adanya; tidak ada panggilan sungguhan yang
# terjadi, karena `llm.read_image` selalu diganti model tiruan.
MODEL = "google/gemini-3.5-flash-lite"


def _png() -> bytes:
    buffer = io.BytesIO()
    PilImage.new("RGB", (300, 400), (235, 235, 225)).save(buffer, format="PNG")
    return buffer.getvalue()


def _pdf_bergambar(jumlah_halaman: int, tmp_path: Path) -> Path:
    """PDF tanpa lapisan teks, seperti hasil pindai atau foto halaman."""
    dokumen = pymupdf.open()

    for _ in range(jumlah_halaman):
        halaman = dokumen.new_page()
        halaman.insert_image(pymupdf.Rect(40, 40, 340, 440), stream=_png())

    berkas = tmp_path / "pindai.pdf"
    berkas.write_bytes(dokumen.tobytes())
    dokumen.close()
    return berkas


class _Berhenti(Exception):
    """Penanda buatan: menghentikan alur tepat setelah tahap pembacaan."""


def _pakai_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Atur pembacaan halaman bergambar supaya memakai model penglihatan dulu."""
    monkeypatch.setattr(settings, "vision_model", MODEL)
    monkeypatch.setattr(settings, "image_reader_order", "model")


# --------------------------------------------------------------------------- #
# Memilih mesin
# --------------------------------------------------------------------------- #


def test_tanpa_model_penglihatan_pembaca_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tanpa model penglihatan, alurnya memakai OCR lokal seperti sebelumnya."""
    monkeypatch.setattr(settings, "vision_model", "")

    assert buat_pembaca(OWNER) is None


def test_pembaca_ada_bila_model_diatur(monkeypatch: pytest.MonkeyPatch) -> None:
    _pakai_model(monkeypatch)

    assert buat_pembaca(OWNER) is not None


async def test_urutan_local_memakai_ocr_lokal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bawaan: OCR lokal lebih dulu, dan model tidak dipanggil sama sekali."""
    from app.ingest import vision

    monkeypatch.setattr(settings, "vision_model", MODEL)
    monkeypatch.setattr(settings, "image_reader_order", "local")
    berkas = _pdf_bergambar(2, tmp_path)
    dipanggil: list[str] = []

    async def ocr_palsu(path, halaman, **kwargs):
        dipanggil.append("ocr")
        return {nomor: f"teks lokal halaman {nomor + 1}" for nomor in halaman}

    async def model_palsu(**kwargs) -> str:
        dipanggil.append("model")
        return "teks dari model"

    monkeypatch.setattr(vision, "ocr_pdf_pages", ocr_palsu)
    monkeypatch.setattr(llm, "read_image", model_palsu)

    hasil = await extract_document(berkas, vision_reader=buat_pembaca(OWNER))

    assert dipanggil == ["ocr"]
    assert "teks lokal halaman 1" in hasil.text
    assert "teks dari model" not in hasil.text


async def test_urutan_local_beralih_ke_model_bila_ocr_tidak_ada(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OCR lokal yang tidak tersedia tidak boleh menggagalkan dokumen."""
    from app.ingest import vision

    monkeypatch.setattr(settings, "vision_model", MODEL)
    monkeypatch.setattr(settings, "image_reader_order", "local")
    berkas = _pdf_bergambar(2, tmp_path)

    async def ocr_tidak_ada(path, halaman, **kwargs):
        raise OcrUnavailable("tesseract tidak ada")

    async def model_palsu(**kwargs) -> str:
        return "teks dari model"

    monkeypatch.setattr(vision, "ocr_pdf_pages", ocr_tidak_ada)
    monkeypatch.setattr(llm, "read_image", model_palsu)

    hasil = await extract_document(berkas, vision_reader=buat_pembaca(OWNER))

    assert hasil.text.count("teks dari model") == 2


# --------------------------------------------------------------------------- #
# Membaca lewat model penglihatan
# --------------------------------------------------------------------------- #


async def test_seluruh_halaman_dibaca_dan_digabung(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pakai_model(monkeypatch)
    berkas = _pdf_bergambar(3, tmp_path)
    nomor: list[int] = []

    async def palsu(*, task, messages, owner, model, **kwargs) -> str:
        nomor.append(len(nomor) + 1)
        assert owner == OWNER, "pemilik harus ikut, supaya token tercatat"
        assert "image_url" in str(messages), "gambar harus ikut terkirim"
        return f"teks halaman ke-{len(nomor)}"

    monkeypatch.setattr(llm, "read_image", palsu)

    hasil = await extract_document(berkas, vision_reader=buat_pembaca(OWNER))

    assert len(nomor) == 3
    assert "teks halaman ke-1" in hasil.text
    assert "teks halaman ke-3" in hasil.text
    assert "[halaman" not in hasil.text


async def test_halaman_yang_gagal_ditandai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Halaman gagal tidak dihilangkan, tetapi ditandai di dalam teksnya."""
    _pakai_model(monkeypatch)
    berkas = _pdf_bergambar(3, tmp_path)
    nomor: list[int] = []

    async def palsu(*, task, messages, owner, model, **kwargs) -> str:
        nomor.append(len(nomor) + 1)
        if len(nomor) == 2:
            raise RuntimeError("model sedang tidak tersedia")
        return f"teks halaman ke-{len(nomor)}"

    monkeypatch.setattr(llm, "read_image", palsu)

    hasil = await extract_document(berkas, vision_reader=buat_pembaca(OWNER))

    assert "teks halaman ke-1" in hasil.text
    assert "teks halaman ke-3" in hasil.text
    assert "[halaman 2: teks tidak dapat dibaca dari gambar]" in hasil.text


async def test_anggaran_habis_menghentikan_pembacaan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anggaran habis bukan alasan menghasilkan teks tanpa keterangan."""
    _pakai_model(monkeypatch)
    berkas = _pdf_bergambar(3, tmp_path)

    async def palsu(*, task, messages, owner, model, **kwargs) -> str:
        raise BudgetExhausted()

    monkeypatch.setattr(llm, "read_image", palsu)

    hasil = await extract_document(berkas, vision_reader=buat_pembaca(OWNER))

    assert hasil.text.count("anggaran token habis") == 3
    assert "tidak dapat dibaca" not in hasil.text


async def test_halaman_tanpa_teks_ditandai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gambar yang tidak memuat teks tetap disebut, bukan dibiarkan kosong."""
    _pakai_model(monkeypatch)
    berkas = _pdf_bergambar(1, tmp_path)

    async def palsu(*, task, messages, owner, model, **kwargs) -> str:
        return "   "

    monkeypatch.setattr(llm, "read_image", palsu)

    hasil = await extract_document(berkas, vision_reader=buat_pembaca(OWNER))

    assert "[halaman 1: halaman kosong]" in hasil.text


# --------------------------------------------------------------------------- #
# Penyambungan di fitur lain
# --------------------------------------------------------------------------- #


async def test_kunci_jawaban_pindai_memakai_pembaca_gambar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bukan hanya fitur dokumen: kunci jawaban hasil pindai juga ikut terbaca."""
    from app.domain.models import ExtractedDocument

    _pakai_model(monkeypatch)
    tercatat: dict = {}

    async def extract_palsu(path, **kwargs) -> ExtractedDocument:
        tercatat.update(kwargs)
        return ExtractedDocument(text="1. Apa fungsi klorofil?", pages=1)

    async def berhenti(*args, **kwargs):
        # Tahap ekstraksi tidak diuji di sini; yang diuji hanya tahap pembacaan.
        raise _Berhenti

    monkeypatch.setattr(answer_keys, "extract_document", extract_palsu)
    monkeypatch.setattr(answer_keys.llm, "structured", berhenti)

    with pytest.raises(_Berhenti):
        await answer_keys.extract_key(tmp_path / "kunci.pdf", owner=OWNER)

    assert tercatat.get("vision_reader") is not None


async def test_berkas_jawaban_pindai_memakai_pembaca_gambar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lembar jawaban siswa yang difoto juga dibaca lewat jalur yang sama."""
    from app.jobs import job_store

    _pakai_model(monkeypatch)
    tercatat: dict = {}

    async def ingest_palsu(*args, **kwargs):
        tercatat.update(kwargs)
        return None

    monkeypatch.setattr(grading, "muat_jawaban", ingest_palsu)

    job = await job_store.create(kind="penilaian", owner=OWNER)
    await grading._read_batch(job, JawabanMasuk(zip_path=Path("jawaban.zip")))

    assert tercatat.get("vision_reader") is not None
