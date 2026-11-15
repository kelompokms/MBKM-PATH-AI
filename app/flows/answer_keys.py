"""Tahap pertama fitur 3 — ekstraksi kunci jawaban.

Tahap ini sengaja dipisahkan dari penilaian. Kunci jawaban yang dibaca dari
berkas pindaian, tulisan tangan, atau tabel yang tidak rapi hampir selalu
memuat ketidakpastian. Bila ketidakpastian itu dibiarkan mengalir langsung
ke penilaian, seluruh nilai satu kelas bisa salah tanpa ada yang menyadari.

Karena itu hasil ekstraksi dikembalikan ke guru lebih dulu, lengkap dengan
daftar keraguan dan tingkat keyakinan. Guru bisa membetulkan, lalu menilai.

Fungsi `extract_key` juga dipakai oleh alur penilaian ketika guru mengunggah
kunci langsung tanpa tahap pemeriksaan, sehingga hanya ada satu tempat yang
memahami cara membaca berkas kunci.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.core.errors import InvalidRequest
from app.core.llm import llm
from app.core.tasks import Task
from app.core.tokens import truncate_to_tokens
from app.domain.schemas import AnswerKeyDraft
from app.ingest.documents import extract_document
from app.ingest.vision import buat_pembaca
from app.jobs import Job, JobState, job_store
from app.observability.logger import get_logger
from app.prompts.kunci import build_kunci_messages
from app.store import StoredAnswerKey, answer_key_store

logger = get_logger(__name__)

_MAX_KEY_TOKENS = 12_000


@dataclass
class ExtractedKey:
    draft: AnswerKeyDraft
    halaman: int
    halaman_ocr: int
    degraded: bool
    model: str


async def extract_key(
    berkas: Path,
    *,
    owner: str,
    judul_tugas: str | None = None,
    kelas: str | None = None,
    mata_pelajaran: str | None = None,
) -> ExtractedKey:
    """Baca berkas kunci jawaban menjadi struktur yang bisa dinilai."""
    document = await extract_document(
        berkas,
        max_ocr_pages=settings.max_answer_key_pages,
        vision_reader=buat_pembaca(owner),
        # Guru akan memeriksa hasilnya, jadi teks hasil OCR tidak dikoreksi
        # otomatis di sini.
    )

    teks = truncate_to_tokens(document.text.strip(), _MAX_KEY_TOKENS)
    if not teks:
        raise InvalidRequest(
            "Berkas kunci jawaban tidak menghasilkan teks. Bila berkas berupa "
            "hasil pindai, pastikan gambar terbaca jelas."
        )

    draft, completion = await llm.structured(
        task=Task.KEY_EXTRACT,
        messages=build_kunci_messages(
            teks_kunci=teks,
            judul_tugas=judul_tugas,
            kelas=kelas,
            mata_pelajaran=mata_pelajaran,
            jumlah_halaman=document.pages,
            memakai_ocr=document.used_ocr,
        ),
        owner=owner,
        schema=AnswerKeyDraft,
    )

    return ExtractedKey(
        draft=draft,
        halaman=document.pages,
        halaman_ocr=document.ocr_pages,
        degraded=completion.degraded,
        model=completion.response.model,
    )


# --------------------------------------------------------------------------- #
# Langkah penyusunan hasil ekstraksi
# --------------------------------------------------------------------------- #


def _build_payload(
    record: StoredAnswerKey,
    draft: AnswerKeyDraft,
    hasil: ExtractedKey,
    judul_tugas: str | None,
    kelas: str | None,
    mata_pelajaran: str | None,
) -> dict[str, object]:
    """Seluruh hasil ekstraksi sebagai data, siap dikirim ke guru.

    Judul, kelas, dan mata pelajaran yang diisi guru mengalahkan tebakan
    model, karena guru yang tahu konteks tugasnya.
    """
    return {
        "answer_key_id": record.id,
        "judul_tugas": draft.judul_tugas or (judul_tugas or ""),
        "kelas": draft.kelas or (kelas or ""),
        "mata_pelajaran": draft.mata_pelajaran or (mata_pelajaran or ""),
        "confidence": round(draft.confidence, 3),
        "needs_review": draft.needs_review,
        "issues": draft.issues,
        "jumlah_butir": len(draft.items),
        "halaman_dibaca": hasil.halaman,
        "halaman_ocr": hasil.halaman_ocr,
        "items": [item.model_dump() for item in draft.items],
        "model": hasil.model,
        "degraded": hasil.degraded,
        "berlaku_sampai_menit": settings.answer_key_ttl_minutes,
        "petunjuk": (
            "Periksa butir dan kunci jawaban di atas. Kirim kembali lewat "
            "kolom kunci_jawaban pada /v1/assessments/grade bila ada yang "
            "perlu dibetulkan."
        ),
    }


def _write_json_artifact(job: Job, payload: dict[str, object]) -> Path:
    artifact = job.tmp_dir / "kunci-jawaban.json"
    artifact.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return artifact


async def _finish(
    job: Job,
    *,
    draft: AnswerKeyDraft,
    payload: dict[str, object],
    artifact: Path,
    degraded: bool,
) -> None:
    """Tandai job selesai.

    Status `needs_review` dipakai bila ada bagian yang tidak terbaca dengan
    yakin: guru harus melihatnya lebih dulu sebelum kunci ini dipakai
    menilai.
    """
    await job_store.update(
        job.id,
        state=JobState.NEEDS_REVIEW if draft.needs_review else JobState.DONE,
        progress=100,
        message=(
            "Ekstraksi selesai, tetapi perlu diperiksa guru."
            if draft.needs_review
            else "Ekstraksi selesai."
        ),
        artifact_path=artifact,
        artifact_name=artifact.name,
        artifact_media_type="application/json",
        json_path=artifact,
        result=payload,
        merge_meta={
            "degraded_tasks": [Task.KEY_EXTRACT.value] if degraded else []
        },
    )


# --------------------------------------------------------------------------- #
# Fungsi utama
# --------------------------------------------------------------------------- #


async def run_answer_key_job(
    job: Job,
    *,
    berkas: Path,
    owner: str,
    judul_tugas: str | None,
    kelas: str | None,
    mata_pelajaran: str | None,
) -> None:
    """Baca berkas kunci jawaban, simpan hasilnya, lalu tandai job selesai.

    Urutan: baca berkas → simpan kunci di memori → tulis JSON hasil →
    tandai selesai. Guru memeriksa hasilnya sebelum kunci dipakai menilai.
    """
    await job_store.update(
        job.id, progress=10, message="Membaca berkas kunci jawaban."
    )
    hasil = await extract_key(
        berkas,
        owner=owner,
        judul_tugas=judul_tugas,
        kelas=kelas,
        mata_pelajaran=mata_pelajaran,
    )
    draft = hasil.draft

    await job_store.update(
        job.id, progress=85, message="Menyimpan hasil ekstraksi."
    )
    record = await answer_key_store.put(
        owner=owner, draft=draft, sumber=berkas.name
    )

    payload = _build_payload(
        record, draft, hasil, judul_tugas, kelas, mata_pelajaran
    )
    artifact = _write_json_artifact(job, payload)
    await _finish(
        job, draft=draft, payload=payload, artifact=artifact,
        degraded=hasil.degraded,
    )

    logger.info(
        "Kunci jawaban terekstraksi",
        job_id=job.id,
        butir=len(draft.items),
        confidence=draft.confidence,
        perlu_review=draft.needs_review,
    )