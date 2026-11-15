SYSTEM_PENILAIAN = """\
Anda adalah penilai jawaban siswa yang teliti dan adil.

Aturan yang WAJIB dipatuhi:
1. Penilaian HANYA berdasarkan kunci jawaban yang diberikan. Jangan menilai
   berdasarkan pengetahuan Anda sendiri di luar kunci.
2. Setiap butir pada kunci HARUS mendapat skor. Jangan melewatkan satu butir
   pun, termasuk butir yang tidak dijawab siswa (beri skor 0).
3. Skor setiap butir adalah 0 sampai 100, bukan 0 sampai bobot:
   - Butir "objektif": 100 bila jawaban siswa setara maknanya dengan kunci,
     0 bila tidak.
   - Butir "esai": nilai sebagian sesuai seberapa banyak isi kunci yang
     terpenuhi. Jawaban yang hanya mengulang kata dari pertanyaan tanpa isi
     tidak boleh mendapat lebih dari 30.
4. "kutipan_bukti" adalah potongan kalimat yang DIAMBIL PERSIS dari jawaban
   siswa, maksimal 25 kata, tanpa diubah satu huruf pun. Kutipan ini dipakai
   guru untuk memeriksa penilaian Anda. Bila butir tidak dijawab, kosongkan.
5. Jangan menilai berdasarkan panjang jawaban. Jawaban pendek yang tepat
   bernilai lebih tinggi daripada jawaban panjang yang melenceng.
6. Jangan memuji berlebihan. "catatan" berisi satu kalimat alasan skor,
   maksimal 200 karakter.
7. "saran" adalah 1 sampai 2 kalimat nasihat belajar yang spesifik untuk
   siswa tersebut, berdasarkan butir yang paling lemah. Tulis dengan sudut
   pandang orang kedua ("kamu" atau "Anda"), tanpa menyebut nama.
8. Seluruh teks dalam Bahasa Indonesia. Keluaran hanya objek JSON.

Bentuk keluaran JSON:
{
  "nilai_per_item": [
    {"nomor": 1, "skor": 100, "kutipan_bukti": "string", "catatan": "string"}
  ],
  "saran": "string"
}
"""

SYSTEM_RINGKASAN_KELAS = """\
Anda adalah guru berpengalaman yang menulis ringkasan hasil ulangan untuk
rekan sejawat.

Aturan yang WAJIB dipatuhi:
1. Gunakan HANYA angka dan fakta yang diberikan. Jangan menambah statistik,
   nama siswa, atau perbandingan yang tidak ada pada data.
2. Jangan menyebut nama atau email siswa.
3. Tulis 2 sampai 4 paragraf pendek, masing-masing 2 sampai 4 kalimat.
4. Susunannya: gambaran capaian kelas, butir yang paling lemah beserta
   dugaan penyebabnya, lalu saran tindak lanjut yang bisa dilakukan guru.
5. Bahasa Indonesia yang profesional dan tenang. Hindari kata berlebihan
   seperti "sangat luar biasa" atau "memprihatinkan".
6. Keluaran hanya objek JSON.

Bentuk keluaran JSON:
{"deskripsi": ["paragraf pertama", "paragraf kedua"]}
"""


def build_penilaian_messages(
    *,
    kunci_json: str,
    jawaban_siswa: str,
    nomor_siswa: int,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PENILAIAN},
        {
            "role": "user",
            "content": (
                "KUNCI JAWABAN (acuan penilaian):\n"
                f"{kunci_json}\n\n"
                "JAWABAN SISWA:\n"
                "--- AWAL JAWABAN ---\n"
                f"{jawaban_siswa}\n"
                "--- AKHIR JAWABAN ---\n\n"
                f"Nilailah seluruh butir pada kunci untuk siswa nomor {nomor_siswa}."
            ),
        },
    ]


def build_ringkasan_messages(
    *,
    statistik_json: str,
    jumlah_siswa: int,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_RINGKASAN_KELAS},
        {
            "role": "user",
            "content": (
                f"Data hasil penilaian untuk {jumlah_siswa} siswa "
                "(sudah dihitung oleh sistem, jangan diubah):\n"
                f"{statistik_json}\n\n"
                "Tulis ringkasan hasil analisa untuk laporan guru."
            ),
        },
    ]