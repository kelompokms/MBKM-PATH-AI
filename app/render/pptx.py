"""Penyusunan materi ajar dalam bentuk presentasi PowerPoint (.pptx).

Menggunakan palet warna yang sama dengan `theme.py` agar materi PDF dan PPTX
terlihat berasal dari sistem yang sama. Seluruh slide disusun dengan ukuran
layar lebar 16:9 (13,33 × 7,5 inci) dan bentuk native PowerPoint sehingga guru
dapat menyunting teks, tabel, maupun kotak diagram secara langsung.
"""

from __future__ import annotations

import io
from typing import Any

from PIL import Image as PilImage
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

from app.domain.documents import IllustrationImage, MaterialDocument
from app.domain.schemas import DiagramSpec, MaterialSection, TableSpec

PPTX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)

# --- Palet (seragam dengan theme.py) -----------------------------------------
_INK = RGBColor(0x1A, 0x22, 0x33)
_MUTED = RGBColor(0x5A, 0x64, 0x72)
_ACCENT = RGBColor(0x2B, 0x5F, 0x8C)
_ACCENT_SOFT = RGBColor(0xE8, 0xF0, 0xF7)
_RULE = RGBColor(0xC9, 0xD3, 0xDF)
_SOFT = RGBColor(0xF4, 0xF6, 0xF9)
_WHITE = RGBColor(0xFF, 0xFF, 0xFF)

_FONT = "Calibri"
_SLIDE_W = Inches(13.333)
_SLIDE_H = Inches(7.5)
_MARGIN_X = Inches(0.8)
_CONTENT_W = _SLIDE_W - 2 * _MARGIN_X


def build_material_pptx(
    doc: MaterialDocument, *, jenjang: str | None = None
) -> tuple[bytes, int]:
    """Susun presentasi .pptx dari `MaterialDocument`, kembalikan (bytes, jumlah_slide)."""
    prs = Presentation()
    prs.slide_width = _SLIDE_W
    prs.slide_height = _SLIDE_H
    blank = prs.slide_layouts[6]

    _add_cover_slide(prs, blank, doc, jenjang=jenjang)

    for index, section in enumerate(doc.isi, start=1):
        _add_section_slide(prs, blank, index, section)
        if section.tabel is not None:
            _add_table_slide(prs, blank, section.judul, section.tabel)

    if doc.diagram is not None:
        _add_diagram_slide(prs, blank, doc.diagram)

    for image in doc.ilustrasi:
        _add_illustration_slide(prs, blank, image)

    if doc.kesimpulan:
        _add_conclusion_slide(prs, blank, doc.kesimpulan)

    _add_slide_numbers(prs)

    buffer = io.BytesIO()
    prs.save(buffer)
    return buffer.getvalue(), len(prs.slides)


# --------------------------------------------------------------------------- #
# Penyusunan per jenis slide
# --------------------------------------------------------------------------- #


def _add_cover_slide(
    prs: Any,
    layout: Any,
    doc: MaterialDocument,
    *,
    jenjang: str | None,
) -> None:
    slide = prs.slides.add_slide(layout)

    # Pita aksen di sisi kiri dan garis tipis di atas.
    stripe = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), Inches(0.35), _SLIDE_H
    )
    stripe.fill.solid()
    stripe.fill.fore_color.rgb = _ACCENT
    stripe.line.fill.background()

    y_cursor = Inches(1.45)

    if jenjang and jenjang.strip():
        badge = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE,
            Inches(1.0),
            y_cursor,
            Inches(3.4),
            Inches(0.45),
        )
        badge.fill.solid()
        badge.fill.fore_color.rgb = _ACCENT_SOFT
        badge.line.fill.background()
        tf_badge = badge.text_frame
        tf_badge.word_wrap = True
        p_badge = tf_badge.paragraphs[0]
        p_badge.text = jenjang.strip()
        p_badge.font.name = _FONT
        p_badge.font.size = Pt(14)
        p_badge.font.bold = True
        p_badge.font.color.rgb = _ACCENT
        y_cursor += Inches(0.7)

    # Judul utama
    title_box = slide.shapes.add_textbox(
        Inches(1.0), y_cursor, Inches(11.3), Inches(1.8)
    )
    tf_title = title_box.text_frame
    tf_title.word_wrap = True
    p_title = tf_title.paragraphs[0]
    p_title.text = doc.judul
    p_title.font.name = _FONT
    p_title.font.size = Pt(38)
    p_title.font.bold = True
    p_title.font.color.rgb = _INK

    # Garis pemisah tipis
    rule = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Inches(1.0),
        y_cursor + Inches(1.9),
        Inches(2.4),
        Inches(0.06),
    )
    rule.fill.solid()
    rule.fill.fore_color.rgb = _ACCENT
    rule.line.fill.background()

    # Deskripsi singkat
    desc_box = slide.shapes.add_textbox(
        Inches(1.0), y_cursor + Inches(2.2), Inches(11.2), Inches(2.0)
    )
    tf_desc = desc_box.text_frame
    tf_desc.word_wrap = True
    p_desc = tf_desc.paragraphs[0]
    p_desc.text = doc.deskripsi_singkat
    p_desc.font.name = _FONT
    p_desc.font.size = Pt(18)
    p_desc.font.color.rgb = _MUTED

    # Footer cover
    footer = slide.shapes.add_textbox(
        Inches(1.0), Inches(6.75), Inches(6.0), Inches(0.4)
    )
    p_foot = footer.text_frame.paragraphs[0]
    p_foot.text = "Materi Ajar — Disusun dengan PATH-AI"
    p_foot.font.name = _FONT
    p_foot.font.size = Pt(11)
    p_foot.font.color.rgb = _MUTED


