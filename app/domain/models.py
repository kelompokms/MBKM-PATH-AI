"""Model domain yang dipakai lintas lapisan."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ManifestRow:
    """Satu baris manifest.csv."""

    line: int
    email: str
    nama: str
    nama_file: str


@dataclass
class StudentSubmission:
    """Satu berkas jawaban siswa yang sudah dibaca menjadi teks."""

    index: int
    email: str
    nama: str
    nama_file: str
    text: str = ""
    tokens_estimated: int = 0
    pages: int = 0
    ocr_pages: int = 0
    # Alasan siswa ini dikeluarkan dari penilaian, bila ada.
    # Contoh: "Berkas melebihi batas 8.000 token (perkiraan 12.400)."
    skipped_reason: str | None = None

    @property
    def usable(self) -> bool:
        return self.skipped_reason is None and bool(self.text.strip())


@dataclass
class BatchIngest:
    """Hasil pembacaan satu ZIP kumpulan jawaban."""

    students: list[StudentSubmission]
    unused_files: list[str] = field(default_factory=list)
    duplicate_emails: list[str] = field(default_factory=list)

    @property
    def graded(self) -> list[StudentSubmission]:
        return [s for s in self.students if s.usable]

    @property
    def skipped(self) -> list[StudentSubmission]:
        return [s for s in self.students if not s.usable]


@dataclass
class ExtractedDocument:
    """Teks hasil pembacaan satu dokumen, beserta jejak cara membacanya."""

    text: str
    pages: int = 0
    ocr_pages: int = 0
    # Halaman bergambar yang TIDAK dibaca karena melewati batas OCR. Dicatat
    # supaya laporan bisa menyebutkannya, bukan berpura-pura seluruh dokumen
    # terbaca. Berkas hasil pindai atau foto sering melewati batas ini.
    ocr_skipped: int = 0

    @property
    def used_ocr(self) -> bool:
        return self.ocr_pages > 0