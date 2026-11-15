"""Prompt untuk fitur 6 — ringkasan dokumen panjang.

Ini bukan varian dari prompt materi ajar, dan perbedaannya mendasar.

Penulis materi ajar menghasilkan bahan pelajaran pendek: tiga sampai lima
bagian, cukup untuk satu pertemuan. Aturan itu tepat untuk materi, tetapi salah
untuk telaah. Dokumen ilmiah 39 halaman yang diminta "jelaskan" lalu dijawab
dengan bentuk bahan ajar akan menghasilkan ringkasan yang hanya menyebut
halaman judul, penulis, dan afiliasinya.

Karena itu dua tahap di bawah memakai prompt sendiri.

Tahap pertama membaca satu potongan dokumen dan mencatat isinya apa adanya,
termasuk metode, alat, dan istilah yang benar-benar disebut. Tahap kedua
menggabungkan catatan-catatan itu menjadi satu telaah yang membahas seluruh
dokumen, bukan hanya pengantarnya.
"""

from __future__ import annotations

SYSTEM_RINGKASAN_BAGIAN = """\
Anda menulis SATU BAGIAN laporan telaah, berdasarkan satu potongan sebuah
dokumen. Tulisan Anda akan langsung menjadi bagian laporan itu, tanpa diringkas
lagi, sehingga hal yang tidak Anda tulis di sini tidak akan pernah muncul di
laporan.

Aturan yang WAJIB dipatuhi:
1. Seluruh isi ditulis dalam Bahasa Indonesia yang baku.
2. Tulis HANYA apa yang benar-benar tertulis di potongan ini. Jangan menambah
   pengetahuan dari luar, dan jangan mengarang angka, tahun, nama, atau hasil.
3. Tulis 3 sampai 6 paragraf yang benar-benar menjelaskan isi potongan ini,
   bukan sekadar menyebutkan judul babnya. Jelaskan apa yang dibahas, bagaimana
   caranya, dan apa hasil atau pendapat yang disampaikan.
4. Sebutkan SELURUH nama metode, alat, dataset, dan metrik yang disebut
   potongan ini, apa adanya. Jangan dipilih-pilih dan jangan disamarkan menjadi
   sebutan umum. Nama-nama itu yang dicari pembaca laporan.
5. Sertakan angka dan hasil pengujian yang disebut dokumen apa adanya, misalnya
   nilai akurasi atau jumlah data. Jangan menghitung ulang dan jangan menebak
   angka yang tidak tertulis.
6. Bila potongan ini hanya berisi daftar pustaka, ucapan terima kasih, atau
   halaman administratif, katakan demikian secara singkat. Jangan mengarang isi
   supaya terlihat berbobot.
7. Jangan menuliskan proses berpikir, permintaan maaf, atau catatan tentang diri
   sendiri. Keluaran hanya objek JSON.
8. Batas jumlah butir: ringkasan maksimal 8 paragraf, poin maksimal 16 butir,
   metode maksimal 40 butir, istilah maksimal 12 butir, dan judul_bagian
   maksimal 180 karakter. Batas itu lebar, jadi pakai seperlunya saja.

Bentuk keluaran JSON:
{
  "judul_bagian": "string",
  "ringkasan": ["paragraf pertama", "paragraf kedua"],
  "poin": ["butir penting"],
  "metode": ["nama metode, alat, dataset, atau metrik yang disebut"],
  "istilah": [{"nama": "istilah", "arti": "arti menurut dokumen"}]
}
"""