def _add_header(slide: Any, title: str, subtitle: str = "") -> None:
    """Header seragam di bagian atas slide isi."""
    bar = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), _SLIDE_W, Inches(0.14)
    )
    bar.fill.solid()
    bar.fill.fore_color.rgb = _ACCENT
    bar.line.fill.background()

    box = slide.shapes.add_textbox(_MARGIN_X, Inches(0.38), _CONTENT_W, Inches(0.9))
    tf = box.text_frame
    tf.word_wrap = True

    p = tf.paragraphs[0]
    p.text = title
    p.font.name = _FONT
    p.font.size = Pt(26)
    p.font.bold = True
    p.font.color.rgb = _ACCENT

    if subtitle:
        p_sub = tf.add_paragraph()
        p_sub.text = subtitle
        p_sub.font.name = _FONT
        p_sub.font.size = Pt(13)
        p_sub.font.color.rgb = _MUTED

    rule = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        _MARGIN_X,
        Inches(1.28),
        _CONTENT_W,
        Inches(0.02),
    )
    rule.fill.solid()
    rule.fill.fore_color.rgb = _RULE
    rule.line.fill.background()


def _add_section_slide(
    prs: Any, layout: Any, index: int, section: MaterialSection
) -> None:
    slide = prs.slides.add_slide(layout)
    _add_header(slide, f"{index}. {section.judul}")

    body = slide.shapes.add_textbox(_MARGIN_X, Inches(1.5), _CONTENT_W, Inches(5.3))
    tf = body.text_frame
    tf.word_wrap = True
    first = True

    total_items = len(section.paragraf) + len(section.poin)
    font_size = Pt(15) if total_items > 6 else Pt(17)

    for paragraf in section.paragraf:
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.text = paragraf
        p.font.name = _FONT
        p.font.size = font_size
        p.font.color.rgb = _INK
        p.space_after = Pt(10)

    for poin in section.poin:
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.text = f"•  {poin}"
        p.font.name = _FONT
        p.font.size = font_size
        p.font.color.rgb = _INK
        p.space_after = Pt(7)


def _add_table_slide(
    prs: Any, layout: Any, section_title: str, table_spec: TableSpec
) -> None:
    rows_per_slide = 10
    chunks = [
        table_spec.baris[i : i + rows_per_slide]
        for i in range(0, max(1, len(table_spec.baris)), rows_per_slide)
    ]

    for page_idx, rows_data in enumerate(chunks, start=1):
        slide = prs.slides.add_slide(layout)
        base_judul = table_spec.judul or f"Tabel — {section_title}"
        judul = (
            f"{base_judul} ({page_idx}/{len(chunks)})"
            if len(chunks) > 1
            else base_judul
        )
        _add_header(slide, judul, subtitle=section_title)

        n_rows = 1 + len(rows_data)
        n_cols = len(table_spec.kolom)

        table_height = min(Inches(5.2), Inches(0.55 * n_rows))
        shape = slide.shapes.add_table(
            n_rows, n_cols, _MARGIN_X, Inches(1.55), _CONTENT_W, table_height
        )
        table = shape.table

        for col_idx, header_text in enumerate(table_spec.kolom):
            cell = table.cell(0, col_idx)
            cell.fill.solid()
            cell.fill.fore_color.rgb = _ACCENT
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            p = cell.text_frame.paragraphs[0]
            p.text = str(header_text)
            p.font.name = _FONT
            p.font.size = Pt(14)
            p.font.bold = True
            p.font.color.rgb = _WHITE

        for row_idx, row in enumerate(rows_data, start=1):
            bg = _SOFT if row_idx % 2 == 0 else _WHITE
            for col_idx in range(n_cols):
                cell = table.cell(row_idx, col_idx)
                cell.fill.solid()
                cell.fill.fore_color.rgb = bg
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                p = cell.text_frame.paragraphs[0]
                p.text = str(row[col_idx]) if col_idx < len(row) else ""
                p.font.name = _FONT
                p.font.size = Pt(13)
                p.font.color.rgb = _INK


