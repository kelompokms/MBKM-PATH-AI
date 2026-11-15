"""Perhitungan nilai yang deterministik.

Model bahasa memberi skor per butir. Mulai dari titik ini, tidak ada lagi
LLM: total, label, dan pembulatan dihitung di sini dengan aturan yang sama
untuk setiap siswa. Itulah yang membuat nilai bisa dipertanggungjawabkan —
dua siswa dengan jawaban setara selalu mendapat angka yang sama, terlepas
dari suasana hati model pada panggilan tertentu.
"""

from __future__ import annotations

import re

from app.core.config import settings

_WHITESPACE_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)


class IncompleteGrading(Exception):
    """Model tidak menilai seluruh butir yang ada di kunci jawaban."""

    def __init__(self, missing: list[int]) -> None:
        self.missing = missing
        super().__init__(
            "Butir yang belum dinilai: " + ", ".join(str(n) for n in missing)
        )


def normalize_text(value: str) -> str:
    """Bentuk pembanding: huruf kecil, tanpa tanda baca, spasi tunggal."""
    lowered = value.lower().replace("\u2019", "'").replace("\u2018", "'")
    without_punct = _NON_WORD_RE.sub(" ", lowered)
    return _WHITESPACE_RE.sub(" ", without_punct).strip()


def compute_score(
    scores: dict[int, float],
    weights: dict[int, float],
    *,
    expected: set[int] | None = None,
) -> float:
    """Nilai akhir 0–100 dengan pembobotan campuran.

    `Σ(skor × bobot) / Σ(bobot)` memakai satu rumus untuk semua bentuk kunci:
    butir objektif berbobot 1 dan esai berbobot 3 berada di skala yang sama,
    karena skor tiap butir sudah dinyatakan dalam 0–100.

    Butir yang tidak ada di `scores` dianggap belum dinilai, bukan nol.
    Menilainya nol berarti menghukum siswa atas kelalaian model.
    """
    if expected is not None:
        missing = sorted(expected - set(scores))
        if missing:
            raise IncompleteGrading(missing)

    total_weight = sum(weights.get(nomor, 1.0) for nomor in scores)
    if total_weight <= 0:
        return 0.0

    weighted = sum(
        scores[nomor] * weights.get(nomor, 1.0) for nomor in scores
    )
    return round(weighted / total_weight, 2)


def label_for(nilai: float) -> str:
    """Label capaian dari ambang yang dikonfigurasi."""
    if nilai >= settings.label_sangat_baik_min:
        return "Sangat Baik"
    if nilai >= settings.label_baik_min:
        return "Baik"
    if nilai >= settings.label_cukup_min:
        return "Cukup"
    return "Perlu Bimbingan"


def evidence_is_verifiable(quote: str, student_text: str, *, min_words: int = 4) -> bool:
    """Apakah kutipan benar-benar ada di jawaban siswa.

    Model cenderung mengarang kutipan yang terdengar meyakinkan. Kutipan
    yang tidak ditemukan di teks asli dibuang oleh pemanggil, sehingga
    laporan tidak pernah memuat bukti fiktif.

    Kutipan sangat pendek tidak bisa diverifikasi secara bermakna — kata
    umum seperti "ya" akan selalu "ditemukan". Kutipan seperti itu dianggap
    tidak terbukti agar tidak memberi rasa aman palsu.
    """
    needle = normalize_text(quote)
    if len(needle.split()) < min_words:
        return False
    return needle in normalize_text(student_text)


def distribute_weights(items: list[tuple[int, float]]) -> dict[int, float]:
    """Rapikan bobot: buang nilai tidak wajar, sisakan minimal 1.0."""
    weights: dict[int, float] = {}
    for nomor, bobot in items:
        if bobot is None or bobot <= 0:
            weights[nomor] = 1.0
        else:
            weights[nomor] = round(min(bobot, 100.0), 3)
    return weights