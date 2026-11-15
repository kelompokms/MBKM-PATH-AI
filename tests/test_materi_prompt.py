"""Test pemisahan prompt pengajar dan pelajar.

Perbedaannya bukan kosmetik. Prompt pengajar **menghasilkan** bahan ajar dan
boleh melengkapi dari pengetahuan umum. Prompt pelajar **menjawab** satu
pertanyaan dan hanya boleh memakai materi yang diberikan guru.

Kalau dua prompt ini tertukar, akibatnya tidak terlihat langsung: pelajar
akan mendapat jawaban yang benar tetapi di luar materi gurunya. Guru lalu
tidak bisa membedakan "kelas saya paham materi" dari "model menjawab dari
luar materi".
"""

from __future__ import annotations

from app.prompts.materi import build_materi_messages

MATERI = "Fotosintesis membutuhkan cahaya matahari, air, dan karbon dioksida."
FLAGS = {"tabel": False, "diagram": False, "ilustrasi": False}


def pesan(audience: str) -> tuple[str, str]:
    hasil = build_materi_messages(
        pertanyaan="Mengapa tumbuhan butuh cahaya?",
        judul=None,
        deskripsi_materi="Materi fotosintesis kelas VII",
        materi_rujukan=MATERI,
        jenjang="SMP kelas VII",
        flags=FLAGS,
        audience=audience,
    )
    return hasil[0]["content"], hasil[1]["content"]


def test_prompt_pengajar_menyusun_materi() -> None:
    sistem, pengguna = pesan("teacher")

    assert "penulis materi ajar" in sistem
    assert "BERKAS RUJUKAN" in pengguna
    # Prompt pengajar memang mengizinkan pelengkapan dari pengetahuan umum.
    assert "lengkapi dengan pengetahuan umum" in pengguna


def test_prompt_pelajar_hanya_boleh_memakai_materi_guru() -> None:
    sistem, pengguna = pesan("student")

    assert "pendamping belajar" in sistem
    assert "ATURAN SUMBER" in sistem
    assert "MATERI DARI GURU" in pengguna
    assert MATERI in pengguna

    # Yang paling penting: izin melengkapi dari pengetahuan umum tidak ada.
    assert "lengkapi dengan pengetahuan umum" not in pengguna
    assert "Jangan menambahkan pengetahuan dari luar materi" in pengguna


def test_pelajar_tidak_menentukan_judul() -> None:
    _, pengguna = pesan("student")

    assert "Beri judul singkat yang mencerminkan pertanyaan pelajar" in pengguna
    # Instruksi "tentukan judul materi" milik pengajar tidak boleh muncul.
    assert "Tentukan judul materi" not in pengguna


def test_pertanyaan_disebut_sebagai_pertanyaan_pelajar() -> None:
    _, pengguna = pesan("student")

    assert "Pertanyaan pelajar yang harus dijawab" in pengguna
    assert "Mengapa tumbuhan butuh cahaya?" in pengguna


def test_jenjang_ikut_disebut_pada_kedua_jalur() -> None:
    for audience in ("teacher", "student"):
        _, pengguna = pesan(audience)
        assert "SMP kelas VII" in pengguna


def test_tanpa_materi_rujukan_blok_sumber_tidak_disertakan() -> None:
    """Pelajar tanpa materi bukan alasan menyisipkan blok kosong."""
    hasil = build_materi_messages(
        pertanyaan="Apa itu fotosintesis?",
        judul=None,
        deskripsi_materi="Materi fotosintesis",
        materi_rujukan=None,
        jenjang=None,
        flags=FLAGS,
        audience="student",
    )

    assert "MATERI DARI GURU" not in hasil[1]["content"]


def test_deskripsi_kosong_tidak_menyisakan_baris_menggantung() -> None:
    """Baris tanpa isi terbaca seperti instruksi yang rusak, jadi dilewati."""
    for audience in ("teacher", "student"):
        hasil = build_materi_messages(
            pertanyaan="Apa itu fotosintesis?",
            judul="Fotosintesis" if audience == "teacher" else None,
            deskripsi_materi="",
            materi_rujukan=MATERI,
            jenjang=None,
            flags=FLAGS,
            audience=audience,
        )

        pengguna = hasil[1]["content"]
        assert "Deskripsi materi yang diminta:" not in pengguna
        assert "Topik materi dari guru:" not in pengguna


def test_aturan_rujukan_panjang_ada_di_prompt_pengajar() -> None:
    """Dokumen panjang harus dibahas seluruhnya, bukan hanya pengantarnya.

    Tanpa aturan ini, dokumen 39 halaman yang diminta dijelaskan dijawab dengan
    ringkasan yang hanya menyebut judul, penulis, dan afiliasinya.
    """
    sistem, _ = pesan("teacher")

    # Spasi dirapikan lebih dulu: aturan prompt ditulis berbaris, sehingga
    # kalimatnya tidak selalu berada dalam satu baris.
    rapi = " ".join(sistem.split())

    assert "bahan rujukan yang diberikan panjang" in rapi
    assert "dari awal sampai akhir" in rapi
    assert "penulis, atau penerbit" in rapi


def test_deskripsi_pelajar_tidak_bertabrakan_dengan_nama_blok_sumber() -> None:
    """Nama baris deskripsi tidak boleh menyerupai nama blok materi guru."""
    _, pengguna = pesan("student")

    assert "Topik materi dari guru: Materi fotosintesis kelas VII" in pengguna
    assert "Materi yang diberikan guru" not in pengguna