def _add_diagram_slide(prs: Any, layout: Any, diagram: DiagramSpec) -> None:
    slide = prs.slides.add_slide(layout)
    judul = diagram.judul or "Diagram Alur Materi"
    jenis_label = "Peta Pikiran" if diagram.jenis == "peta_pikiran" else "Diagram Alur"
    _add_header(slide, judul, subtitle=jenis_label)

    nodes = diagram.nodes
    cols = min(4, max(2, len(nodes)))
    rows = (len(nodes) + cols - 1) // cols

    gap_x = Inches(0.35)
    gap_y = Inches(0.35)
    box_w = (_CONTENT_W - gap_x * (cols - 1)) / cols
    box_h = min(Inches(1.15), (Inches(3.8) - gap_y * max(0, rows - 1)) / max(1, rows))

    label_by_id = {node.id: node.label for node in nodes}

    for idx, node in enumerate(nodes):
        row = idx // cols
        col = idx % cols
        left = _MARGIN_X + col * (box_w + gap_x)
        top = Inches(1.6) + row * (box_h + gap_y)

        shape = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, left, top, box_w, box_h
        )
        shape.fill.solid()
        shape.fill.fore_color.rgb = _ACCENT_SOFT
        shape.line.color.rgb = _ACCENT
        shape.line.width = Pt(1.5)

        tf = shape.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        p.text = node.label
        p.font.name = _FONT
        p.font.size = Pt(14)
        p.font.bold = True
        p.font.color.rgb = _INK

    if diagram.edges:
        edges_box = slide.shapes.add_textbox(
            _MARGIN_X, Inches(5.65), _CONTENT_W, Inches(1.25)
        )
        tf = edges_box.text_frame
        tf.word_wrap = True
        parts = []
        for edge in diagram.edges[:8]:
            dari = label_by_id.get(edge.dari, edge.dari)
            ke = label_by_id.get(edge.ke, edge.ke)
            tengah = f" ({edge.label})" if edge.label else ""
            parts.append(f"{dari} →{tengah} {ke}")
        p = tf.paragraphs[0]
        p.text = "Alur: " + "  |  ".join(parts)
        p.font.name = _FONT
        p.font.size = Pt(13)
        p.font.color.rgb = _MUTED


def _add_illustration_slide(
    prs: Any, layout: Any, image: IllustrationImage
) -> None:
    slide = prs.slides.add_slide(layout)
    _add_header(slide, image.judul or "Ilustrasi")

    try:
        with PilImage.open(io.BytesIO(image.image)) as pil_img:
            width_px, height_px = pil_img.size
            png_buf = io.BytesIO()
            pil_img.convert("RGB").save(png_buf, format="PNG")
            png_buf.seek(0)
    except Exception:
        return

    max_w = Inches(10.5)
    max_h = Inches(4.7)
    aspect = width_px / max(1, height_px)

    if max_w / max_h > aspect:
        img_h = max_h
        img_w = int(max_h * aspect)
    else:
        img_w = max_w
        img_h = int(max_w / aspect)

    left = int((_SLIDE_W - img_w) / 2)
    top = Inches(1.55) + int((max_h - img_h) / 2)
    slide.shapes.add_picture(png_buf, left, top, width=img_w, height=img_h)

    if image.keterangan:
        cap = slide.shapes.add_textbox(
            _MARGIN_X, Inches(6.4), _CONTENT_W, Inches(0.55)
        )
        tf = cap.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        p.text = image.keterangan
        p.font.name = _FONT
        p.font.size = Pt(13)
        p.font.italic = True
        p.font.color.rgb = _MUTED


def _add_conclusion_slide(
    prs: Any, layout: Any, kesimpulan: list[str]
) -> None:
    slide = prs.slides.add_slide(layout)
    _add_header(slide, "Kesimpulan")

    card = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE,
        _MARGIN_X,
        Inches(1.6),
        _CONTENT_W,
        Inches(5.0),
    )
    card.fill.solid()
    card.fill.fore_color.rgb = _SOFT
    card.line.color.rgb = _RULE

    tf = card.text_frame
    tf.word_wrap = True
    tf.margin_left = Inches(0.45)
    tf.margin_right = Inches(0.45)
    tf.margin_top = Inches(0.35)

    for idx, poin in enumerate(kesimpulan):
        p = tf.paragraphs[0] if idx == 0 else tf.add_paragraph()
        p.text = f"{idx + 1}.  {poin}"
        p.font.name = _FONT
        p.font.size = Pt(18)
        p.font.color.rgb = _INK
        p.space_after = Pt(12)


def _add_slide_numbers(prs: Any) -> None:
    """Tambahkan nomor halaman halus di kanan bawah mulai slide ke-2."""
    total = len(prs.slides)
    for number, slide in enumerate(prs.slides, start=1):
        if number == 1:
            continue
        box = slide.shapes.add_textbox(
            _SLIDE_W - Inches(1.6), _SLIDE_H - Inches(0.48), Inches(0.9), Inches(0.3)
        )
        p = box.text_frame.paragraphs[0]
        p.alignment = PP_ALIGN.RIGHT
        p.text = f"{number} / {total}"
        p.font.name = _FONT
        p.font.size = Pt(10)
        p.font.color.rgb = _MUTED
