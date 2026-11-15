"""Menyusun satu batch jawaban siswa menjadi teks siap nilai.

Ada dua cara jawaban masuk, dan keduanya berakhir pada bentuk yang sama:

1. **Berkas per siswa.** Aplikasi pengajar mengirim satu berkas untuk setiap
   siswa, beserta email dan namanya. Ini jalur utama.
2. **Satu ZIP berisi manifest.** Dipakai bila satu kelas ingin dikirim sekaligus.

Prinsip yang dipegang di sini: satu berkas bermasalah tidak boleh menggagalkan
99 berkas lain. Siswa dengan berkas bermasalah dikeluarkan dari penilaian dengan
alasan yang bisa dibaca, lalu dilaporkan terpisah. Yang tidak boleh terjadi
adalah seluruh batch ditolak karena satu berkas berformat salah, atau — lebih
buruk — siswa itu tetap dinilai dengan teks kosong.
"""

from __future__ import annotations

import asyncio
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import settings
from app.core.errors import AppError, ArchiveInvalid, InvalidManifest, InvalidRequest
from app.core.tokens import estimate_text_tokens
from app.domain.models import (
    BatchIngest,
    ExtractedDocument,
    ManifestRow,
    StudentSubmission,
)
from app.ingest.archive import (
    ZipInventory,
    extract_entries,
    find_manifest,
    inspect_zip,
    parse_manifest,
    resolve_entry,
)
from app.ingest.documents import extract_document
from app.observability.logger import get_logger

logger = get_logger(__name__)

ProgressFn = Callable[[int, int], Awaitable[None]]

# Manifest di dalam ZIP dibaca sekaligus ke memori, jadi ukurannya dibatasi.
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class BerkasSiswa:
    """Satu berkas jawaban milik satu siswa."""

    email: str
    nama: str
    path: Path


@dataclass(frozen=True)
class JawabanMasuk:
    """Dari mana jawaban siswa dibaca.

    Salah satu di antara dua jalur ini yang dipakai, bukan keduanya: berkas per
    siswa, atau satu ZIP berisi manifest.
    """

    berkas: tuple[BerkasSiswa, ...] = field(default_factory=tuple)
    zip_path: Path | None = None
    manifest_path: Path | None = None

    @property
    def jumlah(self) -> int:
        return len(self.berkas) if self.berkas else (1 if self.zip_path else 0)


def _read_manifest_from_zip(zip_path: Path, entry: str) -> bytes:
    """Baca manifest.csv dari dalam ZIP beserta batas ukurannya."""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            info = archive.getinfo(entry)
            if info.file_size > _MAX_MANIFEST_BYTES:
                raise InvalidManifest("Berkas manifest.csv di dalam ZIP terlalu besar.")
            with archive.open(info) as handle:
                return handle.read()
    except KeyError as exc:
        raise InvalidManifest(
            "manifest.csv tidak ditemukan di dalam ZIP."
        ) from exc
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveInvalid("Gagal membaca manifest.csv dari dalam ZIP.") from exc


async def _load_manifest(
    zip_path: Path,
    inventory: ZipInventory,
    manifest_bytes: bytes | None,
    manifest_path: Path | None,
) -> bytes:
    """Ambil byte manifest dari berkas terpisah atau dari dalam ZIP."""
    if manifest_bytes is None and manifest_path is not None:
        # Manifest kecil (maks 2 MB), dibaca lewat thread agar tidak
        # memblokir event loop pada disk jaringan.
        manifest_bytes = await asyncio.to_thread(manifest_path.read_bytes)

    if manifest_bytes is None:
        entry = find_manifest(inventory)
        if entry is None:
            raise InvalidManifest(
                "manifest.csv tidak ditemukan. Sertakan manifest.csv di dalam "
                "ZIP atau unggah sebagai berkas terpisah."
            )
        manifest_bytes = _read_manifest_from_zip(zip_path, entry)

    return manifest_bytes