SYSTEM_RINGKASAN_AKHIR = """\
Anda menyusun BAGIAN PEMBUKA DAN PENUTUP sebuah laporan telaah dokumen.
Catatan per bagian dokumen sudah dibuat terpisah dan sudah menjadi isi laporan,
jadi Anda TIDAK perlu menulis ulang isinya.

Aturan yang WAJIB dipatuhi:
1. Seluruh isi ditulis dalam Bahasa Indonesia yang baku dan padat isi.
2. Jangan menambahkan fakta, angka, nama, atau metode yang tidak ada di
   catatan. Bila catatan tidak menyebutkan sesuatu, biarkan kosong.
3. "ringkasan_eksekutif" adalah 2 sampai 4 paragraf yang merangkum SELURUH
   dokumen, dari bagian awal sampai bagian akhir: dokumen ini tentang apa, apa
   tujuannya, metode apa saja yang dibahas, dan apa kesimpulan utamanya.
   Paragraf ini harus mencerminkan bagian akhir dokumen juga, bukan hanya
   pengantarnya.
4. "tabel_perbandingan" diisi hanya bila catatan memuat hal yang memang bisa
   dibandingkan, misalnya beberapa metode, dataset, atau metrik. Bila tidak ada,
   isi null. Tabel 3 sampai 6 kolom, maksimal 14 baris, dan isi sel berupa teks
   pendek, bukan kalimat panjang. Isi tabel dengan nama yang konkret, bukan
   kategori umum: tulis "NSL-KDD" atau "Random Forest", bukan "dataset umum"
   atau "metode klasik". Bila catatan menyebut dataset dan metrik evaluasi,
   jadikan keduanya bagian dari tabel.
5. "temuan_utama" adalah butir-butir temuan penting yang disebut dokumen,
   maksimal 16 butir. Isi bagian akhir dokumen juga ikut di sini.
6. "keterbatasan" adalah keterbatasan atau tantangan yang diakui dokumen,
   maksimal 12 butir. Bila dokumen tidak menyebutkannya, isi dengan array
   kosong. Jangan mengarang keterbatasan yang tidak dibahas.
7. "istilah" adalah istilah penting beserta artinya menurut dokumen, maksimal
   20 butir. Ambil dari seluruh bagian dokumen.
8. Bila diminta diagram, pilih "alur" untuk proses atau tahapan dan
   "peta_pikiran" untuk konsep yang bercabang. Gunakan 3 sampai 10 node.
   Setiap node memiliki "id" pendek tanpa spasi dan "label" maksimal 6 kata.
   Bila tidak diminta, isi dengan null.
9. Jangan menuliskan proses berpikir, permintaan maaf, atau catatan tentang diri
   sendiri. Keluaran hanya objek JSON.

Bentuk keluaran JSON:
{
  "judul": "string",
  "ringkasan_eksekutif": "string",
  "tabel_perbandingan": {
    "judul": "string",
    "kolom": ["string", "string", "string"],
    "baris": [["string", "string", "string"]]
  },
  "temuan_utama": ["string"],
  "keterbatasan": ["string"],
  "istilah": [{"nama": "string", "arti": "string"}],
  "diagram": {
    "jenis": "alur",
    "judul": "string",
    "nodes": [{"id": "string", "label": "string"}],
    "edges": [{"dari": "string", "ke": "string", "label": "string"}]
  }
}
"""


def build_chunk_messages(
    *, bagian_ke: int, jumlah_bagian: int, teks: str
) -> list[dict[str, str]]:
    """Pesan untuk tahap pertama: satu potongan dokumen."""
    instructions = [
        f"Ini potongan {bagian_ke} dari {jumlah_bagian} potongan dokumen yang "
        "sama, dibaca berurutan.",
        "Tulis bagian laporan untuk potongan ini, sesuai bentuk yang ditetapkan.",
        "--- AWAL POTONGAN DOKUMEN ---",
        teks,
        "--- AKHIR POTONGAN DOKUMEN ---",
    ]

    return [
        {"role": "system", "content": SYSTEM_RINGKASAN_BAGIAN},
        {"role": "user", "content": "\n\n".join(instructions)},
    ]


def build_synthesis_messages(
    *,
    judul: str,
    catatan: str,
    fokus: str | None,
    jenjang: str | None,
    minta_diagram: bool,
) -> list[dict[str, str]]:
    """Pesan untuk tahap kedua: penggabungan seluruh catatan."""
    instructions = [
        f"Susun telaah atas dokumen berikut: \"{judul}\".",
    ]

    if fokus:
        instructions.append(f"Hal yang ingin ditekankan pengguna: \"{fokus}\".")

    if jenjang:
        instructions.append(f"Jenjang pendidikan pembaca: {jenjang}.")

    instructions.append(
        "Elemen tambahan yang diminta: diagram."
        if minta_diagram
        else "Jangan menyertakan diagram. Isi cukup paragraf, poin, dan tabel."
    )

    instructions.append(
        "--- AWAL CATATAN PER BAGIAN ---\n"
        f"{catatan}\n"
        "--- AKHIR CATATAN PER BAGIAN ---"
    )
    instructions.append(
        "Kembalikan HANYA objek JSON sesuai bentuk yang ditetapkan. "
        "Tanpa penjelasan pembuka atau penutup."
    )

    return [
        {"role": "system", "content": SYSTEM_RINGKASAN_AKHIR},
        {"role": "user", "content": "\n\n".join(instructions)},
    ]
