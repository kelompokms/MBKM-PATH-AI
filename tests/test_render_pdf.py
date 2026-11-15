"""Test penyusunan PDF: isi harus benar-benar sampai ke halaman."""

from __future__ import annotations

import io

import pymupdf
from app.domain.documents import IllustrationImage, MaterialDocument, ReportDocument
from app.domain.schemas import (
    DiagramEdge,
    DiagramNode,
    DiagramSpec,
    MaterialSection,
    TableSpec,
)
from app.render.pdf import build_material_pdf, build_report_pdf, pdf_page_count
from PIL import Image as PilImage


def _text_of(pdf_bytes: bytes) -> str:
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
        return "\n".join(page.get_text() for page in document)


def _png(width: int = 160, height: int = 90) -> bytes:
    """PNG kecil untuk menguji penempatan gambar."""
    buffer = io.BytesIO()
    PilImage.new("RGB", (width, height), (43, 95, 140)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_materi_pdf_berisi_kepala_isi_dan_kesimpulan() -> None:
    document = MaterialDocument(
        judul="Fotosintesis pada Tumbuhan Hijau",
        deskripsi_singkat="Penjelasan ringkas proses fotosintesis untuk kelas VII.",
        pertanyaan="Mengapa tumbuhan membutuhkan cahaya matahari?",
        isi=[
            MaterialSection(
                judul="Pengertian Fotosintesis",
                paragraf=[
                    "Fotosintesis adalah proses tumbuhan mengubah cahaya menjadi energi kimia."
                ],
                poin=["Berlangsung di kloroplas", "Menghasilkan glukosa dan oksigen"],
                tabel=TableSpec(
                    judul="Komponen utama",
                    kolom=["Komponen", "Peran"],
                    baris=[["Klorofil", "Menangkap cahaya"], ["Air", "Bahan baku"]],
                ),
            )
        ],
        kesimpulan=["Fotosintesis menghubungkan energi cahaya dengan kehidupan."],
        diagram=DiagramSpec(
            jenis="alur",
            judul="Alur fotosintesis",
            nodes=[
                DiagramNode(id="a", label="Cahaya matahari"),
                DiagramNode(id="b", label="Klorofil"),
                DiagramNode(id="c", label="Glukosa"),
            ],
            edges=[
                DiagramEdge(dari="a", ke="b"),
                DiagramEdge(dari="b", ke="c"),
            ],
        ),
    )

    pdf = build_material_pdf(document)
    text = _text_of(pdf)

    assert pdf.startswith(b"%PDF")
    assert "Fotosintesis pada Tumbuhan Hijau" in text
    assert "Pengertian Fotosintesis" in text
    assert "Kesimpulan" in text
    assert "Halaman 1 dari" in text
    assert "Klorofil" in text
    assert pdf_page_count(pdf) >= 1


def test_laporan_pdf_memuat_kepala_tetap_dan_tabel() -> None:
    document = ReportDocument(
        judul="Analisis Capaian Hasil Belajar Siswa",
        pengajar="Bu Ratna",
        mata_pelajaran="Biologi",
        kelas="VII A",
        tugas="Ulangan Harian 1",
        deskripsi=["Sebagian besar siswa memahami konsep dasar dengan baik."],
        kolom=[
            "No",
            "Email",
            "Nama Siswa",
            "Nilai",
            "Keterangan Hasil Capaian",
            "Saran untuk Siswa",
        ],
        baris=[
            ["1", "ani@sekolah.id", "Ani", "88", "Sangat Baik", "Pertahankan."],
            ["2", "budi@sekolah.id", "Budi", "64", "Cukup", "Latih soal hitungan."],
        ],
        lebar_kolom=[0.06, 0.22, 0.16, 0.08, 0.18, 0.30],
        kolom_tengah=[0, 3],
    )

    pdf = build_report_pdf(document)
    text = _text_of(pdf)

    assert "Analisis Capaian Hasil Belajar Siswa" in text
    assert "Bu Ratna" in text
    assert "Ulangan Harian 1" in text
    assert "ani@sekolah.id" in text
    assert "Sangat Baik" in text


def test_tabel_panjang_berlanjut_ke_halaman_berikut() -> None:
    rows = [
        [str(index), f"siswa{index}@sekolah.id", f"Siswa {index}", str(60 + index % 40), "Baik", "—"]
        for index in range(1, 61)
    ]
    document = ReportDocument(
        judul="Analisis Capaian Hasil Belajar Siswa",
        pengajar="Bu Ratna",
        mata_pelajaran="Biologi",
        kelas="VII A",
        tugas="Ulangan Harian 1",
        deskripsi=["Ringkasan."],
        kolom=["No", "Email", "Nama Siswa", "Nilai", "Keterangan", "Saran"],
        baris=rows,
        lebar_kolom=[0.06, 0.24, 0.16, 0.08, 0.16, 0.30],
        kolom_tengah=[0, 3],
    )

    pdf = build_report_pdf(document)

    assert pdf_page_count(pdf) > 1
    text = _text_of(pdf)
    # Header tabel harus diulang di halaman berikutnya.
    assert text.count("Nama Siswa") > 1
    assert "siswa60@sekolah.id" in text


def test_materi_dengan_ilustrasi() -> None:
    document = MaterialDocument(
        judul="Siklus Air",
        deskripsi_singkat="Penjelasan siklus air.",
        isi=[MaterialSection(judul="Penguapan", paragraf=["Air menguap karena panas."])],
        kesimpulan=["Air terus berputar."],
        ilustrasi=[
            IllustrationImage(
                judul="Siklus air", keterangan="Ilustrasi siklus air.", image=_png()
            )
        ],
    )

    pdf = build_material_pdf(document)

    assert pdf.startswith(b"%PDF")
    assert pdf_page_count(pdf) >= 1


def test_diagram_peta_pikiran_tidak_error() -> None:
    document = MaterialDocument(
        judul="Peta Pikiran Ekosistem",
        deskripsi_singkat="Ringkasan ekosistem.",
        isi=[MaterialSection(judul="Komponen", paragraf=["Produsen dan konsumen."])],
        kesimpulan=["Ekosistem saling bergantung."],
        diagram=DiagramSpec(
            jenis="peta_pikiran",
            judul="Ekosistem",
            nodes=[
                DiagramNode(id="pusat", label="Ekosistem"),
                DiagramNode(id="p", label="Produsen"),
                DiagramNode(id="k", label="Konsumen"),
                DiagramNode(id="pg", label="Pengurai"),
            ],
            edges=[
                DiagramEdge(dari="pusat", ke="p"),
                DiagramEdge(dari="pusat", ke="k"),
                DiagramEdge(dari="pusat", ke="pg"),
            ],
        ),
    )

    pdf = build_material_pdf(document)

    assert pdf.startswith(b"%PDF")