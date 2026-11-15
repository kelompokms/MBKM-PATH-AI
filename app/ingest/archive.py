"""Pembacaan ZIP kumpulan jawaban siswa beserta manifest-nya.

Berkas ZIP dari lapangan jarang rapi. Modul ini menangani empat kenyataan
yang paling sering muncul:

1. `manifest.csv` diekspor Excel berbahasa Indonesia → pemisah `;`, BOM UTF-8,
   dan kadang header berspasi. Pemisah dideteksi, bukan diasumsikan.
2. Nama berkas di manifest tidak menyertakan subfolder, padahal isi ZIP
   dikelompokkan per kelas. Pencocokan jatuh ke nama dasar bila unik.
3. ZIP bisa berupa zip-bomb. Ukuran total setelah ekstraksi diperiksa
   SEBELUM satu byte pun ditulis ke disk.
4. Entri bisa menunjuk ke luar direktori kerja (`../`). Hanya entri yang
   lolos pemeriksaan nama yang diekstrak.

Yang tidak ditangani dan sebaiknya tidak: menebak manifest yang rusak.
Bila ada baris yang tidak bisa dibaca, seluruh permintaan ditolak dengan
nomor baris yang jelas. Menebak berarti menilai siswa dengan data yang salah.
"""

from __future__ import annotations

import csv
import io
import shutil
import unicodedata
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.core.config import settings
from app.core.errors import ArchiveInvalid, InvalidManifest
from app.domain.models import ManifestRow
from app.observability.logger import get_logger

logger = get_logger(__name__)

_REQUIRED_COLUMNS = {"email", "nama", "nama_file"}


def _normalize_header(value: str) -> str:
    """`Nama File` / `NAMA_FILE` / ` nama_file ` → `nama_file`."""
    text = unicodedata.normalize("NFKD", value).strip().lower()
    return text.replace(" ", "_").replace("-", "_")


def _delimiter_occurrences(item: tuple[str, int]) -> int:
    """Kunci pembanding: jumlah kemunculan sebuah pemisah di baris header."""
    return item[1]


def _sniff_delimiter(sample: str) -> str:
    """Pilih pemisah dari baris header, bukan dari tebakan csv.Sniffer.

    Sniffer gagal pada file satu kolom dan pada nilai yang mengandung titik
    koma desimal. Menghitung kemunculan di baris header jauh lebih stabil.
    """
    header = sample.splitlines()[0] if sample.splitlines() else ""
    counts = {
        ";": header.count(";"),
        ",": header.count(","),
        "\t": header.count("\t"),
    }
    delimiter, count = max(counts.items(), key=_delimiter_occurrences)
    return delimiter if count > 0 else ","


def _decode_manifest(data: bytes) -> str:
    """Ubah byte manifest menjadi teks, menolak berkas kosong atau rusak.

    Excel berbahasa Indonesia menulis BOM UTF-8, jadi `utf-8-sig` dicoba
    lebih dahulu; cp1252 dipakai sebagai cadangan untuk ekspor lama.
    """
    if not data:
        raise InvalidManifest("Berkas manifest.csv kosong.")

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise InvalidManifest(
                "Berkas manifest.csv tidak dapat dibaca. Simpan ulang sebagai "
                "CSV UTF-8."
            ) from exc

    if not text.strip():
        raise InvalidManifest("Berkas manifest.csv tidak berisi data.")
    return text


def _read_header(reader: Iterator[list[str]]) -> list[str]:
    """Ambil dan normalisasi baris header, menolak header yang tidak lengkap."""
    try:
        raw_header = next(reader)
    except StopIteration as exc:
        raise InvalidManifest(
            "Berkas manifest.csv tidak memiliki baris header."
        ) from exc

    header = [_normalize_header(cell) for cell in raw_header]
    missing = _REQUIRED_COLUMNS - set(header)
    if missing:
        raise InvalidManifest(
            "Header manifest.csv tidak lengkap.",
            details={
                "hilang": sorted(missing),
                "ditemukan": header,
                "wajib": sorted(_REQUIRED_COLUMNS),
            },
        )
    return header


def _pick_cell(cells: list[str], column: dict[str, int], name: str) -> str:
    """Ambil sel berdasarkan nama kolom; kosongkan bila kolom di luar rentang."""
    index = column[name]
    return cells[index].strip() if index < len(cells) else ""


