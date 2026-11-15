"""Test pemotongan teks panjang.

Fungsi ini yang membuat ringkasan dokumen bertahap mungkin. Kalau ia salah,
kata-kata bisa hilang atau bergeser, dan laporan akan tampak lengkap padahal
isinya sudah berbeda dari dokumen aslinya.
"""

from __future__ import annotations

from app.core.tokens import split_into_chunks

# Batas nyata: maks_tokens x 3.5 karakter.
_BATAS_CHARS = int(200 * 3.5)


def _teks(jumlah_kata: int = 2000) -> str:
    return " ".join(f"kata{nomor}" for nomor in range(jumlah_kata))


def _kata(teks: str) -> list[str]:
    return teks.split()


def test_teks_pendek_tidak_dipotong() -> None:
    assert split_into_chunks("satu dua tiga", max_tokens=200) == ["satu dua tiga"]


def test_teks_kosong_menghasilkan_daftar_kosong() -> None:
    assert split_into_chunks("   \n  ", max_tokens=200) == []


def test_teks_panjang_terbagi_menjadi_beberapa_bagian() -> None:
    bagian = split_into_chunks(_teks(), max_tokens=200)

    assert len(bagian) > 1


def test_tidak_ada_kata_yang_hilang() -> None:
    """Digabung kembali, hasilnya harus sama persis dengan teks aslinya."""
    teks = _teks()

    bagian = split_into_chunks(teks, max_tokens=200)

    assert _kata(" ".join(bagian)) == _kata(teks)


def test_setiap_bagian_tidak_melebihi_batas() -> None:
    for bagian in split_into_chunks(_teks(), max_tokens=200):
        assert len(bagian) <= _BATAS_CHARS


def test_setiap_bagian_terisi_penuh() -> None:
    """Batas yang terlalu ketat membuat model dipanggil berkali-kali tanpa guna."""
    bagian = split_into_chunks(_teks(), max_tokens=200)

    for potongan in bagian[:-1]:
        assert len(potongan) > _BATAS_CHARS * 0.8


def test_tumpang_tindih_mengulang_ekor_bagian_sebelumnya() -> None:
    """Kalimat yang jatuh di batas potongan tidak kehilangan kalimat sebelumnya."""
    bagian = split_into_chunks(_teks(), max_tokens=200, overlap_tokens=40)

    kata_terakhir = _kata(bagian[0])[-1]

    assert kata_terakhir in _kata(bagian[1])


def test_tumpang_tindih_tidak_pernah_melebihi_setengah_bagian() -> None:
    """Tumpang tindih yang terlalu besar akan membuat kemajuan macet."""
    bagian = split_into_chunks(_teks(), max_tokens=200, overlap_tokens=500)

    assert len(bagian) > 1
    assert _kata(bagian[0])[-1] in _kata(bagian[1])


def test_teks_tanpa_spasi_tetap_menghasilkan_satu_bagian() -> None:
    """Satu kata raksasa tidak bisa dipotong, dan tidak boleh membuat kacau."""
    teks = "a" * 5000

    assert split_into_chunks(teks, max_tokens=200) == [teks]


def test_potongan_berurutan_menutupi_seluruh_teks() -> None:
    """Tanpa tumpang tindih, gabungan potongan sama panjang dengan teksnya."""
    teks = _teks()

    bagian = split_into_chunks(teks, max_tokens=200, overlap_tokens=0)

    assert len(" ".join(bagian)) == len(teks)
