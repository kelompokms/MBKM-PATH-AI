"""Penyusunan PDF dari data yang sudah jadi.

Tidak ada keputusan bisnis di sini: tidak ada perhitungan nilai, tidak ada
pemanggilan model, tidak ada pembacaan berkas. Modul ini hanya menata teks,
tabel, diagram, dan gambar menjadi halaman A4 yang bisa dicetak.

Reportlab dipakai langsung, bukan mesin render HTML. Konsekuensinya gaya
ditulis sebagai kode, bukan CSS. Imbalannya: hasil deterministik, tanpa
pustaka grafis sistem, identik di Windows dan Linux, dan tinggi setiap
elemen diketahui sebelum halaman disusun sehingga tidak ada tabel yang
terpotong di tengah.
"""

from __future__ import annotations

import io
from xml.sax.saxutils import escape

import pymupdf
from PIL import Image as PilImage
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.domain.documents import (
    IllustrationImage,
    MaterialDocument,
    ReportDocument,
    ReviewDocument,
)
from app.domain.schemas import (
    DiagramSpec,
    IstilahItem,
    MaterialSection,
    ReviewSection,
    TableSpec,
)
from app.engines.diagrams import build_layout
from app.render import theme
from app.render.flowables import DiagramFlowable, NumberedCanvas, footer_label

_MAX_IMAGE_HEIGHT = 210.0


def _p(text: str, style=theme.BODY) -> Paragraph:
    return Paragraph(escape(str(text or "")), style)


def _canvas_factory(label: str, title: str):
    class _Canvas(NumberedCanvas):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.footer_label = label
            self.document_label = title

    return _Canvas


def _doc_template(buffer: io.BytesIO) -> SimpleDocTemplate:
    return SimpleDocTemplate(
        buffer,
        pagesize=(theme.PAGE_WIDTH, theme.PAGE_HEIGHT),
        leftMargin=theme.PAGE_MARGIN,
        rightMargin=theme.PAGE_MARGIN,
        topMargin=theme.PAGE_MARGIN,
        bottomMargin=64,
        title="PATH-AI",
        author="PATH-AI",
        subject="Dokumen pembelajaran",
        creator="PATH-AI",
    )


def _title_block(judul: str, subjudul: str | None, story: list) -> None:
    story.append(_p(judul, theme.TITLE))
    if subjudul:
        story.append(_p(subjudul, theme.SUBTITLE))
    story.append(Spacer(1, 4))
    story.append(
        HRFlowable(
            width="100%",
            thickness=1.4,
            color=theme.ACCENT,
            spaceBefore=2,
            spaceAfter=12,
        )
    )


def _meta_block(rows: list[tuple[str, str]]) -> Table:
    data = [
        [
            Paragraph(escape(label), theme.SMALL),
            Paragraph(escape(value or "—"), theme.BODY),
        ]
        for label, value in rows
    ]
    table = Table(data, colWidths=[38 * mm, theme.CONTENT_WIDTH - 38 * mm])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (0, -1), 0),
            ]
        )
    )
    return table


def _question_box(pertanyaan: str) -> Table:
    inner = Paragraph(escape(pertanyaan), theme.BODY)
    table = Table([[inner]], colWidths=[theme.CONTENT_WIDTH])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), theme.SOFT),
                ("BOX", (0, 0), (-1, -1), 0.6, theme.RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ]
        )
    )
    return table


def _table_header(kolom: list[str], center_columns: set[int]) -> list[Paragraph]:
    """Sel header tabel; kolom bernomor digambar rata tengah."""
    return [
        Paragraph(
            escape(str(name)),
            theme.TABLE_HEADER_CENTER if index in center_columns else theme.TABLE_HEADER,
        )
        for index, name in enumerate(kolom)
    ]


def _cell_style(index: int, center_columns: set[int], small_columns: set[int]):
    """Pilih gaya sel: kolom kecil menang atas kolom tengah, sisanya biasa."""
    if index in small_columns:
        return theme.TABLE_CELL_TINY
    if index in center_columns:
        return theme.TABLE_CELL_CENTER
    return theme.TABLE_CELL