def _build_row(line: int, cells: list[str], column: dict[str, int]) -> ManifestRow:
    """Rangkai satu baris CSV menjadi ManifestRow yang nilainya sudah dibersihkan."""
    return ManifestRow(
        line=line,
        email=_pick_cell(cells, column, "email").lower(),
        nama=_pick_cell(cells, column, "nama"),
        nama_file=_pick_cell(cells, column, "nama_file"),
    )


def _row_problems(
    row: ManifestRow, seen_emails: set[str], seen_files: set[str]
) -> list[str]:
    """Daftar masalah pada satu baris; kosong berarti baris layak dinilai."""
    problems = []
    if not row.email or "@" not in row.email:
        problems.append("email tidak valid")
    if not row.nama:
        problems.append("nama kosong")
    if not row.nama_file:
        problems.append("nama_file kosong")
    if row.email in seen_emails:
        problems.append("email duplikat")
    if row.nama_file in seen_files:
        problems.append("nama_file duplikat")
    return problems


def _collect_rows(
    reader: Iterator[list[str]], column: dict[str, int]
) -> tuple[list[ManifestRow], list[dict[str, object]]]:
    """Ubah sisa baris CSV menjadi ManifestRow, kumpulkan baris bermasalah.

    Baris bermasalah tidak langsung melempar galat supaya pengajar melihat
    semua masalah sekaligus, bukan satu per satu.
    """
    rows: list[ManifestRow] = []
    problems: list[dict[str, object]] = []
    seen_emails: set[str] = set()
    seen_files: set[str] = set()

    for offset, cells in enumerate(reader, start=2):
        if not any(cell.strip() for cell in cells):
            continue  # baris kosong di ujung file

        row = _build_row(offset, cells, column)
        row_problems = _row_problems(row, seen_emails, seen_files)
        if row_problems:
            problems.append({"baris": offset, "masalah": row_problems})
            continue

        seen_emails.add(row.email)
        seen_files.add(row.nama_file)
        rows.append(row)

    return rows, problems


def _raise_if_problems(problems: list[dict[str, object]]) -> None:
    """Hentikan seluruh permintaan bila ada baris yang tidak bisa dibaca."""
    if problems:
        raise InvalidManifest(
            f"Manifest.csv memiliki {len(problems)} baris bermasalah.",
            details={"baris_bermasalah": problems[:20]},
        )


def _check_row_count(rows: list[ManifestRow]) -> None:
    """Pastikan ada siswa dan jumlahnya tidak melebihi batas batch."""
    if not rows:
        raise InvalidManifest("Manifest.csv tidak berisi satu pun baris siswa.")

    if len(rows) > settings.max_students_per_batch:
        raise InvalidManifest(
            f"Jumlah siswa melebihi batas {settings.max_students_per_batch} per batch.",
            details={"jumlah": len(rows), "batas": settings.max_students_per_batch},
        )


def parse_manifest(data: bytes) -> list[ManifestRow]:
    """Baca manifest.csv menjadi daftar baris yang sudah divalidasi."""
    text = _decode_manifest(data)
    delimiter = _sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)

    header = _read_header(reader)
    column = {name: header.index(name) for name in _REQUIRED_COLUMNS}

    rows, problems = _collect_rows(reader, column)

    # Urutan pemeriksaan sengaja seperti ini: masalah per baris lebih dulu,
    # baru jumlah baris, agar pesan galat menunjuk ke penyebab yang paling
    # dekat dengan berkas yang diunggah pengajar.
    _raise_if_problems(problems)
    _check_row_count(rows)
    return rows


@dataclass
class ZipInventory:
    """Isi ZIP yang sudah lolos pemeriksaan keamanan."""

    entries: dict[str, zipfile.ZipInfo]
    total_uncompressed: int


def _is_unsafe_name(name: str) -> bool:
    """Tolak path absolut dan path yang keluar dari direktori kerja."""
    pure = PurePosixPath(name)
    if pure.is_absolute():
        return True
    return any(part == ".." for part in pure.parts)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    # Mode file Unix disimpan di 16 bit atas external_attr.
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _check_entry(info: zipfile.ZipInfo) -> None:
    """Tolak entri berbahaya atau kebesaran sebelum apa pun ditulis ke disk."""
    if _is_unsafe_name(info.filename) or _is_symlink(info):
        raise ArchiveInvalid(
            "ZIP memuat entri dengan nama berkas yang tidak aman.",
            details={"entri": info.filename[:120]},
        )
    if info.file_size > settings.max_file_size_bytes:
        raise ArchiveInvalid(
            "Ada berkas di dalam ZIP yang melebihi batas ukuran per berkas.",
            details={
                "berkas": info.filename[:120],
                "ukuran_mb": round(info.file_size / 1_048_576, 2),
                "batas_mb": settings.max_file_size_mb,
            },
        )


