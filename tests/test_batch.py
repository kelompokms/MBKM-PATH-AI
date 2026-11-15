"""Test batch ingest: satu berkas bermasalah tidak boleh mematikan batch."""

from __future__ import annotations

import zipfile
from pathlib import Path

import docx
import pytest
from app.core.config import settings
from app.core.errors import ArchiveInvalid, InvalidManifest
from app.ingest.archive import inspect_zip
from app.ingest.batch import ingest_answers
from app.ingest.documents import extract_document


def _build_zip(path: Path, files: dict[str, bytes], manifest: str) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.csv", manifest.encode())
        for name, payload in files.items():
            archive.writestr(name, payload)
    return path


MANIFEST = (
    "email,nama,nama_file\n"
    "ani@sekolah.id,Ani,001.txt\n"
    "budi@sekolah.id,Budi,002.txt\n"
    "citra@sekolah.id,Citra,003.txt\n"
)


async def test_batch_terbaca_menjadi_teks(tmp_path: Path) -> None:
    zip_path = _build_zip(
        tmp_path / "jawaban.zip",
        {
            "001.txt": b"Jawaban Ani tentang fotosintesis.",
            "002.txt": b"Jawaban Budi tentang fotosintesis.",
            "003.txt": b"Jawaban Citra tentang fotosintesis.",
        },
        MANIFEST,
    )

    batch = await ingest_answers(zip_path, workdir=tmp_path / "kerja")

    assert len(batch.students) == 3
    assert len(batch.graded) == 3
    assert batch.students[0].nama == "Ani"
    assert "fotosintesis" in batch.students[0].text
    assert batch.students[0].tokens_estimated > 0


async def test_satu_berkas_besar_tidak_menggagalkan_sisanya(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "max_tokens_per_student", 50)

    zip_path = _build_zip(
        tmp_path / "campur.zip",
        {
            "001.txt": b"pendek",
            "002.txt": ("panjang " * 400).encode(),
            "003.txt": b"juga pendek",
        },
        MANIFEST,
    )

    batch = await ingest_answers(zip_path, workdir=tmp_path / "kerja")

    assert len(batch.graded) == 2
    assert len(batch.skipped) == 1
    assert batch.skipped[0].nama == "Budi"
    assert "melebihi batas" in (batch.skipped[0].skipped_reason or "")


async def test_berkas_format_lama_dilewati_bukan_mematikan_batch(
    tmp_path: Path,
) -> None:
    manifest = (
        "email,nama,nama_file\n"
        "ani@sekolah.id,Ani,001.txt\n"
        "budi@sekolah.id,Budi,002.doc\n"
        "citra@sekolah.id,Citra,003.txt\n"
    )
    zip_path = _build_zip(
        tmp_path / "doc.zip",
        {
            "001.txt": b"jawaban ani",
            "002.doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
            "003.txt": b"jawaban citra",
        },
        manifest,
    )

    batch = await ingest_answers(zip_path, workdir=tmp_path / "kerja")

    assert len(batch.graded) == 2
    assert "docx" in (batch.skipped[0].skipped_reason or "")


async def test_semua_berkas_gagal_menghasilkan_galat_jelas(tmp_path: Path) -> None:
    zip_path = _build_zip(
        tmp_path / "rusak.zip",
        {
            "001.txt": b"",
            "002.txt": b"   ",
            "003.txt": b"",
        },
        MANIFEST,
    )

    with pytest.raises(InvalidManifest) as info:
        await ingest_answers(zip_path, workdir=tmp_path / "kerja")

    assert "Tidak ada satu pun" in info.value.message


async def test_manifest_menunjuk_berkas_yang_tidak_ada(tmp_path: Path) -> None:
    zip_path = _build_zip(
        tmp_path / "hilang.zip",
        {"001.txt": b"ani"},
        MANIFEST,
    )

    with pytest.raises(InvalidManifest) as info:
        await ingest_answers(zip_path, workdir=tmp_path / "kerja")

    assert len(info.value.details["tidak_ditemukan"]) == 2


async def test_berkas_tak_terpakai_dilaporkan(tmp_path: Path) -> None:
    zip_path = _build_zip(
        tmp_path / "lebih.zip",
        {
            "001.txt": b"ani",
            "002.txt": b"budi",
            "003.txt": b"citra",
            "catatan-lain.txt": b"bukan milik siapa pun",
        },
        MANIFEST,
    )

    batch = await ingest_answers(zip_path, workdir=tmp_path / "kerja")

    assert batch.unused_files == ["catatan-lain.txt"]


async def test_tanpa_manifest_ditolak(tmp_path: Path) -> None:
    zip_path = tmp_path / "tanpa.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("001.txt", b"ani")

    with pytest.raises(InvalidManifest) as info:
        await ingest_answers(zip_path, workdir=tmp_path / "kerja")

    assert "manifest.csv tidak ditemukan" in info.value.message


async def test_docx_terbaca(tmp_path: Path) -> None:
    document = docx.Document()
    document.add_paragraph("Jawaban soal satu.")
    document.add_paragraph("Jawaban soal dua.")
    path = tmp_path / "jawaban.docx"
    document.save(str(path))

    extracted = await extract_document(path)

    assert "Jawaban soal satu." in extracted.text
    assert "Jawaban soal dua." in extracted.text


async def test_ekstensi_asing_ditolak(tmp_path: Path) -> None:
    path = tmp_path / "catatan.md"
    path.write_text("halo")

    with pytest.raises(Exception) as info:
        await extract_document(path)

    assert "tidak didukung" in str(info.value)


def test_zip_kosong_ditolak(tmp_path: Path) -> None:
    path = tmp_path / "kosong.zip"
    with zipfile.ZipFile(path, "w"):
        pass

    with pytest.raises(ArchiveInvalid):
        inspect_zip(path)