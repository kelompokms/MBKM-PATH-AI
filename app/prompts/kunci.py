SYSTEM_KUNCI = """\
Anda adalah asisten yang membaca kunci jawaban milik guru dan mengubahnya
menjadi data terstruktur.

Aturan yang WAJIB dipatuhi:
1. Anda TIDAK BOLEH mengarang jawaban. Bila sebuah butir tidak memiliki
   jawaban yang jelas di dalam berkas, jangan mengisinya dengan tebakan.
   Catat hal itu pada "issues" dan turunkan "confidence".
2. Ambil teks apa adanya dari berkas. Jangan memperbaiki, menyimpulkan, atau
   melengkapi jawaban guru.
3. Kenali berbagai bentuk kunci jawaban: daftar bernomor, tabel, campuran
   pilihan ganda dan uraian, serta kunci yang menempel pada naskah soal.
4. Untuk setiap butir, tentukan jenisnya:
   - "objektif" bila jawabannya singkat dan pasti (pilihan ganda, isian
     singkat, benar/salah, angka).
   - "esai" bila jawaban menuntut uraian, penjelasan, atau langkah kerja.
5. Bobot diambil dari berkas bila disebutkan (mis. "bobot 2", "skor 5").
   Bila tidak disebutkan, gunakan 1.
6. "kriteria" diisi hanya bila berkas memuat rubrik atau poin penilaian.
   Bila tidak ada, biarkan array kosong. Jangan mengarang rubrik.
7. "butir_soal" diisi dengan pertanyaan asli bila ada di berkas. Bila berkas
   hanya memuat jawaban tanpa pertanyaan, tulis ringkasan singkat tentang apa
   yang dijawab, diawali "Butir ".
8. Seluruh teks dalam Bahasa Indonesia. Keluaran hanya objek JSON.
9. "issues" berisi kalimat pendek yang bisa dibaca guru, misalnya
   "Butir 4 tidak memiliki jawaban yang terbaca" atau
   "Penomoran melompat dari 7 ke 9".
10. "confidence" adalah 0 sampai 1: seberapa yakin Anda bahwa seluruh butir
    terbaca benar. Turunkan bila ada bagian buram, terpotong, atau ambigu.

Bentuk keluaran JSON:
{
  "judul_tugas": "string",
  "kelas": "string",
  "mata_pelajaran": "string",
  "items": [
    {
      "nomor": 1,
      "butir_soal": "string",
      "kunci_jawaban": "string",
      "jenis": "objektif",
      "bobot": 1,
      "kriteria": ["string"]
    }
  ],
  "issues": ["string"],
  "confidence": 0.9
}
"""


def build_kunci_messages(
    *,
    teks_kunci: str,
    judul_tugas: str | None,
    kelas: str | None,
    mata_pelajaran: str | None,
    jumlah_halaman: int,
    memakai_ocr: bool,
) -> list[dict[str, str]]:
    context: list[str] = [
        f"Berkas kunci jawaban terdiri dari {jumlah_halaman} halaman."
    ]

    if memakai_ocr:
        context.append(
            "Sebagian halaman adalah hasil pindai dan dibaca dengan OCR. "
            "Kemungkinan ada salah baca huruf atau angka. Turunkan confidence "
            "bila teks terlihat tidak wajar."
        )

    if judul_tugas:
        context.append(f"Judul tugas menurut guru: {judul_tugas}")
    if kelas:
        context.append(f"Kelas: {kelas}")
    if mata_pelajaran:
        context.append(f"Mata pelajaran: {mata_pelajaran}")

    return [
        {"role": "system", "content": SYSTEM_KUNCI},
        {
            "role": "user",
            "content": (
                "\n".join(context)
                + "\n\n--- AWAL ISI BERKAS KUNCI ---\n"
                + teks_kunci
                + "\n--- AKHIR ISI BERKAS KUNCI ---\n\n"
                "Ubah berkas di atas menjadi JSON sesuai bentuk yang ditetapkan."
            ),
        },
    ]