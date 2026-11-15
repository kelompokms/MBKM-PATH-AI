"""Penilaian heuristik indikasi teks dihasilkan AI.

Modul ini hanya memberi **indikasi**, bukan putusan. Tidak ada cara yang
bisa membuktikan sebuah tulisan dibuat model bahasa, dan alat pendeteksi
komersial pun salah pada dua arah: tulisan manusia yang rapi sering
dicap AI, dan tulisan AI yang diedit ringan lolos.

Karena itu skor di sini disusun dari ciri yang bisa dijelaskan satu per satu.
Setiap sinyal yang menambah atau mengurangi nilai disebutkan bobotnya, dan
laporan menampilkan alasannya. Pengajar bisa menilai sendiri apakah alasan
itu masuk akal untuk tulisan siswanya.

Satu sinyal satu fungsi kecil. `analyze` menjumlahkan bobot seluruh sinyal
di atas nilai dasar 30.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from app.core.config import settings

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"\b[\w'-]+\b", re.UNICODE)

# Nilai awal sebelum sinyal apa pun dihitung. Tengah rentang 0–100.
_BASE_SCORE = 30.0

# Di bawah batas ini, teks terlalu pendek untuk disimpulkan apa pun.
_MIN_WORDS = 40
_MIN_SENTENCES = 3

# Penanda ragam santai. Kehadirannya nyaris memastikan tulisan ditulis
# manusia secara spontan, jadi bobotnya besar ke arah negatif.
_CASUAL = frozenset(
    {
        "banget", "gak", "nggak", "enggak", "udah", "udh", "bgt", "gw",
        "gue", "lo", "lu", "wkwk", "hehe", "hihi", "sih", "dong", "deh",
        "nih", "tuh", "kayak", "kaya", "bener", "bnr", "trs", "terus",
        "yaudah",
    }
)

# Kata ganti dan penanda pengalaman pribadi.
_PERSONAL = frozenset(
    {
        "saya", "aku", "kami", "menurut", "pembaca", "penulis",
        "mengalami", "mengamati",
    }
)

# Frasa penghubung yang sangat sering muncul pada keluaran model.
_FORMULAIC = (
    "selain itu",
    "oleh karena itu",
    "dengan demikian",
    "sebagai kesimpulan",
    "penting untuk diketahui",
    "perlu diingat bahwa",
    "hal ini menunjukkan",
    "dapat disimpulkan bahwa",
    "di sisi lain",
    "sebagai contoh",
    "dalam hal ini",
    "pada dasarnya",
)


@dataclass
class Signal:
    """Satu ciri teks yang menggeser skor, beserta besar geserannya."""

    nama: str
    nilai: float
    bobot: float
    keterangan: str


@dataclass
class HeuristicResult:
    skor: float
    band: str
    sinyal: list[Signal] = field(default_factory=list)

    def bukti(self) -> list[str]:
        """Kalimat bukti siap tampil di laporan.

        Hanya sinyal yang benar-benar menggeser nilai yang disebutkan, supaya
        laporan tidak penuh dengan catatan yang tidak relevan.
        """
        rows = [item for item in self.sinyal if abs(item.bobot) >= 4]
        rows.sort(key=_abs_weight, reverse=True)
        return [f"{item.nama}: {item.keterangan}" for item in rows[:4]]


def _abs_weight(signal: Signal) -> float:
    """Pengurut untuk `sorted`: besar geseran, tanpa memperhatikan arahnya."""
    return abs(signal.bobot)


def band_for(skor: float) -> str:
    if skor < settings.ai_low:
        return "Rendah"
    if skor > settings.ai_high:
        return "Tinggi"
    return "Sedang"


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]


@dataclass
class _TextFacts:
    """Fakta dasar teks, dihitung sekali lalu dipakai seluruh sinyal."""

    words: list[str]
    sentences: list[str]
    lowered: str
    lengths: list[int]

    @property
    def total_words(self) -> int:
        return len(self.words)


# --------------------------------------------------------------------------- #
# Sembilan sinyal
#
# Setiap fungsi mengembalikan satu `Signal`, atau `None` bila ciri itu tidak
# ditemukan pada teks. Bobotnya sudah terbukti memisahkan tulisan rapi dari
# tulisan spontan pada contoh uji; mengubahnya mengubah seluruh penilaian.
# --------------------------------------------------------------------------- #


def _signal_sentence_uniformity(facts: _TextFacts) -> Signal | None:
    """Panjang kalimat yang seragam menandakan tulisan yang disunting rapi."""
    if len(facts.lengths) < 3:
        return None

    mean = statistics.fmean(facts.lengths)
    stdev = statistics.pstdev(facts.lengths)
    variation = stdev / mean if mean else 0.0

    if variation < 0.30:
        return Signal(
            "Keseragaman kalimat",
            round(variation, 3),
            14,
            f"Panjang kalimat sangat seragam (variasi {variation:.2f}).",
        )
    if variation < 0.40:
        return Signal(
            "Keseragaman kalimat",
            round(variation, 3),
            7,
            f"Panjang kalimat cukup seragam (variasi {variation:.2f}).",
        )
    return None


def _signal_formulaic_phrases(facts: _TextFacts) -> Signal | None:
    """Frasa penghubung baku yang berulang adalah ciri khas keluaran model."""
    hits = sum(facts.lowered.count(phrase) for phrase in _FORMULAIC)
    per_hundred = hits / facts.total_words * 100

    if per_hundred >= 1.0:
        return Signal(
            "Frasa penghubung baku",
            round(per_hundred, 2),
            12,
            f"{hits} frasa penghubung baku dalam {facts.total_words} kata.",
        )
    if per_hundred >= 0.4:
        return Signal(
            "Frasa penghubung baku",
            round(per_hundred, 2),
            6,
            "Beberapa frasa penghubung baku muncul berulang.",
        )
    return None


def _signal_lexical_diversity(facts: _TextFacts) -> Signal | None:
    """Keragaman kata: tinggi dan merata = rapi, banyak ulangan = spontan."""
    diversity = len(set(facts.words)) / facts.total_words

    if 0.55 <= diversity <= 0.80:
        return Signal(
            "Keragaman kata",
            round(diversity, 3),
            7,
            "Keragaman kata tinggi dan merata, ciri tulisan yang disunting rapi.",
        )
    if diversity < 0.40:
        return Signal(
            "Keragaman kata",
            round(diversity, 3),
            -6,
            "Banyak kata diulang, ciri tulisan spontan.",
        )
    return None


def _signal_personal_markers(facts: _TextFacts) -> Signal | None:
    """Rujukan pada pengalaman sendiri hampir selalu tulisan manusia."""
    hits = sum(1 for word in facts.words if word in _PERSONAL)
    density = hits / facts.total_words * 100

    if density >= 1.0:
        return Signal(
            "Penanda pengalaman pribadi",
            round(density, 2),
            -18,
            "Banyak rujukan pada pengalaman penulis sendiri.",
        )
    return None


def _signal_casual_words(facts: _TextFacts) -> Signal | None:
    """Ragam santai nyaris memastikan tulisan ditulis manusia secara spontan."""
    hits = sum(1 for word in facts.words if word in _CASUAL)
    if hits >= 1:
        return Signal(
            "Ragam santai",
            float(hits),
            -28,
            f"{hits} kata ragam santai ditemukan.",
        )
    return None


def _signal_punctuation_tidiness(facts: _TextFacts) -> Signal | None:
    """Tanda baca dan huruf kapital yang selalu rapi menandakan tulisan disunting."""
    if len(facts.sentences) < 5:
        return None

    rapi = sum(
        1
        for sentence in facts.sentences
        if sentence[:1].isupper() and sentence.rstrip().endswith((".", "!", "?"))
    )
    ratio = rapi / len(facts.sentences)

    if ratio >= 0.9:
        return Signal(
            "Kerapian tanda baca",
            round(ratio, 3),
            8,
            "Hampir seluruh kalimat diawali huruf kapital dan diakhiri tanda baca.",
        )
    return None


def _signal_word_length(facts: _TextFacts) -> Signal | None:
    """Kata-kata panjang menandakan register yang sangat formal."""
    if not facts.words:
        return None

    average = statistics.fmean(len(word) for word in facts.words)
    if average >= 6.2:
        return Signal(
            "Panjang kata",
            round(average, 2),
            6,
            "Kata-kata cenderung panjang, menandakan register sangat formal.",
        )
    return None


def _signal_sentence_length(facts: _TextFacts) -> Signal | None:
    """Kalimat rata-rata sangat panjang dengan banyak anak kalimat."""
    if not facts.lengths:
        return None

    average = statistics.fmean(facts.lengths)
    if average >= 28:
        return Signal(
            "Panjang kalimat",
            round(average, 1),
            6,
            "Kalimat rata-rata sangat panjang dengan banyak anak kalimat.",
        )
    return None


def _signal_trigram_repetition(facts: _TextFacts) -> Signal | None:
    """Rangkaian tiga kata yang berulang menandakan tulisan spontan."""
    trigrams = [
        " ".join(facts.words[index : index + 3])
        for index in range(max(0, facts.total_words - 2))
    ]
    if not trigrams:
        return None

    repeat_ratio = 1 - len(set(trigrams)) / len(trigrams)
    if repeat_ratio > 0.05:
        return Signal(
            "Pengulangan rangkaian kata",
            round(repeat_ratio, 3),
            -10,
            "Banyak rangkaian kata yang diulang, ciri tulisan spontan.",
        )
    return None


# --------------------------------------------------------------------------- #
# Fungsi utama
# --------------------------------------------------------------------------- #

_SIGNAL_BUILDERS = (
    _signal_sentence_uniformity,
    _signal_formulaic_phrases,
    _signal_lexical_diversity,
    _signal_personal_markers,
    _signal_casual_words,
    _signal_punctuation_tidiness,
    _signal_word_length,
    _signal_sentence_length,
    _signal_trigram_repetition,
)


def _too_short_result(total_words: int) -> HeuristicResult:
    """Teks sependek ini tidak bisa dinilai; jangan menebak apa pun."""
    return HeuristicResult(
        skor=round(settings.ai_low, 2),
        band="Sedang",
        sinyal=[
            Signal(
                nama="Panjang teks",
                nilai=float(total_words),
                bobot=0.0,
                keterangan="Teks terlalu pendek untuk dinilai secara andal.",
            )
        ],
    )


def analyze(text: str) -> HeuristicResult:
    """Skor 0–100 beserta sinyal yang membentuknya."""
    words_all = _WORD_RE.findall(text)
    sentences = _sentences(text)

    if len(words_all) < _MIN_WORDS or len(sentences) < _MIN_SENTENCES:
        return _too_short_result(len(words_all))

    facts = _TextFacts(
        words=[word.lower() for word in words_all],
        sentences=sentences,
        lowered=text.lower(),
        lengths=[len(_WORD_RE.findall(sentence)) for sentence in sentences],
    )

    sinyal: list[Signal] = []
    for builder in _SIGNAL_BUILDERS:
        item = builder(facts)
        if item is not None:
            sinyal.append(item)

    score = _BASE_SCORE + sum(item.bobot for item in sinyal)
    final = round(max(0.0, min(100.0, score)), 2)
    return HeuristicResult(skor=final, band=band_for(final), sinyal=sinyal)
