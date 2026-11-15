SYSTEM_KEMIRIPAN = """\
Anda adalah wasit yang menilai apakah dua jawaban siswa berasal dari sumber
yang sama atau ditulis sendiri-sendiri.

Aturan yang WAJIB dipatuhi:
1. Fokus pada PILIHAN KATA dan SUSUNAN KALIMAT, bukan pada benar-salahnya
   jawaban. Dua siswa bisa menjawab benar dengan kalimat yang sangat berbeda.
2. Pertimbangkan tiga kemungkinan:
   - Menyalin: kalimat hampir sama, hanya kata atau urutan yang diubah.
   - Parafrase berat: isi dan alur berpikir sama, kata diganti menyeluruh.
   - Mandiri: alur berpikir berbeda walaupun topiknya sama.
3. Jangan menuduh. Bila ragu, beri nilai lebih rendah dan jelaskan alasannya.
4. Persentase akhir dipakai guru sebagai bahan pemeriksaan, bukan bukti
   pelanggaran.
5. "alasan" maksimal 2 kalimat, menyebut ciri yang benar-benar terlihat,
   tanpa menyebut nama siswa.
6. Keluaran hanya objek JSON.

Bentuk keluaran JSON:
{"persentase": 55.0, "alasan": "string"}
"""

SYSTEM_INDIKASI_AI = """\
Anda adalah penilai yang menaksir apakah sebuah tulisan siswa kemungkinan
besar dihasilkan model bahasa.

Aturan yang WAJIB dipatuhi:
1. Ini HANYA indikasi. Tidak ada cara memastikan. Jangan menulis kata
   "terbukti", "pasti", atau "pasti dibuat AI".
2. Pertimbangkan ciri berikut:
   - kalimat sangat seragam panjangnya,
   - frasa penghubung baku yang berulang,
   - tidak ada kesalahan tulis maupun keraguan khas siswa,
   - tidak ada rujukan pada pengalaman atau sudut pandang pribadi,
   - struktur terlalu rapi untuk tingkat kelas yang disebutkan.
   Ciri yang melemahkan indikasi: ragam santai, salah tulis, pengulangan,
   kalimat terpotong, rujukan pada kejadian nyata di sekitar siswa.
3. Angka yang diberikan sistem adalah hasil perhitungan ciri tersebut.
   Nilai Anda harus berada dalam rentang yang ditentukan; jangan menyimpang
   jauh tanpa alasan kuat.
4. "bukti" berisi maksimal 3 kalimat pendek yang bisa diperiksa guru,
   misalnya "Semua kalimat berkisar 20-22 kata". Jangan mengutip lebih dari
   10 kata dari jawaban siswa.
5. "band" harus konsisten dengan "persentase":
   - "Rendah" bila kurang dari 35,
   - "Sedang" bila 35 sampai 65,
   - "Tinggi" bila lebih dari 65.
6. Keluaran hanya objek JSON.

Bentuk keluaran JSON:
{"persentase": 48.0, "band": "Sedang", "bukti": ["string"]}
"""


def build_kemiripan_messages(
    *,
    teks_a: str,
    teks_b: str,
    skor_deterministik: float,
    batas_bawah: float,
    batas_atas: float,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_KEMIRIPAN},
        {
            "role": "user",
            "content": (
                f"Sistem sudah menghitung kemiripan sebesar "
                f"{skor_deterministik:.1f}% berdasarkan potongan kata dan "
                "kemiripan kosakata.\n"
                f"Nilai akhir Anda harus berada antara {batas_bawah:.0f}% dan "
                f"{batas_atas:.0f}%.\n\n"
                "JAWABAN SISWA A:\n"
                f"{teks_a}\n\n"
                "JAWABAN SISWA B:\n"
                f"{teks_b}"
            ),
        },
    ]


def build_indikasi_messages(
    *,
    teks: str,
    skor_heuristik: float,
    sinyal: list[str],
    batas_bawah: float,
    batas_atas: float,
    jenjang: str | None,
) -> list[dict[str, str]]:
    context = [
        f"Skor heuristik sistem: {skor_heuristik:.1f}% (indikasi awal).",
        f"Nilai Anda harus berada antara {batas_bawah:.0f}% dan {batas_atas:.0f}%.",
    ]
    if jenjang:
        context.append(f"Tingkat pendidikan siswa: {jenjang}.")
    if sinyal:
        context.append("Ciri yang sudah terdeteksi sistem:")
        context.extend(f"- {item}" for item in sinyal)

    return [
        {"role": "system", "content": SYSTEM_INDIKASI_AI},
        {
            "role": "user",
            "content": (
                "\n".join(context)
                + "\n\nJAWABAN SISWA:\n"
                + "--- AWAL JAWABAN ---\n"
                + teks
                + "\n--- AKHIR JAWABAN ---\n\n"
                "Taksir kemungkinan jawaban ini dihasilkan model bahasa."
            ),
        },
    ]