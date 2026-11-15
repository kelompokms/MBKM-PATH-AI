"""Data yang siap dicetak.

Lapisan `flows` menyusun objek ini; lapisan `render` mengubahnya menjadi PDF.
Pemisahan ini membuat penyusunan isi bisa diuji tanpa menyentuh reportlab,
dan membuat penggantian mesin cetak hanya berdampak pada satu modul.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.schemas import (
    DiagramSpec,
    IstilahItem,
    MaterialSection,
    ReviewSection,
    TableSpec,
)


@dataclass
class IllustrationImage:
    """Satu gambar pelengkap, siap ditempelkan ke PDF."""

    judul: str
    keterangan: str
    # Namanya `image`, bukan `png`: model gambar mengembalikan JPEG, dan
    # hanya format yang tidak bisa ditempelkan ke PDF yang dikonversi.
    image: bytes


@dataclass
class MaterialDocument:
    judul: str
    deskripsi_singkat: str
    isi: list[MaterialSection]
    kesimpulan: list[str]
    pertanyaan: str | None = None
    diagram: DiagramSpec | None = None
    ilustrasi: list[IllustrationImage] = field(default_factory=list)


@dataclass
class ReviewDocument:
    """Telaah atas sebuah dokumen panjang, siap dicetak.

    `peta_bagian` bukan hiasan. Dokumen yang lebih panjang daripada batas
    pemrosesan tetap diterima, dan tabel itu yang membuat pembaca tahu bagian
    mana yang benar-benar dibaca dan bagian mana yang gagal.
    """

    judul: str
    ringkasan_eksekutif: str
    bagian: list[ReviewSection]
    temuan_utama: list[str] = field(default_factory=list)
    keterbatasan: list[str] = field(default_factory=list)
    istilah: list[IstilahItem] = field(default_factory=list)
    sumber: str = ""
    fokus: str | None = None
    halaman_dokumen: int = 0
    halaman_dibaca: int = 0
    tabel_perbandingan: TableSpec | None = None
    diagram: DiagramSpec | None = None
    kolom_peta: list[str] = field(default_factory=list)
    peta_bagian: list[list[str]] = field(default_factory=list)
    catatan: list[str] = field(default_factory=list)


@dataclass
class BarisRekap:
    """Satu baris rekap koreksi: satu siswa."""

    nama: str
    email: str
    nilai: float
    keterangan: str
    saran: str = ""


@dataclass
class RekapKoreksi:
    """Rekap koreksi satu kelas, siap diisi ke template Excel."""

    pengajar: str
    mapel: str
    kelas: str
    tugas: str
    deskripsi: list[str] = field(default_factory=list)
    baris: list[BarisRekap] = field(default_factory=list)


@dataclass
class BarisPlagiasi:
    """Satu baris rekap kemiripan: satu siswa dan persentase tertingginya."""

    nama: str
    email: str
    persentase: float


@dataclass
class RekapPlagiasi:
    """Rekap kemiripan satu kelas, siap diisi ke template Excel."""

    pengajar: str
    mapel: str
    kelas: str
    tugas: str
    deskripsi: list[str] = field(default_factory=list)
    baris: list[BarisPlagiasi] = field(default_factory=list)


@dataclass
class ReportDocument:
    judul: str
    pengajar: str
    mata_pelajaran: str
    kelas: str
    tugas: str
    deskripsi: list[str]
    kolom: list[str]
    baris: list[list[str]]
    # Proporsi lebar kolom, mis. [0.06, 0.22, 0.16, 0.08, 0.18, 0.30].
    lebar_kolom: list[float] | None = None
    # Indeks kolom yang isinya di tengah (mis. nomor dan nilai).
    kolom_tengah: list[int] = field(default_factory=list)
    # Indeks kolom yang dicetak lebih kecil, untuk teks panjang tanpa spasi.
    kolom_kecil: list[int] = field(default_factory=list)
    catatan: list[str] = field(default_factory=list)