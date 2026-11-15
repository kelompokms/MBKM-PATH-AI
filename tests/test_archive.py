"""Test pembacaan manifest dan pertahanan terhadap ZIP berbahaya."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from app.core.errors import ArchiveInvalid, InvalidManifest
from app.ingest.archive import (
    extract_entries,
    find_manifest,
    inspect_zip,
    parse_manifest,
    resolve_entry,
)


def _zip_with(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


def test_manifest_dipisah_koma() -> None:
    data = b"email,nama,nama_file\nani@sekolah.id,Ani,001.txt\nbudi@sekolah.id,Budi,002.txt\n"

    rows = parse_manifest(data)

    assert [r.email for r in rows] == ["ani@sekolah.id", "budi@sekolah.id"]
    assert rows[0].nama == "Ani"
    assert rows[0].nama_file == "001.txt"
    assert rows[1].line == 3


def test_manifest_ekspor_excel_titik_koma_dan_bom() -> None:
    """Excel berbahasa Indonesia: BOM UTF-8, pemisah `;`, header berspasi."""
    data = (
        "\ufeffEmail;Nama;Nama File\n"
        "ani@sekolah.id;Ani;001.txt\n"
        "budi@sekolah.id;Budi;002.txt\n"
    ).encode("utf-8")

    rows = parse_manifest(data)

    assert len(rows) == 2
    assert rows[0].nama_file == "001.txt"
    assert rows[0].nama == "Ani"


def test_manifest_email_duplikat_ditolak() -> None:
    data = (
        b"email,nama,nama_file\n"
        b"ani@sekolah.id,Ani,001.txt\n"
        b"ani@sekolah.id,Ani Lain,002.txt\n"
    )

    with pytest.raises(InvalidManifest) as info:
        parse_manifest(data)

    assert "baris_bermasalah" in info.value.details
    assert info.value.details["baris_bermasalah"][0]["baris"] == 3


def test_manifest_header_kurang_ditolak() -> None:
    with pytest.raises(InvalidManifest) as info:
        parse_manifest(b"email,nama\nani@sekolah.id,Ani\n")

    assert info.value.details["hilang"] == ["nama_file"]


def test_manifest_kosong_ditolak() -> None:
    with pytest.raises(InvalidManifest):
        parse_manifest(b"")


def test_zip_bomb_ditolak_sebelum_ekstraksi(tmp_path: Path) -> None:
    # 20 MB data nol terkompresi sangat kecil → rasio jauh di atas batas.
    payload = b"\0" * (20 * 1024 * 1024)
    zip_path = _zip_with(tmp_path / "bomb.zip", {"besar.txt": payload})

    with pytest.raises(ArchiveInvalid) as info:
        inspect_zip(zip_path)

    assert info.value.code == "archive_invalid"
    assert "zip-bomb" in info.value.message


def test_entri_path_traversal_ditolak(tmp_path: Path) -> None:
    zip_path = _zip_with(tmp_path / "jahat.zip", {"../keluar.txt": b"x"})

    with pytest.raises(ArchiveInvalid):
        inspect_zip(zip_path)


def test_bukan_zip_ditolak(tmp_path: Path) -> None:
    fake = tmp_path / "bukan.zip"
    fake.write_bytes(b"ini bukan zip")

    with pytest.raises(ArchiveInvalid):
        inspect_zip(fake)


def test_berkas_tidak_dikenal_manifest_dan_nama_dasar(tmp_path: Path) -> None:
    zip_path = _zip_with(
        tmp_path / "kelas.zip",
        {
            "kelas-a/001.txt": b"jawaban ani",
            "kelas-a/002.txt": b"jawaban budi",
            "manifest.csv": b"email,nama,nama_file\n",
        },
    )
    inventory = inspect_zip(zip_path)

    assert find_manifest(inventory) == "manifest.csv"
    # Manifest menulis nama dasar, ZIP menyimpan subfolder.
    assert resolve_entry(inventory, "001.txt") == "kelas-a/001.txt"


def test_nama_dasar_ambigu_tidak_ditebak(tmp_path: Path) -> None:
    zip_path = _zip_with(
        tmp_path / "ambigu.zip",
        {"a/001.txt": b"a", "b/001.txt": b"b"},
    )
    inventory = inspect_zip(zip_path)

    assert resolve_entry(inventory, "001.txt") is None


def test_ekstraksi_hanya_entri_terpilih(tmp_path: Path) -> None:
    zip_path = _zip_with(
        tmp_path / "pilih.zip",
        {"001.txt": b"ani", "002.txt": b"budi", "lain.txt": b"x"},
    )
    inventory = inspect_zip(zip_path)
    written = extract_entries(zip_path, inventory, ["001.txt"], tmp_path / "out")

    assert set(written) == {"001.txt"}
    assert (tmp_path / "out" / "001.txt").read_bytes() == b"ani"
    assert not (tmp_path / "out" / "lain.txt").exists()