from __future__ import annotations

from app.prompts.materi_siswa import SYSTEM_MATERI_SISWA

SYSTEM_MATERI = """\
Anda adalah penulis materi ajar untuk sekolah di Indonesia.

Aturan yang WAJIB dipatuhi:
1. Seluruh isi ditulis dalam Bahasa Indonesia yang baku, jelas, dan sesuai
   jenjang pendidikan yang disebutkan.
2. Anda hanya boleh menuliskan pengetahuan yang benar-benar Anda yakini.
   Jangan mengarang angka, tahun, nama penemu, rumus, atau kutipan. Bila ragu,
   tulis penjelasan yang lebih umum daripada menyebutkan detail yang salah.
3. Jangan menuliskan proses berpikir, permintaan maaf, atau catatan tentang
   dirinya sendiri. Keluaran hanya objek JSON.
4. Struktur materi harus runtut: pengantar, penjelasan bertahap, lalu
   kesimpulan yang merangkum butir-butir utama.
5. Tentukan sendiri jumlah bagian, panjang paragraf, jumlah kalimat, serta
   poin-poin secara bebas mengikuti kebutuhan topik dan permintaan pengguna.
   Bila bahan rujukan yang diberikan panjang, bahas seluruh isinya dari awal
   sampai akhir, bukan hanya pengantarnya. Jangan menghabiskan ruang untuk
   menyebutkan judul, penulis, atau penerbit dokumennya.
6. Bila pengguna meminta tabel, buat tabel sebanyak dan selengkap yang sesuai
   dengan materi dan permintaan pengguna. Isi sel berupa teks yang rapi dan
   mudah dibaca.
7. Bila pengguna meminta diagram, pilih "alur" untuk proses/tahapan dan
   "peta_pikiran" untuk konsep yang bercabang. Tentukan jumlah node dan sisi
   secukupnya sesuai kompleksitas materi. Setiap node memiliki "id" pendek
   tanpa spasi (huruf kecil, mis. "tahap1") dan "label" yang ringkas dan
   jelas. Sisi menghubungkan id yang benar-benar ada.
8. Bila pengguna meminta ilustrasi, buat jumlah ilustrasi yang sesuai dengan
   permintaan pengguna dan kebutuhan visual materi. Tulis "prompt_gambar"
   dalam BAHASA INGGRIS yang sangat spesifik dan relevan langsung dengan
   konsep materi yang dibahas: jelaskan objek utama, proses atau adegan
   konkret yang terjadi, detail anatomi/sains/lingkungan yang akurat,
   komposisi yang bersih dan jelas, pencahayaan terang, serta gaya ilustrasi
   buku pelajaran/ensiklopedia modern yang rapi. Jangan meminta teks, huruf,
   angka, label, atau tanda air (watermark) di dalam gambar.
9. Bila sebuah elemen tidak diminta, isi dengan null atau array kosong.
   Jangan menyisipkan elemen yang tidak diminta.
10. "deskripsi_singkat" menjelaskan cakupan dan inti materi secara jelas,
    bukan sekadar pengulangan judul.

Bentuk keluaran JSON:
{
  "judul": "string",
  "deskripsi_singkat": "string",
  "isi": [
    {
      "judul": "string",
      "paragraf": ["string"],
      "poin": ["string"],
      "tabel": {
        "judul": "string",
        "kolom": ["string", "string"],
        "baris": [["string", "string"]]
      }
    }
  ],
  "kesimpulan": ["string"],
  "diagram": {
    "jenis": "alur",
    "judul": "string",
    "nodes": [{"id": "string", "label": "string"}],
    "edges": [{"dari": "string", "ke": "string", "label": "string"}]
  },
  "ilustrasi": [
    {"judul": "string", "prompt_gambar": "string", "keterangan": "string"}
  ]
}
"""


# Urutan nama elemen di sini menentukan urutan penyebutannya pada pesan.
_ELEMENT_NAMES = ("tabel", "diagram", "ilustrasi")


def _title_instruction(judul: str | None) -> str:
    """Judul dari pengajar dipakai apa adanya; kalau tidak ada, model memilih."""
    if judul:
        return f"Buat materi ajar berjudul tepat seperti ini: \"{judul}\"."
    return (
        "Tentukan judul materi yang tepat dan spesifik, lalu jawab pertanyaan "
        "pengguna di dalam materi tersebut."
    )


