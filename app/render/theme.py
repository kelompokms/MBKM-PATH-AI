"""Palet dan gaya tipografi dokumen.

Semua warna dan ukuran huruf ada di sini supaya materi dan laporan terlihat
berasal dari sistem yang sama. Font yang dipakai adalah font bawaan
reportlab (Helvetica), sehingga hasil di Windows dan di container Linux
identik tanpa perlu memasang font sistem.
"""

from __future__ import annotations

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle

# --- Palet ----------------------------------------------------------------
INK = colors.HexColor("#1A2233")
MUTED = colors.HexColor("#5A6472")
ACCENT = colors.HexColor("#2B5F8C")
ACCENT_SOFT = colors.HexColor("#E8F0F7")
RULE = colors.HexColor("#C9D3DF")
SOFT = colors.HexColor("#F4F6F9")
WHITE = colors.HexColor("#FFFFFF")

FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
FONT_ITALIC = "Helvetica-Oblique"

# --- Ukuran halaman -------------------------------------------------------
PAGE_MARGIN = 52.0  # ≈ 1.8 cm
PAGE_WIDTH = 595.27  # A4
PAGE_HEIGHT = 841.89
CONTENT_WIDTH = PAGE_WIDTH - 2 * PAGE_MARGIN

# --- Gaya paragraf --------------------------------------------------------
TITLE = ParagraphStyle(
    "title",
    fontName=FONT_BOLD,
    fontSize=17,
    leading=21,
    textColor=INK,
    spaceAfter=6,
    alignment=TA_LEFT,
)

SUBTITLE = ParagraphStyle(
    "subtitle",
    fontName=FONT,
    fontSize=10,
    leading=15,
    textColor=MUTED,
    spaceAfter=2,
)

SECTION = ParagraphStyle(
    "section",
    fontName=FONT_BOLD,
    fontSize=12.5,
    leading=16,
    textColor=ACCENT,
    spaceBefore=14,
    spaceAfter=6,
)

BODY = ParagraphStyle(
    "body",
    fontName=FONT,
    fontSize=10,
    leading=15,
    textColor=INK,
    spaceAfter=6,
)

BULLET = ParagraphStyle(
    "bullet",
    parent=BODY,
    leftIndent=14,
    bulletIndent=4,
    spaceAfter=3,
)

CAPTION = ParagraphStyle(
    "caption",
    fontName=FONT_ITALIC,
    fontSize=8.5,
    leading=11,
    textColor=MUTED,
    spaceBefore=3,
    spaceAfter=10,
)

SMALL = ParagraphStyle(
    "small",
    fontName=FONT,
    fontSize=8.5,
    leading=11.5,
    textColor=MUTED,
)

TABLE_HEADER = ParagraphStyle(
    "table_header",
    fontName=FONT_BOLD,
    fontSize=8.5,
    leading=11,
    textColor=WHITE,
)

TABLE_HEADER_CENTER = ParagraphStyle(
    "table_header_center",
    parent=TABLE_HEADER,
    alignment=1,
)

TABLE_CELL = ParagraphStyle(
    "table_cell",
    fontName=FONT,
    fontSize=8.5,
    leading=11,
    textColor=INK,
)

TABLE_CELL_CENTER = ParagraphStyle(
    "table_cell_center",
    parent=TABLE_CELL,
    alignment=1,
)

# Untuk kolom berisi teks panjang tanpa spasi (mis. alamat email).
# Pada 8.5 pt, "budi.santoso@sekolah.id" butuh ±130 pt dan akan terpotong
# di tengah alamat pada lebar kolom yang wajar. Satu setengah poin lebih
# kecil menyelesaikannya tanpa mengubah tata letak keseluruhan.
TABLE_CELL_TINY = ParagraphStyle(
    "table_cell_tiny",
    fontName=FONT,
    fontSize=7.5,
    leading=10,
    textColor=INK,
)
