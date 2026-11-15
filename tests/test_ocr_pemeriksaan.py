"""Test pencarian dan pemeriksaan mesin OCR.

Dua hal yang dijaga di sini.

Pertama, berkas tesseract harus ditemukan walau tidak ada di PATH. Pemasangan
lewat winget di Windows menaruh semuanya di folder pengguna, dan folder itu tidak
pernah masuk PATH. Kalau pencarian hanya percaya PATH, OCR mati padahal mesinnya
ada, dan orang akan mengira perlu memasang ulang.

Kedua, ketika OCR memang tidak bisa dipakai, sebabnya harus disebut apa adanya.
Pesan "berkas tidak menghasilkan teks" tidak menolong siapa pun.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.core.config import settings
from app.core.errors import OcrUnavailable
from app.ingest import ocr


@pytest.fixture(autouse=True)
def _bersihkan_pemeriksaan() -> None:
    """Hasil pemeriksaan diingat, jadi harus dilupakan antar test."""
    ocr.bersihkan_cache_info()
    yield
    ocr.bersihkan_cache_info()


def _berkas_palsu(tmp_path: Path, nama: str = "tesseract.exe") -> Path:
    """Berkas kosong sebagai pengganti tesseract; isinya tidak pernah dibaca."""
    berkas = tmp_path / nama
    berkas.parent.mkdir(parents=True, exist_ok=True)
    berkas.write_bytes(b"bukan sungguhan")
    return berkas


def test_tesseract_cmd_dihormati(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nilai konfigurasi dipakai lebih dulu, tanpa mencari yang lain."""
    berkas = _berkas_palsu(tmp_path)
    monkeypatch.setattr(settings, "tesseract_cmd", str(berkas))

    assert ocr._cari_biner() == berkas


def test_tesseract_cmd_salah_tetap_dicari_ulang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Salah tulis jalur tidak boleh mematikan OCR padahal mesinnya ada."""
    berkas = _berkas_palsu(tmp_path)
    monkeypatch.setattr(settings, "tesseract_cmd", str(tmp_path / "tidak-ada.exe"))
    monkeypatch.setattr(ocr.shutil, "which", lambda nama: str(berkas))

    assert ocr._cari_biner() == berkas


def test_dicari_di_lokasi_lazim_bila_tidak_ada_di_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kasus pemasangan winget: ada di folder pengguna, tidak ada di PATH."""
    berkas = _berkas_palsu(tmp_path / "Tesseract-OCR")
    monkeypatch.setattr(settings, "tesseract_cmd", "")
    monkeypatch.setattr(ocr.shutil, "which", lambda nama: None)
    monkeypatch.setattr(ocr, "_lokasi_lazim", lambda: [berkas])

    assert ocr._cari_biner() == berkas


def test_tidak_ditemukan_mengembalikan_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tesseract_cmd", "")
    monkeypatch.setattr(ocr.shutil, "which", lambda nama: None)
    monkeypatch.setattr(ocr, "_lokasi_lazim", lambda: [])

    assert ocr._cari_biner() is None


def test_keadaan_saat_biner_tidak_ada(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "_cari_biner", lambda: None)

    info = ocr.info_ocr()

    assert info.siap is False
    assert info.biner is None
    assert info.ringkas() == "tidak ditemukan"


def test_keadaan_saat_bahasa_kurang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Data bahasa yang kurang harus disebut, bukan dianggap berkas rusak."""
    berkas = _berkas_palsu(tmp_path)
    monkeypatch.setattr(ocr, "_cari_biner", lambda: berkas)
    monkeypatch.setattr(ocr.pytesseract, "get_tesseract_version", lambda: "5.5.0")
    monkeypatch.setattr(
        ocr.pytesseract, "get_languages", lambda config="": ["eng", "osd"]
    )

    info = ocr.info_ocr()

    assert info.siap is False
    assert "ind" in (info.masalah or "")
    assert "eng" not in (info.masalah or "")


def test_keadaan_siap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    berkas = _berkas_palsu(tmp_path)
    monkeypatch.setattr(ocr, "_cari_biner", lambda: berkas)
    monkeypatch.setattr(ocr.pytesseract, "get_tesseract_version", lambda: "5.5.0")
    monkeypatch.setattr(
        ocr.pytesseract, "get_languages", lambda config="": ["eng", "ind"]
    )

    info = ocr.info_ocr()

    assert info.siap is True
    assert "bahasa ind, eng" in info.ringkas()


def test_pesan_saat_tidak_ditemukan_menyebut_jalan_keluar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pesannya harus memberi tahu apa yang bisa dilakukan pembaca log."""
    monkeypatch.setattr(ocr, "_cari_biner", lambda: None)

    with pytest.raises(OcrUnavailable) as galat:
        ocr._pastikan_siap()

    pesan = galat.value.message
    assert "tesseract" in pesan
    assert "PATH_AI_MODEL_VISION" in pesan


def test_pesan_saat_bahasa_kurang_menyebut_bahasa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    berkas = _berkas_palsu(tmp_path)
    monkeypatch.setattr(ocr, "_cari_biner", lambda: berkas)
    monkeypatch.setattr(ocr.pytesseract, "get_tesseract_version", lambda: "5.5.0")
    monkeypatch.setattr(ocr.pytesseract, "get_languages", lambda config="": ["eng"])

    with pytest.raises(OcrUnavailable) as galat:
        ocr._pastikan_siap()

    assert "ind" in galat.value.message


def test_cetak_keadaan_dipakai_skrip_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Format satu baris ini yang dibaca oleh start.ps1 dan start.sh."""
    monkeypatch.setattr(ocr, "_cari_biner", lambda: None)

    ocr.cetak_keadaan()

    assert capsys.readouterr().out.strip() == "belum|tidak ditemukan"