def _reference_instruction(materi_rujukan: str, *, siswa: bool) -> str:
    """Sisipkan materi rujukan, dengan batas yang jelas agar isinya tidak
    tertukar dengan instruksi lain.

    Untuk pelajar, kalimatnya berbeda secara mendasar: materi itu satu-satunya
    sumber, bukan "sumber utama yang boleh dilengkapi pengetahuan umum".
    """
    if siswa:
        return (
            "MATERI DARI GURU — ini satu-satunya sumber yang boleh dipakai. "
            "Jangan menambahkan pengetahuan dari luar materi ini.\n\n"
            "--- AWAL MATERI DARI GURU ---\n"
            f"{materi_rujukan}\n"
            "--- AKHIR MATERI DARI GURU ---"
        )
    return (
        "Gunakan berkas rujukan berikut sebagai sumber utama. Bila isinya "
        "tidak memadai untuk menjawab, lengkapi dengan pengetahuan umum dan "
        "jangan mengarang isi berkas.\n\n"
        "--- AWAL BERKAS RUJUKAN ---\n"
        f"{materi_rujukan}\n"
        "--- AKHIR BERKAS RUJUKAN ---"
    )


def _student_title_instruction() -> str:
    """Pelajar tidak menentukan judul; judul mengikuti pertanyaannya."""
    return (
        "Beri judul singkat yang mencerminkan pertanyaan pelajar di bawah, "
        "lalu jawab pertanyaan itu di dalam materi tersebut."
    )


def _elements_instruction(flags: dict[str, bool]) -> str:
    """Sebutkan elemen tambahan yang diminta, atau larang semuanya."""
    requested = [name for name in _ELEMENT_NAMES if flags.get(name)]
    if requested:
        return "Elemen tambahan yang diminta: " + ", ".join(requested) + "."
    return (
        "Jangan menyertakan tabel, diagram, maupun ilustrasi. "
        "Isi cukup paragraf dan poin."
    )


def _closing_instruction() -> str:
    return (
        "Kembalikan HANYA objek JSON sesuai bentuk yang ditetapkan. "
        "Tanpa penjelasan pembuka atau penutup."
    )


def _build_instructions(
    *,
    pertanyaan: str | None,
    judul: str | None,
    deskripsi_materi: str,
    materi_rujukan: str | None,
    jenjang: str | None,
    flags: dict[str, bool],
    siswa: bool,
) -> list[str]:
    """Susun butir-butir instruksi, sesuai audiensnya."""
    instructions: list[str] = []

    if siswa:
        instructions.append(_student_title_instruction())
    else:
        instructions.append(_title_instruction(judul))

    if pertanyaan:
        if siswa:
            instructions.append(
                f"Pertanyaan pelajar yang harus dijawab: \"{pertanyaan}\""
            )
        else:
            instructions.append(f"Pertanyaan yang harus dijawab: \"{pertanyaan}\"")

    # Deskripsi bersifat opsional. Bila kosong, baris ini dilewati sama
    # sekali. Baris yang tertinggal tanpa isi ("Deskripsi materi yang
    # diminta:") terbaca seperti instruksi yang rusak, dan pada jalur pelajar
    # bisa disalahpahami sebagai materi guru yang memang kosong.
    if deskripsi_materi:
        if siswa:
            # Sengaja tidak memakai kata "materi dari guru": nama itu sudah
            # dipakai blok sumber di bawah. Bila bertabrakan, model bisa
            # mengira materi guru hanya sebatas deskripsi ini.
            instructions.append(f"Topik materi dari guru: {deskripsi_materi}")
        else:
            instructions.append(f"Deskripsi materi yang diminta: {deskripsi_materi}")

    if jenjang:
        instructions.append(f"Jenjang pendidikan: {jenjang}.")

    if materi_rujukan:
        instructions.append(_reference_instruction(materi_rujukan, siswa=siswa))

    instructions.append(_elements_instruction(flags))
    instructions.append(_closing_instruction())
    return instructions


def build_materi_messages(
    *,
    pertanyaan: str | None,
    judul: str | None,
    deskripsi_materi: str,
    materi_rujukan: str | None,
    jenjang: str | None,
    flags: dict[str, bool],
    audience: str = "teacher",
) -> list[dict[str, str]]:
    """Susun pesan untuk fitur 1 dan 2.

    Fitur 1 dan 2 memakai satu pipeline; bedanya hanya ada tidaknya judul
    yang ditentukan pengajar.

    `audience="student"` memakai prompt yang berbeda secara mendasar: pelajar
    tidak menyusun materi, ia menanyakan satu hal, dan jawabannya harus
    terkurung di dalam materi yang diberikan gurunya.
    """
    siswa = audience == "student"

    instructions = _build_instructions(
        pertanyaan=pertanyaan,
        judul=judul,
        deskripsi_materi=deskripsi_materi,
        materi_rujukan=materi_rujukan,
        jenjang=jenjang,
        flags=flags,
        siswa=siswa,
    )

    return [
        {"role": "system", "content": SYSTEM_MATERI_SISWA if siswa else SYSTEM_MATERI},
        {"role": "user", "content": "\n\n".join(instructions)},
    ]