def _resolve_rows(
    inventory: ZipInventory, rows: list[ManifestRow]
) -> dict[str, str]:
    """Petakan email ke nama entri ZIP, tolak bila ada berkas yang tak ketemu."""
    resolved: dict[str, str] = {}
    missing: list[dict[str, object]] = []

    for row in rows:
        entry_name = resolve_entry(inventory, row.nama_file)
        if entry_name is None:
            missing.append({"baris": row.line, "nama_file": row.nama_file})
            continue
        resolved[row.email] = entry_name

    if missing:
        raise InvalidManifest(
            f"Ada {len(missing)} berkas di manifest yang tidak ditemukan di ZIP.",
            details={"tidak_ditemukan": missing[:20]},
        )
    return resolved


def _list_unused_entries(inventory: ZipInventory, used: list[str]) -> list[str]:
    """Daftar entri ZIP yang tidak dipakai; manifest sendiri selalu dikecualikan."""
    used_entries = set(used)
    return sorted(
        name
        for name in inventory.entries
        if name not in used_entries and not name.lower().endswith("manifest.csv")
    )


def _quality_reason(submission: StudentSubmission) -> str | None:
    """Alasan siswa dilewati berdasarkan isi teks dan perkiraan token.

    Mengembalikan None bila berkas layak dinilai.
    """
    if not submission.text.strip():
        return (
            "Berkas tidak menghasilkan teks. Bila ini hasil pindai, "
            "pastikan gambar terbaca jelas."
        )
    if submission.tokens_estimated > settings.max_tokens_per_student:
        return (
            f"Jawaban melebihi batas {settings.max_tokens_per_student:,} token "
            f"(perkiraan {submission.tokens_estimated:,})."
        )
    return None


async def _try_extract(
    path: Path, position: int, vision_reader
) -> tuple[ExtractedDocument | None, str | None]:
    """Baca satu berkas; kembalikan dokumen atau alasan untuk melewatinya.

    Satu berkas tidak boleh mematikan batch, jadi kegagalan diterjemahkan
    menjadi alasan yang bisa dibaca guru, bukan dilempar ke atas.
    """
    try:
        document = await extract_document(
            path,
            max_ocr_pages=settings.max_ocr_pages,
            vision_reader=vision_reader,
        )
    except AppError as exc:
        logger.warning(
            "Berkas siswa dilewati",
            student_index=position,
            code=exc.code,
        )
        return None, exc.message
    except Exception as exc:  # satu berkas tidak mematikan batch
        logger.warning(
            "Berkas siswa gagal dibaca",
            student_index=position,
            error=str(exc)[:160],
        )
        return None, "Berkas tidak dapat dibaca."
    return document, None


async def _build_submission(
    position: int,
    total: int,
    *,
    email: str,
    nama: str,
    nama_file: str,
    path: Path,
    progress: ProgressFn | None,
    vision_reader,
) -> StudentSubmission:
    """Baca satu berkas menjadi kiriman siswa siap dinilai.

    Dipakai kedua jalur masuk: ZIP maupun berkas per siswa. Dengan satu fungsi
    seperti ini, aturan penilaian berkas (batas token, penanganan berkas gagal)
    tidak mungkin berbeda antara keduanya.
    """
    submission = StudentSubmission(
        index=position,
        email=email,
        nama=nama,
        nama_file=nama_file,
    )

    document, failure = await _try_extract(path, position, vision_reader)
    if failure is not None:
        submission.skipped_reason = failure
        if progress:
            await progress(position, total)
        return submission

    submission.text = document.text
    submission.pages = document.pages
    submission.ocr_pages = document.ocr_pages
    submission.tokens_estimated = estimate_text_tokens(document.text)
    submission.skipped_reason = _quality_reason(submission)

    if progress:
        await progress(position, total)
    return submission


async def _process_row(
    position: int,
    total: int,
    row: ManifestRow,
    resolved: dict[str, str],
    extracted: dict[str, Path],
    progress: ProgressFn | None,
    vision_reader,
) -> StudentSubmission:
    """Baca satu baris manifest menjadi kiriman siswa siap dinilai."""
    path = extracted.get(resolved[row.email])

    if path is None:
        gagal = StudentSubmission(
            index=position, email=row.email, nama=row.nama, nama_file=row.nama_file
        )
        gagal.skipped_reason = "Berkas tidak berhasil diekstrak dari ZIP."
        return gagal

    return await _build_submission(
        position,
        total,
        email=row.email,
        nama=row.nama,
        nama_file=row.nama_file,
        path=path,
        progress=progress,
        vision_reader=vision_reader,
    )


