"""Penamaan berkas hasil.

Dipakai beberapa alur untuk menamai artefak PDF dari judul dokumennya. Ditaruh
di satu tempat supaya aturannya sama di semua fitur: huruf kecil, hanya huruf
dan angka, dipisah tanda hubung, dan dibatasi panjangnya.
"""

from __future__ import annotations

import re

_MAX_SLUG_CHARS = 60


def slugify(value: str, fallback: str = "dokumen") -> str:
    """Ubah judul menjadi nama berkas yang aman di semua sistem berkas."""
    bersih = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (bersih or fallback)[:_MAX_SLUG_CHARS]
