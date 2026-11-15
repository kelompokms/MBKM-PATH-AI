"""Perhitungan kemiripan jawaban antarsiswa, sepenuhnya deterministik.

Model bahasa tidak dipakai untuk menghitung angka di sini. Alasannya bukan
soal biaya, melainkan kepastian: persentase kemiripan adalah tuduhan yang
akan ditanyakan siswa dan orang tua. Angka yang berubah setiap kali
dijalankan tidak bisa dipertahankan.

Dua ukuran dipakai bersama karena masing-masing punya titik buta:

- **Jaccard pada shingle kata** menangkap penyalinan harfiah, termasuk
  ketika kata-kata diacak sedikit.
- **Cosine TF-IDF** menangkap kesamaan tema walau kalimatnya disusun ulang,
  karena kata umum diberi bobot rendah.

Rata-rata keduanya cukup stabil untuk memisahkan tiga wilayah: jelas berbeda,
jelas mirip, dan pita abu-abu. Hanya pita abu-abu yang diserahkan ke model
sebagai wasit, dan model tidak pernah menentukan angka akhir — ia hanya
menyempurnakan nilai yang sudah dihitung.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")

# Kata yang terlalu umum untuk membantu membedakan dua jawaban.
_STOPWORDS = frozenset(
    {
        "yang", "dan", "di", "ke", "dari", "untuk", "pada", "dengan",
        "ini", "itu", "adalah", "akan", "tidak", "juga", "dalam",
        "sebagai", "oleh", "atau", "karena", "bahwa", "telah", "sudah",
        "masih", "bisa", "dapat", "para", "lebih", "sangat", "serta",
        "namun", "tetapi", "sehingga", "apabila", "jika", "maka",
        "hal", "satu", "dua", "tiga", "kami", "kita", "anda", "saya",
        "mereka", "dia", "ia", "nya",
    }
)


@dataclass
class PairResult:
    a: int
    b: int
    jaccard: float
    cosine: float
    skor: float
    sumber: str = "deterministik"


@dataclass
class SimilarityReport:
    # `matriks[i][j]` = skor 0..1 antara siswa i dan j (bentuk simetris).
    matriks: list[list[float]] = field(default_factory=list)
    pasangan: list[PairResult] = field(default_factory=list)
    # Persentase kemiripan tertinggi untuk tiap siswa.
    tertinggi: list[float] = field(default_factory=list)
    # Indeks siswa dengan kemiripan tertinggi, untuk keterangan tambahan.
    pasangan_tertinggi: list[int | None] = field(default_factory=list)


def normalize(text: str) -> str:
    lowered = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    return _WHITESPACE_RE.sub(" ", _NON_WORD_RE.sub(" ", lowered)).strip()


def tokenize(text: str) -> list[str]:
    return [word for word in normalize(text).split() if word]


def shingles(tokens: list[str], size: int) -> set[str]:
    """Himpunan potongan berurutan sepanjang `size` kata."""
    if len(tokens) < size:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


def tfidf_vectors(documents: list[list[str]]) -> list[dict[str, float]]:
    """Vektor TF-IDF sederhana. Nol dependensi, cukup untuk ≤100 dokumen."""
    total_docs = len(documents)
    document_frequency: dict[str, int] = {}

    for tokens in documents:
        for term in set(tokens):
            document_frequency[term] = document_frequency.get(term, 0) + 1

    idf = {
        term: math.log((1 + total_docs) / (1 + freq)) + 1.0
        for term, freq in document_frequency.items()
    }

    vectors: list[dict[str, float]] = []
    for tokens in documents:
        counts: dict[str, int] = {}
        for token in tokens:
            if token in _STOPWORDS:
                continue
            counts[token] = counts.get(token, 0) + 1

        total = sum(counts.values())
        if total == 0:
            vectors.append({})
            continue

        vectors.append(
            {term: (count / total) * idf[term] for term, count in counts.items()}
        )

    return vectors


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    # Iterasi kamus yang lebih kecil agar perhitungan tetap ringan.
    if len(a) > len(b):
        a, b = b, a
    dot = sum(value * b.get(term, 0.0) for term, value in a.items())
    if dot == 0.0:
        return 0.0
    norm_a = math.sqrt(sum(value * value for value in a.values()))
    norm_b = math.sqrt(sum(value * value for value in b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def build_pairs(
    texts: list[str], *, shingle_size: int = 5
) -> list[PairResult]:
    """Hitung skor untuk setiap pasangan unik (i < j)."""
    tokens = [tokenize(text) for text in texts]
    shingle_sets = [shingles(item, shingle_size) for item in tokens]
    vectors = tfidf_vectors(tokens)

    results: list[PairResult] = []
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            jac = jaccard(shingle_sets[i], shingle_sets[j])
            cos = cosine(vectors[i], vectors[j])
            # Rata-rata sederhana: tidak ada alasan kuat memberi bobot
            # berbeda, dan bobot yang dikarang akan sulit dipertanggungjawabkan.
            score = round(0.5 * jac + 0.5 * cos, 4)
            results.append(
                PairResult(
                    a=i, b=j, jaccard=round(jac, 4), cosine=round(cos, 4), skor=score
                )
            )
    return results


def _pair_score(pair: PairResult) -> float:
    """Pengurut untuk `sorted`: dari pasangan paling mirip ke paling beda."""
    return pair.skor


def select_grey_band(
    pairs: list[PairResult],
    *,
    low: float,
    high: float,
    max_per_student: int,
) -> list[PairResult]:
    """Pilih pasangan yang diserahkan ke model.

    Hanya pasangan di antara `low` dan `high` yang ambigu. Kuota per siswa
    mencegah satu siswa dengan banyak kemiripan kecil menghabiskan seluruh
    anggaran panggilan model.
    """
    candidates = [pair for pair in pairs if low <= pair.skor <= high]
    candidates.sort(key=_pair_score, reverse=True)

    quota: dict[int, int] = {}
    selected: list[PairResult] = []

    for pair in candidates:
        if quota.get(pair.a, 0) >= max_per_student:
            continue
        if quota.get(pair.b, 0) >= max_per_student:
            continue
        quota[pair.a] = quota.get(pair.a, 0) + 1
        quota[pair.b] = quota.get(pair.b, 0) + 1
        selected.append(pair)

    return selected


def finalize(
    count: int, pairs: list[PairResult], *, low: float, high: float
) -> SimilarityReport:
    """Susun matriks akhir dan persentase tertinggi per siswa."""
    matrix = [[0.0] * count for _ in range(count)]

    for pair in pairs:
        matrix[pair.a][pair.b] = pair.skor
        matrix[pair.b][pair.a] = pair.skor

    highest = [0.0] * count
    partner: list[int | None] = [None] * count

    for index in range(count):
        best = 0.0
        best_partner: int | None = None
        for other in range(count):
            if other == index:
                continue
            if matrix[index][other] > best:
                best = matrix[index][other]
                best_partner = other
        highest[index] = round(best * 100, 2)
        partner[index] = best_partner

    return SimilarityReport(
        matriks=matrix, pasangan=pairs, tertinggi=highest, pasangan_tertinggi=partner
    )


def describe_similarity(persentase: float, *, low: float, high: float) -> str:
    """Keterangan singkat untuk laporan, memakai ambang yang sama."""
    if persentase >= high * 100:
        return "Kemiripan tinggi — perlu ditinjau pengajar"
    if persentase >= low * 100:
        return "Kemiripan sedang — perlu diperiksa"
    return "Kemiripan rendah"