def _survey_entries(
    infos: list[zipfile.ZipInfo],
) -> tuple[dict[str, zipfile.ZipInfo], int]:
    """Kumpulkan entri aman dan jumlahkan ukurannya setelah ekstraksi."""
    entries: dict[str, zipfile.ZipInfo] = {}
    total_uncompressed = 0

    for info in infos:
        if info.is_dir():
            continue
        _check_entry(info)
        total_uncompressed += info.file_size
        entries[info.filename] = info

    return entries, total_uncompressed


def _check_total_size(total_uncompressed: int) -> None:
    """Tolak ZIP yang total isinya setelah ekstraksi melebihi batas."""
    if total_uncompressed > settings.max_zip_uncompressed_bytes:
        raise ArchiveInvalid(
            "Total ukuran isi ZIP setelah ekstraksi melebihi batas.",
            details={
                "total_mb": round(total_uncompressed / 1_048_576, 2),
                "batas_mb": settings.max_zip_uncompressed_mb,
            },
        )


def _check_compression_ratio(zip_path: Path, total_uncompressed: int) -> None:
    """Tolak rasio kompresi tidak wajar sebagai pertahanan terhadap zip-bomb."""
    compressed = max(1, zip_path.stat().st_size)
    ratio = total_uncompressed / compressed
    if ratio > settings.zip_max_ratio:
        raise ArchiveInvalid(
            "Rasio kompresi ZIP tidak wajar dan ditolak sebagai tindakan "
            "pengamanan terhadap zip-bomb.",
            details={
                "rasio": round(ratio, 1),
                "batas_rasio": settings.zip_max_ratio,
            },
        )


def _open_zip(zip_path: Path) -> zipfile.ZipFile:
    """Buka ZIP, menerjemahkan berkas yang rusak menjadi galat domain."""
    try:
        return zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveInvalid(
            "Berkas yang diunggah bukan ZIP yang valid atau rusak."
        ) from exc


def inspect_zip(zip_path: Path) -> ZipInventory:
    """Periksa isi ZIP tanpa mengekstrak apa pun."""
    with _open_zip(zip_path) as archive:
        infos = archive.infolist()

        if not infos:
            raise ArchiveInvalid("ZIP tidak berisi berkas apa pun.")

        entries, total_uncompressed = _survey_entries(infos)
        _check_total_size(total_uncompressed)
        _check_compression_ratio(zip_path, total_uncompressed)

        return ZipInventory(entries=entries, total_uncompressed=total_uncompressed)


def find_manifest(inventory: ZipInventory) -> str | None:
    """Cari manifest.csv di akar ZIP (nama apa pun huruf besar/kecilnya)."""
    for name in inventory.entries:
        if PurePosixPath(name).name.lower() == "manifest.csv":
            return name
    return None


def resolve_entry(inventory: ZipInventory, nama_file: str) -> str | None:
    """Cocokkan nama di manifest dengan entri ZIP.

    Urutan: sama persis → nama dasar unik. Nama dasar yang muncul lebih dari
    sekali sengaja TIDAK dipilih, karena menebak berkas siswa adalah cara
    termudah menilai orang yang salah.
    """
    if nama_file in inventory.entries:
        return nama_file

    wanted = PurePosixPath(nama_file.replace("\\", "/")).name
    matches = [
        entry
        for entry in inventory.entries
        if PurePosixPath(entry).name == wanted
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def extract_entries(
    zip_path: Path, inventory: ZipInventory, names: list[str], dest: Path
) -> dict[str, Path]:
    """Ekstrak hanya entri terpilih ke direktori kerja."""
    dest.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    try:
        with zipfile.ZipFile(zip_path) as archive:
            for name in names:
                info = inventory.entries[name]
                target = dest / PurePosixPath(name).name
                # Hindari tabrakan bila dua subfolder memuat nama sama.
                if target.exists():
                    target = dest / f"{len(written):03d}_{PurePosixPath(name).name}"
                with archive.open(info) as source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink, length=1024 * 256)
                written[name] = target
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveInvalid(
            "Gagal mengekstrak isi ZIP. Berkas mungkin rusak."
        ) from exc

    return written
