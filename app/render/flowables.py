"""Flowable kustom dan penomoran halaman."""

from __future__ import annotations

import math
from datetime import datetime

from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import Flowable

from app.engines.diagrams import DiagramLayout
from app.render import theme

_ARROW_LENGTH = 5.5
_ARROW_WIDTH = 3.2


class DiagramFlowable(Flowable):
    """Menggambar tata letak diagram yang sudah dihitung sebelumnya."""

    def __init__(self, layout: DiagramLayout) -> None:
        super().__init__()
        self.layout = layout
        self.width = max(1.0, layout.width)
        self.height = max(1.0, layout.height) + 2

    def wrap(self, avail_width: float, avail_height: float) -> tuple[float, float]:
        return self.width, self.height

    def draw(self) -> None:
        canvas = self.canv
        canvas.saveState()
        self._draw_links(canvas)
        self._draw_boxes(canvas)
        canvas.restoreState()

    # --- bagian gambar ----------------------------------------------------

    def _flip(self, y: float, height: float = 0.0) -> float:
        """Tata letak memakai sumbu Y ke bawah; kanvas PDF ke atas."""
        return self.height - y - height

    def _draw_boxes(self, canvas: pdfcanvas.Canvas) -> None:
        for box in self.layout.boxes:
            y = self._flip(box.y, box.height)
            canvas.setFillColor(theme.ACCENT_SOFT)
            canvas.setStrokeColor(theme.ACCENT)
            canvas.setLineWidth(0.9)
            canvas.roundRect(box.x, y, box.width, box.height, 5, stroke=1, fill=1)

            canvas.setFillColor(theme.INK)
            canvas.setFont(theme.FONT, 8.5)
            text_y = y + box.height / 2 + (len(box.lines) * 11.0) / 2 - 8.8
            for line in box.lines:
                canvas.drawCentredString(box.center_x, text_y, line)
                text_y -= 11.0

    def _draw_links(self, canvas: pdfcanvas.Canvas) -> None:
        canvas.setStrokeColor(theme.MUTED)
        canvas.setLineWidth(0.8)
        canvas.setFillColor(theme.MUTED)

        for link in self.layout.links:
            sx, sy = link.start
            ex, ey = link.end
            canvas.line(sx, self._flip(sy), ex, self._flip(ey))

            angle = math.atan2(self._flip(ey) - self._flip(sy), ex - sx)
            canvas.saveState()
            canvas.translate(ex, self._flip(ey))
            canvas.rotate(math.degrees(angle))
            path = canvas.beginPath()
            path.moveTo(0, 0)
            path.lineTo(-_ARROW_LENGTH, _ARROW_WIDTH / 2)
            path.lineTo(-_ARROW_LENGTH, -_ARROW_WIDTH / 2)
            path.close()
            canvas.drawPath(path, stroke=0, fill=1)
            canvas.restoreState()

            if link.label:
                canvas.setFont(theme.FONT, 7)
                canvas.setFillColor(theme.MUTED)
                mid_x = (sx + ex) / 2
                mid_y = self._flip((sy + ey) / 2)
                label_width = canvas.stringWidth(link.label, theme.FONT, 7)
                canvas.setFillColor(theme.WHITE)
                canvas.rect(
                    mid_x - label_width / 2 - 2,
                    mid_y - 3,
                    label_width + 4,
                    8,
                    stroke=0,
                    fill=1,
                )
                canvas.setFillColor(theme.MUTED)
                canvas.drawCentredString(mid_x, mid_y, link.label)


class NumberedCanvas(pdfcanvas.Canvas):
    """Kanvas dua lintas agar bisa mencetak "Halaman X dari Y".

    Jumlah halaman baru diketahui setelah seluruh dokumen disusun, sehingga
    setiap halaman disimpan dulu, lalu dicetak ulang di akhir dengan angka
    total yang sudah pasti.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._states: list[dict] = []
        self.footer_label = "PATH-AI"
        self.document_label = ""

    def showPage(self) -> None:  # mengikuti API reportlab
        self._states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._states)
        for state in self._states:
            self.__dict__.update(state)
            self._draw_footer(total)
            pdfcanvas.Canvas.showPage(self)
        pdfcanvas.Canvas.save(self)

    def _draw_footer(self, total: int) -> None:
        width = theme.PAGE_WIDTH
        self.setStrokeColor(theme.RULE)
        self.setLineWidth(0.6)
        self.line(theme.PAGE_MARGIN, 46, width - theme.PAGE_MARGIN, 46)

        self.setFont(theme.FONT, 7.5)
        self.setFillColor(theme.MUTED)
        self.drawString(theme.PAGE_MARGIN, 34, self.footer_label)

        page_number = self._pageNumber
        self.drawRightString(
            width - theme.PAGE_MARGIN,
            34,
            f"Halaman {page_number} dari {total}",
        )

        if self.document_label:
            self.drawCentredString(width / 2, 34, self.document_label[:70])


def footer_label(with_title: str = "") -> str:
    """Teks kiri bawah: nama layanan + tanggal cetak."""
    stamp = datetime.now().strftime("%d-%m-%Y")
    base = f"PATH-AI · dicetak {stamp}"
    return f"{base} · {with_title}" if with_title else base