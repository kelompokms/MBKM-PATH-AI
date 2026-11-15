"""Test algoritme kemiripan."""

from __future__ import annotations

from app.core.config import settings
from app.domain.similarity import (
    PairResult,
    build_pairs,
    cosine,
    finalize,
    jaccard,
    normalize,
    select_grey_band,
    shingles,
    tfidf_vectors,
    tokenize,
)

IDENTIK_A = (
    "Fotosintesis adalah proses tumbuhan mengubah cahaya matahari menjadi "
    "energi kimia yang disimpan dalam bentuk glukosa dan oksigen."
)

IDENTIK_B = (
    "Fotosintesis adalah proses tumbuhan mengubah cahaya matahari menjadi "
    "energi kimia yang disimpan dalam bentuk glukosa dan oksigen."
)

BERBEDA = (
    "Kerajaan Majapahit berdiri pada abad keempat belas dan dipimpin oleh "
    "Raden Wijaya sebelum berkembang menjadi kerajaan maritim besar."
)


def test_jawaban_identik_bernilai_satu() -> None:
    pairs = build_pairs([IDENTIK_A, IDENTIK_B])

    assert len(pairs) == 1
    assert pairs[0].skor == 1.0
    assert pairs[0].jaccard == 1.0
    assert pairs[0].cosine == 1.0


def test_jawaban_berbeda_bernilai_rendah() -> None:
    pairs = build_pairs([IDENTIK_A, BERBEDA])

    assert pairs[0].skor < 0.15


def test_normalisasi_mengabaikan_tanda_baca_dan_huruf_besar() -> None:
    assert normalize("Halo, Dunia!") == "halo dunia"
    assert tokenize("Satu, dua; tiga.") == ["satu", "dua", "tiga"]


def test_shingle_lima_kata() -> None:
    tokens = ["a", "b", "c", "d", "e", "f"]
    hasil = shingles(tokens, 5)

    assert hasil == {"a b c d e", "b c d e f"}


def test_jaccard_dan_cosine_aman_untuk_kosong() -> None:
    assert jaccard(set(), set()) == 0.0
    assert jaccard({"a"}, set()) == 0.0
    assert cosine({}, {}) == 0.0


def test_tfidf_memberi_bobot_rendah_pada_kata_umum() -> None:
    documents = [
        "yang dan di fotosintesis kloroplas",
        "yang dan di majapahit kerajaan",
    ]
    vectors = tfidf_vectors([doc.split() for doc in documents])

    # Kata umum ("yang", "dan", "di") ada di daftar henti sehingga tidak
    # masuk vektor sama sekali.
    assert "yang" not in vectors[0]
    assert "fotosintesis" in vectors[0]
    assert vectors[0]["fotosintesis"] > 0


def test_pita_abu_abu_saja_yang_dipilih() -> None:
    pairs = [
        PairResult(a=0, b=1, jaccard=0.9, cosine=0.9, skor=0.9),  # jelas mirip
        PairResult(a=0, b=2, jaccard=0.5, cosine=0.5, skor=0.5),  # ambigu
        PairResult(a=1, b=2, jaccard=0.05, cosine=0.05, skor=0.05),  # jelas beda
    ]

    selected = select_grey_band(
        pairs,
        low=settings.sim_llm_low,
        high=settings.sim_llm_high,
        max_per_student=3,
    )

    assert [p.skor for p in selected] == [0.5]


def test_kuota_per_siswa_dihormati() -> None:
    pairs = [
        PairResult(a=0, b=1, jaccard=0, cosine=0, skor=0.56),
        PairResult(a=0, b=2, jaccard=0, cosine=0, skor=0.55),
        PairResult(a=0, b=3, jaccard=0, cosine=0, skor=0.54),
        PairResult(a=0, b=4, jaccard=0, cosine=0, skor=0.53),
    ]

    selected = select_grey_band(pairs, low=0.25, high=0.75, max_per_student=2)

    assert len(selected) == 2


def test_persentase_tertinggi_per_siswa() -> None:
    pairs = [
        PairResult(a=0, b=1, jaccard=0, cosine=0, skor=0.9),
        PairResult(a=1, b=2, jaccard=0, cosine=0, skor=0.3),
    ]

    report = finalize(3, pairs, low=0.25, high=0.75)

    assert report.tertinggi == [90.0, 90.0, 30.0]
    assert report.pasangan_tertinggi == [1, 0, 1]
    # Matriks simetris.
    assert report.matriks[0][1] == report.matriks[1][0] == 0.9


def test_siswa_dengan_jawaban_tunggal_tidak_menghasilkan_pasangan() -> None:
    assert build_pairs([IDENTIK_A]) == []