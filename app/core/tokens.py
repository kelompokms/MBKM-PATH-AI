"""Estimasi token tanpa dependensi eksternal.

Dua kegunaan, keduanya sengaja memakai perkiraan kasar:

1. Pra-panggilan: memutuskan apakah anggaran berbayar masih cukup sebelum
   request dikirim. Kelebihan estimasi hanya membuat kita lebih hemat.
2. Ingest: menolak file jawaban siswa yang terlalu besar sebelum menyentuh
   model sama sekali.

Akuntansi yang sebenarnya TIDAK memakai modul ini. Setelah respons diterima,
angka pasti diambil dari `usage.prompt_tokens` / `usage.completion_tokens`
yang dikirim OpenRouter sendiri.
"""

from __future__ import annotations

import math

# Rasio karakter-per-token untuk teks Indonesia/Inggris campuran.
# Nilai 3.5 sengaja konservatif (sedikit melebih-lebihkan jumlah token),
# karena overestimate hanya berdampak pada penghematan, sedangkan
# underestimate bisa membuat anggaran jebol.
_CHARS_PER_TOKEN = 3.5

# Biaya tetap per pesan dalam format chat (role, separator, dsb).
_TOKENS_PER_MESSAGE = 4

# Cadangan untuk nama model + parameter yang ditambahkan server.
_REQUEST_OVERHEAD = 16


def estimate_text_tokens(text: str) -> int:
    """Perkiraan jumlah token untuk sebuah string."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))


def estimate_messages_tokens(messages: list[dict[str, object]]) -> int:
    """Perkiraan jumlah token untuk daftar pesan format chat."""
    total = _REQUEST_OVERHEAD
    for message in messages:
        total += _TOKENS_PER_MESSAGE
        content = message.get("content")
        if isinstance(content, str):
            total += estimate_text_tokens(content)
        elif isinstance(content, list):
            # Konten multimodal: hitung bagian teks saja. Bagian gambar
            # ditagih terpisah oleh provider dan tidak dihitung di sini.
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += estimate_text_tokens(part["text"])
    return total


def truncate_to_tokens(text: str, limit: int) -> str:
    """Potong teks agar mendekati batas token, memotong di batas kata."""
    if limit <= 0:
        return ""
    max_chars = int(limit * _CHARS_PER_TOKEN)
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last_space = cut.rfind(" ")
    if last_space > max_chars * 0.8:
        cut = cut[:last_space]
    return cut.rstrip() + "…"


def split_into_chunks(
    text: str, *, max_tokens: int, overlap_tokens: int = 0
) -> list[str]:
    """Bagi teks panjang menjadi beberapa bagian yang muat diproses model.

    Dokumen puluhan halaman tidak boleh dikirim sekaligus. Bukan hanya karena
    batas token: model yang menerima puluhan ribu token sekaligus cenderung
    hanya mengingat bagian awal dan akhir, sedangkan bagian tengahnya hilang.
    Memotongnya membuat setiap bagian dibaca dengan perhatian penuh.

    `overlap_tokens` mengulang sedikit ekor bagian sebelumnya di awal bagian
    berikutnya, supaya kalimat yang jatuh tepat di batas potongan tidak
    kehilangan kalimat sebelumnya.
    """
    bersih = (text or "").strip()
    if not bersih:
        return []

    batas = max(1, int(max_tokens * _CHARS_PER_TOKEN))
    tumpang = max(0, min(int(overlap_tokens * _CHARS_PER_TOKEN), batas // 2))

    if len(bersih) <= batas:
        return [bersih]

    kata = bersih.split(" ")
    bagian: list[str] = []
    sekarang: list[str] = []

    for potongan in kata:
        tambahan = len(potongan) + (1 if sekarang else 0)

        if sekarang and _panjang(sekarang) + tambahan > batas:
            bagian.append(" ".join(sekarang))
            sekarang = _ekor(sekarang, tumpang)

        sekarang.append(potongan)

    if sekarang:
        bagian.append(" ".join(sekarang))

    return bagian


def _panjang(kata: list[str]) -> int:
    """Jumlah karakter bila kata-kata itu disatukan dengan spasi."""
    if not kata:
        return 0
    return sum(len(item) for item in kata) + len(kata) - 1


def _ekor(kata: list[str], batas: int) -> list[str]:
    """Sejumlah kata terakhir yang panjangnya mendekati batas."""
    if batas <= 0:
        return []

    ekor: list[str] = []
    for potongan in reversed(kata):
        if ekor and _panjang([potongan, *ekor]) > batas:
            break
        ekor.insert(0, potongan)

    return ekor