def _table_body(
    baris: list[list[str]],
    center_columns: set[int],
    small_columns: set[int],
) -> list[list[Paragraph]]:
    """Sel isi tabel, dengan gaya berbeda menurut jenis kolomnya."""
    body: list[list[Paragraph]] = []
    for row in baris:
        cells: list[Paragraph] = []
        for index, cell in enumerate(row):
            style = _cell_style(index, center_columns, small_columns)
            cells.append(Paragraph(escape(str(cell)), style))
        body.append(cells)
    return body


def _column_widths(kolom: list[str], lebar: list[float] | None) -> list[float]:
    """Lebar kolom proporsional; jatuh ke lebar rata bila proporsi tak cocok."""
    if lebar and len(lebar) == len(kolom):
        total = sum(lebar) or 1.0
        return [theme.CONTENT_WIDTH * (value / total) for value in lebar]
    return [theme.CONTENT_WIDTH / len(kolom)] * len(kolom)


def _data_table_style() -> TableStyle:
    """Gaya tabel data: header berwarna dan baris selang-seling."""
    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), theme.ACCENT),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (0, 0), (-1, 0), "LEFT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LINEBELOW", (0, 0), (-1, -2), 0.4, theme.RULE),
            ("LINEBELOW", (0, -1), (-1, -1), 0.8, theme.RULE),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, theme.SOFT]),
        ]
    )


def _data_table(
    kolom: list[str],
    baris: list[list[str]],
    *,
    lebar: list[float] | None = None,
    tengah: list[int] | None = None,
    kecil: list[int] | None = None,
) -> Table:
    """Tabel data dengan header yang berulang di setiap halaman."""
    center_columns = set(tengah or [])
    small_columns = set(kecil or [])

    header = _table_header(kolom, center_columns)
    body = _table_body(baris, center_columns, small_columns)
    widths = _column_widths(kolom, lebar)

    table = Table([header, *body], colWidths=widths, repeatRows=1)
    table.setStyle(_data_table_style())
    return table


def _spec_table(spec: TableSpec) -> list:
    parts: list = [Spacer(1, 6)]
    if spec.judul:
        parts.append(_p(spec.judul, theme.SMALL))
        parts.append(Spacer(1, 3))
    parts.append(_data_table(spec.kolom, spec.baris))
    parts.append(Spacer(1, 8))
    return parts


def _image_flowable(image: bytes, max_width: float) -> Image | None:
    """Bungkus byte gambar menjadi Image dengan ukuran yang muat di halaman.

    Isinya bisa JPEG atau PNG; keduanya dibaca lewat PIL untuk mengetahui
    ukurannya, lalu ditempelkan apa adanya.
    """
    if not image:
        return None
    try:
        with PilImage.open(io.BytesIO(image)) as probe:
            pixel_w, pixel_h = probe.size
    except Exception:  # gambar rusak tidak boleh mematikan dokumen
        return None

    if pixel_w <= 0 or pixel_h <= 0:
        return None

    width = max_width
    height = width * pixel_h / pixel_w
    if height > _MAX_IMAGE_HEIGHT:
        height = _MAX_IMAGE_HEIGHT
        width = height * pixel_w / pixel_h

    try:
        return Image(io.BytesIO(image), width=width, height=height)
    except Exception:  # gambar rusak tidak boleh mematikan dokumen
        return None


def _question_section(pertanyaan: str) -> list:
    """Blok pertanyaan pemicu: label, kotak, dan jeda sesudahnya."""
    return [
        _p("Pertanyaan", theme.SMALL),
        Spacer(1, 3),
        _question_box(pertanyaan),
        Spacer(1, 10),
    ]


def _isi_section(section: MaterialSection, index: int) -> list:
    """Satu bagian isi: judul bernomor, paragraf, poin, tabel, lalu jeda."""
    block: list = [_p(f"{index}. {section.judul}", theme.BODY)]
    for paragraph in section.paragraf:
        block.append(_p(paragraph, theme.BODY))
    for point in section.poin:
        block.append(Paragraph(escape(point), theme.BULLET, bulletText="•"))
    if section.tabel is not None:
        block.extend(_spec_table(section.tabel))
    block.append(Spacer(1, 4))
    return block


