"""Test penilaian heuristik indikasi AI."""

from __future__ import annotations

from app.domain.aidetect import analyze, band_for

SANTUAI = (
    "menurut gue sih fotosintesis itu ya proces tumbuhan bikin makanan pake "
    "cahaya matahari. gak tau deh kalo detailnya, yang penting tumbuhan butuh "
    "cahaya. gw pernah liat di kebun nenek, daun yang kena matahari lebih ijo. "
    "bener gak sih? hehe. intinya gitu deh yang gw inget dari pelajaran kemarin."
)

RAPIH = (
    "Fotosintesis merupakan proses biologis yang mengubah energi cahaya "
    "menjadi energi kimia. Proses tersebut berlangsung di dalam kloroplas "
    "pada sel tumbuhan hijau. Selain itu, fotosintesis menghasilkan glukosa "
    "yang berfungsi sebagai sumber energi utama bagi tumbuhan. Dengan "
    "demikian, fotosintesis menjadi dasar bagi hampir seluruh rantai makanan "
    "di permukaan bumi. Oleh karena itu, keberadaan cahaya matahari menjadi "
    "faktor yang sangat menentukan. Penting untuk diketahui bahwa proses ini "
    "juga melepaskan oksigen ke atmosfer. Dapat disimpulkan bahwa fotosintesis "
    "memiliki peran yang tidak tergantikan dalam kehidupan."
)

PENDEK = "Fotosintesis itu penting."


def test_tulisan_santai_berada_di_band_rendah() -> None:
    hasil = analyze(SANTUAI)

    assert hasil.skor < 35
    assert hasil.band == "Rendah"


def test_tulisan_rapih_menghasilkan_skor_lebih_tinggi() -> None:
    santai = analyze(SANTUAI)
    rapih = analyze(RAPIH)

    assert rapih.skor > santai.skor
    assert rapih.skor >= 35


def test_bukti_menyebutkan_alasan_yang_bisa_dibaca() -> None:
    hasil = analyze(RAPIH)

    assert hasil.bukti()
    assert any("frasa penghubung" in item.lower() for item in hasil.bukti())


def test_teks_terlalu_pendek_tidak_dinilai_ekstrem() -> None:
    hasil = analyze(PENDEK)

    assert hasil.band == "Sedang"
    assert hasil.bukti() == []
    assert "terlalu pendek" in hasil.sinyal[0].keterangan.lower()


def test_ambang_band_mengikuti_konfigurasi() -> None:
    assert band_for(10) == "Rendah"
    assert band_for(50) == "Sedang"
    assert band_for(90) == "Tinggi"


def test_skor_selalu_dalam_rentang_nol_sampai_seratus() -> None:
    for teks in (SANTUAI, RAPIH, PENDEK):
        hasil = analyze(teks)
        assert 0.0 <= hasil.skor <= 100.0