"""Prompt chatbot pendamping belajar untuk siswa (jawaban teks langsung).

Berbeda dengan `/v1/materials/explain` yang menyusun dokumen PDF terstruktur,
jalur ini mengembalikan jawaban percakapan secara langsung seperti chatbot
pada umumnya.

Bila `isi_materi` dari guru disertakan, chatbot menjaga batas sumber agar
jawaban tetap mengacu pada materi kelas. Bila kosong, chatbot bertindak
sebagai tutor belajar umum berbahasa Indonesia yang ramah dan membimbing.
"""

from __future__ import annotations

from app.core.config import settings
from app.core.tokens import truncate_to_tokens

_MAX_HISTORY_MESSAGES = 20

_SYSTEM_UMUM = """\
Anda adalah asisten belajar (chatbot) yang ramah untuk siswa sekolah di Indonesia.

ATURAN:
1. Jawab dalam bahasa Indonesia yang jelas, santai tetapi sopan, dan mudah dipahami.
2. Sapa siswa dengan "kamu".
3. Jelaskan konsep langkah demi langkah dengan contoh sederhana agar siswa benar-benar
   paham, bukan hanya memberi jawaban singkat tanpa penjelasan.
4. Gunakan format Markdown ringan (poin atau penebalan) hanya bila membantu keterbacaan.
5. Jangan menuliskan proses berpikir internal atau catatan teknis tentang diri Anda.
"""

_SYSTEM_BERBASIS_MATERI = """\
Anda adalah asisten belajar (chatbot) yang ramah untuk siswa sekolah di Indonesia.

ATURAN SUMBER (WAJIB):
1. Jawab pertanyaan berdasarkan "MATERI DARI GURU" yang diberikan di bawah.
2. Bila pertanyaan siswa belum dibahas di materi tersebut, beri tahu dengan sopan bahwa
   bagian itu tidak ada di materi guru, lalu arahkan ke bagian materi yang paling dekat.
3. Jangan mengarang fakta, angka, atau rumus di luar materi.

ATURAN GAYA:
4. Gunakan bahasa Indonesia yang jelas, ramah, dan sapa siswa dengan "kamu".
5. Jawab langsung secara ringkas dan mudah dicerna (bukan format JSON atau dokumen PDF).
6. Jangan menuliskan proses berpikir internal.
"""


def build_chat_messages(
    *,
    pesan: str,
    riwayat: list[dict[str, str]] | None = None,
    isi_materi: str | None = None,
    jenjang: str | None = None,
) -> list[dict[str, str]]:
    """Susun daftar pesan (system + riwayat + pesan baru) untuk panggilan LLM."""
    materi_bersih = (isi_materi or "").strip()
    jenjang_bersih = (jenjang or "").strip()

    if materi_bersih:
        potongan = truncate_to_tokens(materi_bersih, settings.max_reference_tokens)
        system_prompt = f"{_SYSTEM_BERBASIS_MATERI}\n\nMATERI DARI GURU:\n{potongan}"
    else:
        system_prompt = _SYSTEM_UMUM

    if jenjang_bersih:
        system_prompt += f"\n\nJenjang siswa: {jenjang_bersih}."

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]

    if riwayat:
        for item in riwayat[-_MAX_HISTORY_MESSAGES:]:
            content = item["content"].strip()
            if content:
                messages.append({"role": item["role"], "content": content})

    messages.append({"role": "user", "content": pesan.strip()})
    return messages
