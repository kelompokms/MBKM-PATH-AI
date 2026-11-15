"""Tata letak diagram, dihitung sebagai matematika murni.

Modul ini TIDAK menggambar apa pun. Ia menerima spesifikasi diagram dari
model dan mengembalikan posisi kotak serta garis dalam satuan poin. Pemisahan
ini membuat tata letak bisa diuji tanpa membuka PDF, dan membuat penggambar
di lapisan `render` cukup menyalin koordinat.

Menggambar langsung di kanvas PDF (bukan merender SVG lalu dirasterisasi)
dipilih karena tiga alasan: hasilnya vektor sehingga tajam saat dicetak,
tidak memerlukan pustaka grafis sistem, dan tinggi diagram bisa dihitung
sebelum halaman disusun sehingga pagination tidak pernah terpotong.

Hanya dua bentuk yang didukung — alur dan peta pikiran — karena spesifikasi
membatasi jumlah node dan bentuknya memang dua itu. Grafik bebas akan
memerlukan mesin tata letak tersendiri, dan itu bukan masalah yang perlu
dipecahkan di sini.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from reportlab.pdfbase.pdfmetrics import stringWidth

from app.domain.schemas import DiagramNode, DiagramSpec

_FONT = "Helvetica"
_FONT_SIZE = 8.5
_LINE_HEIGHT = 11.0
_H_PADDING = 9.0
_V_PADDING = 6.0
_MIN_BOX_WIDTH = 78.0
_MAX_TEXT_WIDTH = 132.0
_MAX_LINES = 3
_ROW_GAP = 26.0
_COL_GAP = 12.0


@dataclass
class Box:
    key: str
    lines: list[str]
    x: float
    y: float
    width: float
    height: float

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2


@dataclass
class Link:
    source: str
    target: str
    label: str
    start: tuple[float, float]
    end: tuple[float, float]


@dataclass
class DiagramLayout:
    width: float
    height: float
    boxes: list[Box] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    judul: str = ""

    def box(self, key: str) -> Box | None:
        for item in self.boxes:
            if item.key == key:
                return item
        return None


def wrap_label(text: str, max_width: float) -> list[str]:
    """Bungkus label menjadi baris yang muat di dalam kotak."""
    words = (text or "").split()
    if not words:
        return [""]

    lines: list[str] = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip()
        if stringWidth(candidate, _FONT, _FONT_SIZE) <= max_width or not current:
            current = candidate
            continue
        lines.append(current)
        current = word

        # Kata tunggal yang lebih lebar dari kotak: potong paksa.
        while stringWidth(current, _FONT, _FONT_SIZE) > max_width and len(current) > 1:
            cut = len(current)
            while cut > 1 and stringWidth(current[:cut], _FONT, _FONT_SIZE) > max_width:
                cut -= 1
            lines.append(current[:cut])
            current = current[cut:]

    if current:
        lines.append(current)

    if len(lines) > _MAX_LINES:
        lines = lines[:_MAX_LINES]
        last = lines[-1]
        while last and stringWidth(f"{last}…", _FONT, _FONT_SIZE) > max_width:
            last = last[:-1]
        lines[-1] = f"{last}…"

    return lines


def _measure(lines: list[str]) -> tuple[float, float]:
    widest = max(
        (stringWidth(line, _FONT, _FONT_SIZE) for line in lines), default=40.0
    )
    width = max(
        _MIN_BOX_WIDTH,
        min(widest + 2 * _H_PADDING, _MAX_TEXT_WIDTH + 2 * _H_PADDING),
    )
    height = len(lines) * _LINE_HEIGHT + 2 * _V_PADDING
    return width, height


def _levels(spec: DiagramSpec) -> list[list[str]]:
    """Kelompokkan node per tingkat dengan lintasan terpanjang.

    Salah satu node selalu jadi akar: node tanpa sisi masuk. Bila seluruh
    node membentuk lingkaran, node pertama dipakai sebagai akar agar diagram
    tetap tampil daripada gagal.
    """
    order = [node.id for node in spec.nodes]
    incoming: dict[str, int] = {key: 0 for key in order}
    outgoing: dict[str, list[str]] = {key: [] for key in order}

    for edge in spec.edges:
        incoming[edge.ke] += 1
        outgoing[edge.dari].append(edge.ke)

    roots = [key for key in order if incoming[key] == 0]
    if not roots:
        roots = [order[0]]

    level: dict[str, int] = {key: 0 for key in order}
    frontier = list(roots)

    # Batasi iterasi: graf dengan siklus tidak boleh membuat ini berputar.
    for _ in range(len(order) + 1):
        next_frontier: list[str] = []
        changed = False
        for key in frontier:
            for target in outgoing[key]:
                if level[target] < level[key] + 1:
                    level[target] = level[key] + 1
                    next_frontier.append(target)
                    changed = True
        if not changed:
            break
        frontier = next_frontier

    depth = max(level.values()) + 1
    rows: list[list[str]] = [[] for _ in range(depth)]
    for key in order:
        rows[level[key]].append(key)
    return [row for row in rows if row]


def _node_sizes(spec: DiagramSpec) -> dict[str, tuple[list[str], float, float]]:
    """Bungkus label dan ukur kotak untuk setiap node diagram alur."""
    sizes: dict[str, tuple[list[str], float, float]] = {}
    for node in spec.nodes:
        lines = wrap_label(node.label, _MAX_TEXT_WIDTH)
        width, height = _measure(lines)
        sizes[node.id] = (lines, width, height)
    return sizes


def _flow_row(
    row: list[str],
    sizes: dict[str, tuple[list[str], float, float]],
    cursor_y: float,
    max_width: float,
) -> tuple[list[Box], float, float]:
    """Susun satu baris: kembalikan kotak, tinggi baris, dan lebar terpakai."""
    widths = [sizes[key][1] for key in row]
    heights = [sizes[key][2] for key in row]
    row_height = max(heights)
    total = sum(widths) + _COL_GAP * (len(row) - 1)
    start_x = max(0.0, (max_width - total) / 2)

    boxes: list[Box] = []
    for key, width, height in zip(row, widths, heights, strict=True):
        boxes.append(
            Box(
                key=key,
                lines=sizes[key][0],
                x=start_x,
                y=cursor_y + (row_height - height) / 2,
                width=width,
                height=height,
            )
        )
        start_x += width + _COL_GAP

    return boxes, row_height, total


def _add_flow_links(layout: DiagramLayout, spec: DiagramSpec) -> None:
    """Gambar garis antar kotak sesuai sisi, dari bawah sumber ke atas tujuan."""
    for edge in spec.edges:
        source = layout.box(edge.dari)
        target = layout.box(edge.ke)
        if source is None or target is None:
            continue
        layout.links.append(
            Link(
                source=edge.dari,
                target=edge.ke,
                label=edge.label,
                start=(source.center_x, source.y + source.height),
                end=(target.center_x, target.y),
            )
        )


def _layout_flow(spec: DiagramSpec, max_width: float) -> DiagramLayout:
    """Tata letak alur: node disusun baris per baris mengikuti tingkatnya."""
    rows = _levels(spec)
    sizes = _node_sizes(spec)

    boxes: list[Box] = []
    cursor_y = 0.0
    used_width = 0.0

    for row in rows:
        row_boxes, row_height, total = _flow_row(row, sizes, cursor_y, max_width)
        boxes.extend(row_boxes)
        used_width = max(used_width, total)
        cursor_y += row_height + _ROW_GAP

    height = max(0.0, cursor_y - _ROW_GAP)

    layout = DiagramLayout(
        width=min(max_width, used_width),
        height=height,
        boxes=boxes,
        judul=spec.judul,
    )
    _add_flow_links(layout, spec)
    return layout


def _mindmap_ring_sizes(others: list[DiagramNode]) -> list[tuple[list[str], float, float]]:
    """Bungkus label dan ukur kotak untuk setiap satelit peta pikiran."""
    ring: list[tuple[list[str], float, float]] = []
    for node in others:
        lines = wrap_label(node.label, _MAX_TEXT_WIDTH - 20)
        box_w, box_h = _measure(lines)
        ring.append((lines, box_w, box_h))
    return ring


def _ring_boxes(
    others: list[DiagramNode],
    ring: list[tuple[list[str], float, float]],
    cx: float,
    cy: float,
    radius_x: float,
    radius_y: float,
) -> list[Box]:
    """Sebar kotak satelit melingkar, mulai dari atas lalu searah jarum jam."""
    boxes: list[Box] = []
    for index, (node, (lines, box_w, box_h)) in enumerate(zip(others, ring, strict=True)):
        angle = -math.pi / 2 + (2 * math.pi * index / max(1, len(others)))
        x = cx + math.cos(angle) * radius_x - box_w / 2
        y = cy + math.sin(angle) * radius_y - box_h / 2
        boxes.append(
            Box(key=node.id, lines=lines, x=x, y=y, width=box_w, height=box_h)
        )
    return boxes


def _add_mindmap_links(layout: DiagramLayout, spec: DiagramSpec, center_id: str) -> None:
    """Gambar garis dari pusat ke satelit, lalu sisi antar-satelitnya."""
    hub = layout.box(center_id)
    if hub is None:
        return
    for edge in spec.edges:
        side = edge.ke if edge.dari == center_id else edge.dari
        target = layout.box(side)
        if target is None or target.key == center_id:
            continue
        layout.links.append(
            Link(
                source=center_id,
                target=target.key,
                label=edge.label,
                start=_edge_point(hub, target),
                end=_edge_point(target, hub),
            )
        )

    # Sisi antar-node yang bukan dari pusat tetap digambar.
    for edge in spec.edges:
        if edge.dari == center_id:
            continue
        source = layout.box(edge.dari)
        target = layout.box(edge.ke)
        if source is None or target is None:
            continue
        layout.links.append(
            Link(
                source=edge.dari,
                target=edge.ke,
                label=edge.label,
                start=_edge_point(source, target),
                end=_edge_point(target, source),
            )
        )


def _layout_mindmap(spec: DiagramSpec, max_width: float) -> DiagramLayout:
    """Tata letak peta pikiran: satu kotak pusat dan satelit di sekelilingnya."""
    nodes = spec.nodes
    center = nodes[0]
    others = nodes[1:]

    center_lines = wrap_label(center.label, _MAX_TEXT_WIDTH)
    center_w, center_h = _measure(center_lines)
    ring = _mindmap_ring_sizes(others)

    max_box_w = max((item[1] for item in ring), default=center_w)
    max_box_h = max((item[2] for item in ring), default=center_h)

    radius_x = max(max_width / 2 - max_box_w / 2 - 6, 70.0)
    radius_y = max(96.0, 46.0 + 20.0 * len(others) + max_box_h)

    width = min(max_width, 2 * radius_x + max_box_w)
    height = 2 * radius_y + max_box_h
    cx = width / 2
    cy = height / 2

    boxes: list[Box] = [
        Box(
            key=center.id,
            lines=center_lines,
            x=cx - center_w / 2,
            y=cy - center_h / 2,
            width=center_w,
            height=center_h,
        )
    ]
    boxes.extend(_ring_boxes(others, ring, cx, cy, radius_x, radius_y))

    layout = DiagramLayout(width=width, height=height, boxes=boxes, judul=spec.judul)
    _add_mindmap_links(layout, spec, center.id)
    return layout


def _edge_point(box: Box, target: Box) -> tuple[float, float]:
    """Titik di tepi kotak yang menghadap kotak lain."""
    dx = target.center_x - box.center_x
    dy = target.center_y - box.center_y
    if dx == 0 and dy == 0:
        return box.center_x, box.center_y

    half_w = box.width / 2
    half_h = box.height / 2
    scale = min(
        half_w / abs(dx) if dx else float("inf"),
        half_h / abs(dy) if dy else float("inf"),
    )
    return box.center_x + dx * scale, box.center_y + dy * scale


def build_layout(spec: DiagramSpec, max_width: float) -> DiagramLayout:
    """Hitung tata letak diagram sesuai jenisnya."""
    if spec.jenis == "peta_pikiran":
        return _layout_mindmap(spec, max_width)
    return _layout_flow(spec, max_width)