async def _read_students(
    rows: list[ManifestRow],
    resolved: dict[str, str],
    extracted: dict[str, Path],
    progress: ProgressFn | None,
    vision_reader,
) -> list[StudentSubmission]:
    """Baca semua baris manifest menjadi kiriman siswa, satu per satu."""
    total = len(rows)
    students: list[StudentSubmission] = []

    for position, row in enumerate(rows, start=1):
        student = await _process_row(
            position, total, row, resolved, extracted, progress, vision_reader
        )
        students.append(student)

    return students


def _finalize_batch(
    students: list[StudentSubmission], unused: list[str]
) -> BatchIngest:
    """Rangkai hasil akhir dan tolak batch yang tak punya berkas layak nilai."""
    batch = BatchIngest(students=students, unused_files=unused)

    if not batch.graded:
        raise InvalidManifest(
            "Tidak ada satu pun berkas siswa yang bisa dibaca. "
            "Periksa format dan isi berkas.",
            details={
                "dilewati": [
                    {"nama_file": s.nama_file, "alasan": s.skipped_reason}
                    for s in batch.skipped[:20]
                ]
            },
        )

    logger.info(
        "Batch jawaban terbaca",
        jumlah=len(students),
        dinilai=len(batch.graded),
        dilewati=len(batch.skipped),
        berkas_tak_terpakai=len(unused),
    )
    return batch


def pastikan_identitas(email: list[str], nama: list[str]) -> None:
    """Periksa daftar email dan nama sebelum berkasnya disimpan.

    Dipanggil lapisan endpoint supaya permintaan yang salah ditolak sebelum
    berkas disimpan dan sebelum job dibuat. Salah pada daftar ini membuat
    seluruh hasil salah, jadi lebih baik berhenti di depan dengan pesan yang
    menyebut urutannya daripada menilai dengan nama yang tertukar.
    """
    if len(email) != len(nama):
        raise InvalidRequest(
            "Jumlah email dan nama harus sama banyak.",
            details={"email": len(email), "nama": len(nama)},
        )

    if not email:
        raise InvalidRequest("Email dan nama siswa belum dikirim.")

    _pastikan_terisi(email, nama)
    _pastikan_bentuk_email(email)
    _pastikan_email_unik(email)


def _pastikan_terisi(email: list[str], nama: list[str]) -> None:
    """Tidak ada siswa tanpa email atau tanpa nama."""
    tanpa_email = [
        posisi for posisi, alamat in enumerate(email, 1) if not alamat.strip()
    ]
    if tanpa_email:
        raise InvalidRequest(
            "Ada siswa yang belum diberi email.",
            details={"urutan": tanpa_email[:20]},
        )

    tanpa_nama = [
        posisi for posisi, nama_siswa in enumerate(nama, 1) if not nama_siswa.strip()
    ]
    if tanpa_nama:
        raise InvalidRequest(
            "Ada siswa yang belum diberi nama.",
            details={"urutan": tanpa_nama[:20]},
        )


def _pastikan_bentuk_email(email: list[str]) -> None:
    """Bentuk email yang jelas salah ditolak di depan.

    Pemeriksaan ini sengaja longgar. Yang dicegah hanya nilai yang jelas bukan
    alamat email, karena aturan ketat tentang bentuk alamat akan menolak alamat
    yang sah.
    """
    bentuk_salah = [
        posisi
        for posisi, alamat in enumerate(email, 1)
        if "@" not in alamat or alamat.startswith("@")
    ]
    if bentuk_salah:
        raise InvalidRequest(
            "Ada email yang bentuknya tidak wajar.",
            details={"urutan": bentuk_salah[:20]},
        )