def _diagram_block(diagram: DiagramSpec) -> list:
    """Blok diagram; kosong bila tata letaknya tidak menghasilkan kotak."""
    layout = build_layout(diagram, theme.CONTENT_WIDTH)
    if not layout.boxes:
        return []
    block = [_p("Diagram", theme.SMALL), Spacer(1, 4)]
    if diagram.judul:
        block.append(_p(diagram.judul, theme.CAPTION))
    block.append(DiagramFlowable(layout))
    return [KeepTogether(block), Spacer(1, 10)]


def _ilustrasi_block(image: IllustrationImage) -> list:
    """Blok ilustrasi: gambar beserta keterangan; kosong bila gambar rusak."""
    flowable = _image_flowable(image.image, theme.CONTENT_WIDTH)
    if flowable is None:
        return []
    block: list = [flowable]
    caption = image.keterangan or image.judul
    if caption:
        block.append(_p(caption, theme.CAPTION))
    return [KeepTogether(block)]


def _kesimpulan_block(kesimpulan: list[str]) -> list:
    """Judul Kesimpulan beserta poin-poin penutupnya."""
    block: list = [_p("Kesimpulan", theme.SECTION)]
    for point in kesimpulan:
        block.append(Paragraph(escape(point), theme.BULLET, bulletText="•"))
    return block


def build_material_pdf(document: MaterialDocument) -> bytes:
    """PDF materi: Kepala, Isi, Kesimpulan."""
    buffer = io.BytesIO()
    story: list = []

    _title_block(document.judul, document.deskripsi_singkat, story)

    if document.pertanyaan:
        story.extend(_question_section(document.pertanyaan))

    story.append(_p("Isi", theme.SECTION))
    for index, section in enumerate(document.isi, start=1):
        story.extend(_isi_section(section, index))

    if document.diagram is not None:
        story.extend(_diagram_block(document.diagram))

    for image in document.ilustrasi:
        story.extend(_ilustrasi_block(image))

    story.extend(_kesimpulan_block(document.kesimpulan))

    template = _doc_template(buffer)
    template.build(
        story,
        canvasmaker=_canvas_factory(footer_label(), document.judul[:60]),
    )
    return buffer.getvalue()


def build_report_pdf(document: ReportDocument) -> bytes:
    """PDF laporan penilaian/kemiripan/indikasi AI dengan kepala tetap."""
    buffer = io.BytesIO()
    story: list = []

    _title_block(document.judul, None, story)

    story.append(
        _meta_block(
            [
                ("Pengajar", document.pengajar),
                ("Mata Pelajaran", document.mata_pelajaran),
                ("Kelas", document.kelas),
                ("Tugas", document.tugas),
            ]
        )
    )
    story.append(Spacer(1, 10))

    story.append(_p("Deskripsi Hasil Analisa", theme.SECTION))
    for paragraph in document.deskripsi:
        story.append(_p(paragraph, theme.BODY))

    if document.baris:
        story.append(Spacer(1, 6))
        story.append(
            _data_table(
                document.kolom,
                document.baris,
                lebar=document.lebar_kolom,
                tengah=document.kolom_tengah,
                kecil=document.kolom_kecil,
            )
        )

    for note in document.catatan:
        story.append(Spacer(1, 6))
        story.append(_p(note, theme.SMALL))

    template = _doc_template(buffer)
    template.build(
        story,
        canvasmaker=_canvas_factory(footer_label(), document.judul[:60]),
    )
    return buffer.getvalue()


def _meta_halaman(document: ReviewDocument) -> str:
    """Keterangan halaman: '39 halaman, dibaca 39' atau hanya jumlahnya."""
    if document.halaman_dokumen <= 0:
        return "—"
    if document.halaman_dibaca and document.halaman_dibaca < document.halaman_dokumen:
        return (
            f"{document.halaman_dokumen} halaman, "
            f"dibaca sampai {document.halaman_dibaca}"
        )
    return f"{document.halaman_dokumen} halaman"


