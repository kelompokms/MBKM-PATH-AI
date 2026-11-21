"""Skema keluaran terstruktur dari model.

Semua kelas di sini adalah kontrak: model wajib mengembalikan bentuk ini,
dan `core.jsonio` akan memaksa satu putaran perbaikan bila tidak.

Prinsip yang dipegang di seluruh berkas:

- Tidak ada aritmetika yang diserahkan ke model. Model memberi skor per butir
  dan kutipan bukti; total, label, dan persentase dihitung server.
- Nilai yang rentan keluar rentang (mis. skor 120 atau persentase -5)
  dijepit di sini, bukan dibiarkan merambat ke laporan.
- Data yang tidak bisa dibuktikan dibuang, bukan dipercaya. Kutipan bukti
  divalidasi terhadap teks asli oleh lapisan pemanggil.
"""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

DiagramKind = Literal["alur", "peta_pikiran"]
ItemKind = Literal["objektif", "esai"]
AiBand = Literal["Rendah", "Sedang", "Tinggi"]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------- #
# Fitur 1 & 2 — materi
# --------------------------------------------------------------------------- #


class TableSpec(BaseModel):
    """Tabel yang dibuat model. Isi sel adalah teks, bukan angka terhitung."""

    judul: str = ""
    kolom: list[str] = Field(min_length=2)
    baris: list[list[str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _rapikan_baris(self) -> TableSpec:
        # Lebar baris yang tidak sama akan merusak tabel di PDF. Sel yang
        # kurang diisi kosong, sel yang lebih dipotong.
        width = len(self.kolom)
        self.baris = [
            (row + [""] * width)[:width] for row in self.baris
        ]
        return self


class DiagramNode(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=120)


class DiagramEdge(BaseModel):
    dari: str = Field(min_length=1, max_length=40)
    ke: str = Field(min_length=1, max_length=40)
    label: str = Field(default="", max_length=80)


class DiagramSpec(BaseModel):
    jenis: DiagramKind = "alur"
    judul: str = ""
    nodes: list[DiagramNode] = Field(min_length=2)
    edges: list[DiagramEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def _buang_sisi_menggantung(self) -> DiagramSpec:
        """Sisi yang menunjuk node tidak dikenal dibuang.

        Model kadang mengarang id pada sisi. Menggambar sisi seperti itu akan
        melempar galat atau menghasilkan garis ke tempat acak.
        """
        known = {node.id for node in self.nodes}
        self.edges = [
            edge
            for edge in self.edges
            if edge.dari in known and edge.ke in known and edge.dari != edge.ke
        ]
        return self


class IllustrationSpec(BaseModel):
    judul: str = Field(default="", max_length=160)
    prompt_gambar: str = Field(min_length=10, max_length=2000)
    keterangan: str = Field(default="", max_length=400)


class MaterialSection(BaseModel):
    judul: str = Field(min_length=3, max_length=200)
    paragraf: list[str] = Field(default_factory=list)
    poin: list[str] = Field(default_factory=list)
    tabel: TableSpec | None = None


class MaterialOutput(BaseModel):
    judul: str = Field(min_length=3, max_length=200)
    deskripsi_singkat: str = Field(min_length=10)
    isi: list[MaterialSection] = Field(min_length=1)
    kesimpulan: list[str] = Field(min_length=1)
    diagram: DiagramSpec | None = None
    ilustrasi: list[IllustrationSpec] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Fitur 6 — ringkasan dokumen panjang
# --------------------------------------------------------------------------- #


class IstilahItem(BaseModel):
    """Satu istilah penting beserta artinya menurut dokumen."""

    nama: str = Field(min_length=1, max_length=80)
    arti: str = Field(min_length=3, max_length=400)


# Batas jumlah butir per bidang catatan bagian.
#
# Batasnya lebar dengan sengaja. Catatan ini BUKAN ringkasan akhir, melainkan
# badan laporan: satu potongan dokumen menjadi satu bagian laporan, apa adanya,
# tanpa dipadatkan lagi. Batas yang ketat di sini langsung memotong isi
# laporan, sedangkan batas yang lebar hanya membuat catatannya lebih tebal.
_HITUNG_BUTIR = {"ringkasan": 8, "poin": 16, "metode": 40, "istilah": 12}
_BATAS_JUDUL_BAGIAN = 180


class ChunkSummary(BaseModel):
    """Catatan isi satu potongan dokumen (tahap pertama).

    Bentuknya sengaja berbeda dari materi ajar: yang dicatat adalah isi dokumen
    apa adanya, termasuk seluruh nama metode, dataset, metrik, dan istilah yang
    disebut. Catatan inilah yang menjadi badan laporan akhir, sehingga hal yang
    tidak tercatat di sini tidak akan pernah muncul di laporan.
    """

    judul_bagian: str = Field(min_length=3, max_length=_BATAS_JUDUL_BAGIAN)
    ringkasan: list[str] = Field(min_length=1, max_length=8)
    poin: list[str] = Field(default_factory=list, max_length=16)
    # Nama metode, alat, dataset, atau pendekatan yang disebut potongan ini.
    metode: list[str] = Field(default_factory=list, max_length=40)
    istilah: list[IstilahItem] = Field(default_factory=list, max_length=12)

    @field_validator("ringkasan", "poin", "metode", "istilah", mode="before")
    @classmethod
    def _pangkas_kelebihan_butir(cls, value: object, info: ValidationInfo) -> object:
        """Potong butir yang berlebih, jangan tolak keluarannya.

        Model kadang mencatat 30 lebih nama metode dalam satu bagian. Menolak
        keluaran seperti itu memaksa satu putaran perbaikan penuh, yang biayanya
        sebesar satu panggilan lagi. Memotongnya di sini menghilangkan
        pemborosan itu, dan batasnya sudah lebar sehingga jarang terpakai.
        """
        batas = _HITUNG_BUTIR.get(info.field_name, 0)

        if batas and isinstance(value, list) and len(value) > batas:
            return value[:batas]

        return value

    @field_validator("judul_bagian", mode="before")
    @classmethod
    def _pangkas_judul(cls, value: object) -> object:
        """Judul yang kepanjangan dipotong, dengan alasan yang sama."""
        if isinstance(value, str) and len(value) > _BATAS_JUDUL_BAGIAN:
            return value[:_BATAS_JUDUL_BAGIAN]

        return value


class ReviewSection(BaseModel):
    """Satu bagian laporan.

    Dibangun dari catatan satu potongan dokumen, jadi jumlahnya mengikuti
    panjang dokumen: dokumen tujuh bagian menghasilkan tujuh bagian laporan.
    """

    judul: str = Field(min_length=3, max_length=200)
    paragraf: list[str] = Field(default_factory=list, max_length=12)
    poin: list[str] = Field(default_factory=list, max_length=20)


class DocumentOverview(BaseModel):
    """Keluaran tahap penggabungan: bagian di luar badan laporan.

    Badan laporan disusun dari catatan per bagian, sehingga yang diminta di
    sini hanya hal-hal yang menuntut pembacaan seluruh dokumen sekaligus:
    ikhtisar, perbandingan, temuan, keterbatasan, dan daftar istilah.
    """

    judul: str = Field(min_length=3, max_length=200)
    ringkasan_eksekutif: str = Field(min_length=40, max_length=3000)
    tabel_perbandingan: TableSpec | None = None
    temuan_utama: list[str] = Field(default_factory=list, max_length=16)
    keterbatasan: list[str] = Field(default_factory=list, max_length=12)
    istilah: list[IstilahItem] = Field(default_factory=list, max_length=20)
    diagram: DiagramSpec | None = None


# --------------------------------------------------------------------------- #
# Fitur 3 — kunci jawaban
# --------------------------------------------------------------------------- #


class AnswerKeyItem(BaseModel):
    nomor: int = Field(ge=1, le=200)
    butir_soal: str = Field(min_length=3, max_length=800)
    kunci_jawaban: str = Field(min_length=1, max_length=2000)
    jenis: ItemKind = "esai"
    bobot: float = Field(default=1.0, gt=0, le=100)
    kriteria: list[str] = Field(default_factory=list, max_length=8)


class AnswerKeyDraft(BaseModel):
    """Keluaran tahap ekstraksi. Tidak langsung dipakai menilai."""

    judul_tugas: str = Field(default="", max_length=200)
    kelas: str = Field(default="", max_length=60)
    mata_pelajaran: str = Field(default="", max_length=100)
    items: list[AnswerKeyItem] = Field(min_length=1, max_length=100)
    # Keraguan model dilaporkan, bukan disembunyikan.
    issues: list[str] = Field(default_factory=list, max_length=10)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _nomor_unik(self) -> AnswerKeyDraft:
        seen: set[int] = set()
        kept: list[AnswerKeyItem] = []
        for item in self.items:
            if item.nomor in seen:
                continue
            seen.add(item.nomor)
            kept.append(item)
        self.items = kept
        return self

    @property
    def needs_review(self) -> bool:
        return self.confidence < 0.6 or bool(self.issues)


# --------------------------------------------------------------------------- #
# Fitur 3 — penilaian
# --------------------------------------------------------------------------- #


class ItemScore(BaseModel):
    nomor: int = Field(ge=1, le=200)
    skor: float = Field(ge=0, le=100)
    kutipan_bukti: str = Field(default="", max_length=400)
    catatan: str = Field(default="", max_length=300)

    @field_validator("skor")
    @classmethod
    def _jepit(cls, value: float) -> float:
        return _clamp(value, 0.0, 100.0)


class StudentGradeDraft(BaseModel):
    """Keluaran penilaian per siswa. Total dihitung server dari `skor`."""

    nilai_per_item: list[ItemScore] = Field(min_length=1, max_length=100)
    saran: str = Field(min_length=5, max_length=600)


class ClassSummaryDraft(BaseModel):
    deskripsi: list[str] = Field(min_length=1, max_length=6)


# --------------------------------------------------------------------------- #
# Fitur 4 — kemiripan
# --------------------------------------------------------------------------- #


class PairJudgeDraft(BaseModel):
    persentase: float = Field(ge=0, le=100)
    alasan: str = Field(default="", max_length=400)

    @field_validator("persentase")
    @classmethod
    def _jepit(cls, value: float) -> float:
        return _clamp(value, 0.0, 100.0)


# --------------------------------------------------------------------------- #
# Fitur 5 — indikasi penggunaan AI
# --------------------------------------------------------------------------- #


class AiDetectDraft(BaseModel):
    persentase: float = Field(ge=0, le=100)
    band: AiBand
    bukti: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("persentase")
    @classmethod
    def _jepit(cls, value: float) -> float:
        return _clamp(value, 0.0, 100.0)