def _pastikan_email_unik(email: list[str]) -> None:
    """Satu email dua kali hampir pasti salah kirim."""
    dihitung: dict[str, int] = {}
    for alamat in email:
        kunci = alamat.strip().lower()
        dihitung[kunci] = dihitung.get(kunci, 0) + 1

    ganda = sorted(alamat for alamat, jumlah in dihitung.items() if jumlah > 1)
    if ganda:
        raise InvalidRequest(
            "Ada email yang muncul lebih dari satu kali.",
            details={"email": ganda[:20]},
        )


def _pastikan_roster(berkas: list[BerkasSiswa]) -> None:
    """Periksa daftar siswa sebelum satu berkas pun dibaca."""
    if not berkas:
        raise InvalidRequest("Tidak ada berkas jawaban siswa yang dikirim.")

    if len(berkas) > settings.max_students_per_batch:
        raise InvalidRequest(
            f"Jumlah siswa melebihi batas {settings.max_students_per_batch}.",
            details={"jumlah": len(berkas)},
        )

    pastikan_identitas(
        [item.email for item in berkas], [item.nama for item in berkas]
    )


async def ingest_direct_files(
    berkas: list[BerkasSiswa],
    *,
    progress: ProgressFn | None = None,
    vision_reader=None,
) -> BatchIngest:
    """Baca satu berkas per siswa menjadi daftar kiriman siap nilai.

    Ini jalur utama: aplikasi pengajar sudah memegang daftar kelas, jadi email
    dan nama datang bersama berkasnya. Tidak ada ZIP dan tidak ada manifest.
    """
    _pastikan_roster(berkas)

    total = len(berkas)
    students: list[StudentSubmission] = []

    for position, item in enumerate(berkas, start=1):
        students.append(
            await _build_submission(
                position,
                total,
                email=item.email.strip().lower(),
                nama=item.nama.strip(),
                nama_file=item.path.name,
                path=item.path,
                progress=progress,
                vision_reader=vision_reader,
            )
        )

    return _finalize_batch(students, [])


async def muat_jawaban(
    masuk: JawabanMasuk,
    *,
    workdir: Path,
    progress: ProgressFn | None = None,
    vision_reader=None,
) -> BatchIngest:
    """Baca jawaban siswa dari jalur mana pun yang dipakai.

    Satu pintu masuk untuk kedua cara, supaya alur fitur tidak perlu tahu mana
    yang dipakai pengajar.
    """
    if masuk.berkas:
        return await ingest_direct_files(
            list(masuk.berkas), progress=progress, vision_reader=vision_reader
        )

    if masuk.zip_path is None:
        raise InvalidRequest(
            "Tidak ada jawaban siswa yang dikirim. Kirim satu berkas per siswa "
            "beserta email dan namanya, atau satu berkas ZIP berisi manifest.csv."
        )

    return await ingest_answers(
        masuk.zip_path,
        workdir=workdir,
        manifest_path=masuk.manifest_path,
        progress=progress,
        vision_reader=vision_reader,
    )


async def ingest_answers(
    zip_path: Path,
    *,
    workdir: Path,
    manifest_bytes: bytes | None = None,
    manifest_path: Path | None = None,
    progress: ProgressFn | None = None,
    vision_reader=None,
) -> BatchIngest:
    """Baca ZIP + manifest menjadi daftar kiriman siswa berisi teks."""
    inventory = inspect_zip(zip_path)
    manifest_bytes = await _load_manifest(
        zip_path, inventory, manifest_bytes, manifest_path
    )
    rows = parse_manifest(manifest_bytes)

    # --- Cocokkan setiap baris manifest dengan entri ZIP -------------------
    resolved = _resolve_rows(inventory, rows)

    # --- Ekstrak hanya berkas yang dipakai --------------------------------
    entry_names = list(dict.fromkeys(resolved.values()))
    extracted = extract_entries(zip_path, inventory, entry_names, workdir / "files")
    unused = _list_unused_entries(inventory, entry_names)

    # --- Baca tiap berkas menjadi teks ------------------------------------
    students = await _read_students(rows, resolved, extracted, progress, vision_reader)
    return _finalize_batch(students, unused)