def _poin_block(judul: str, poin: list[str]) -> list:
    """Judul bagian beserta poin-poinnya."""
    block: list = [_p(judul, theme.SECTION)]
    for butir in poin:
        block.append(Paragraph(escape(butir), theme.BULLET, bulletText="•"))
    return block


def _kepala_telaah(document: ReviewDocument) -> list:
    """Meta telaah: dokumen sumber, jumlah halaman, dan fokusnya."""
    return [
        _meta_block(
            [
                ("Dokumen", document.sumber or "—"),
                ("Halaman", _meta_halaman(document)),
                ("Fokus", document.fokus or "Seluruh isi dokumen"),
            ]
        ),
        Spacer(1, 10),
    ]


def _bagian_telaah(bagian: list[ReviewSection]) -> list:
    """Seluruh bagian laporan, bernomor."""
    blok: list = []

    for index, bagian_laporan in enumerate(bagian, start=1):
        blok.append(_p(f"{index}. {bagian_laporan.judul}", theme.BODY))
        for paragraf in bagian_laporan.paragraf:
            blok.append(_p(paragraf, theme.BODY))
        for butir in bagian_laporan.poin:
            blok.append(Paragraph(escape(butir), theme.BULLET, bulletText="•"))
        blok.append(Spacer(1, 4))

    return blok


def _istilah_telaah(istilah: list[IstilahItem]) -> list:
    """Tabel istilah penting beserta artinya menurut dokumen."""
    if not istilah:
        return []

    return [
        Spacer(1, 6),
        _p("Istilah Penting", theme.SECTION),
        _data_table(
            ["Istilah", "Arti menurut dokumen"],
            [[item.nama, item.arti] for item in istilah],
            lebar=[0.26, 0.74],
        ),
    ]


def _cakupan_telaah(document: ReviewDocument) -> list:
    """Tabel kerja dan keterangan apa adanya tentang cakupan pembacaan."""
    blok: list = []

    if document.peta_bagian:
        blok.extend(
            [
                Spacer(1, 10),
                _p("Cakupan Pembacaan", theme.SECTION),
                _data_table(
                    document.kolom_peta,
                    document.peta_bagian,
                    lebar=[0.1, 0.2, 0.5, 0.2],
                    tengah=[0],
                ),
            ]
        )

    for note in document.catatan:
        blok.extend([Spacer(1, 6), _p(note, theme.SMALL)])

    return blok


def build_review_pdf(document: ReviewDocument) -> bytes:
    """PDF telaah dokumen: ringkasan eksekutif, isi, temuan, lalu cakupan."""
    buffer = io.BytesIO()
    story: list = []

    _title_block(document.judul, document.sumber or None, story)
    story.extend(_kepala_telaah(document))

    story.append(_p("Ringkasan Eksekutif", theme.SECTION))
    story.append(_p(document.ringkasan_eksekutif, theme.BODY))

    story.append(_p("Isi Dokumen", theme.SECTION))
    story.extend(_bagian_telaah(document.bagian))

    if document.tabel_perbandingan is not None:
        story.append(_p("Perbandingan", theme.SECTION))
        story.extend(_spec_table(document.tabel_perbandingan))

    if document.diagram is not None:
        story.extend(_diagram_block(document.diagram))

    if document.temuan_utama:
        story.append(Spacer(1, 6))
        story.extend(_poin_block("Temuan Utama", document.temuan_utama))

    if document.keterbatasan:
        story.append(Spacer(1, 6))
        story.extend(_poin_block("Keterbatasan", document.keterbatasan))

    story.extend(_istilah_telaah(document.istilah))
    story.extend(_cakupan_telaah(document))

    template = _doc_template(buffer)
    template.build(
        story,
        canvasmaker=_canvas_factory(footer_label(), document.judul[:60]),
    )
    return buffer.getvalue()


def pdf_page_count(pdf_bytes: bytes) -> int:
    """Jumlah halaman, untuk pengujian dan laporan operasional."""
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
            return document.page_count
    except Exception:  # PDF rusak dianggap tidak punya halaman
        return 0
