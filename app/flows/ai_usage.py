"""Alur fitur 5 — pemeriksaan indikasi penggunaan AI pada jawaban siswa.

Hibrida: setiap jawaban dinilai dengan ciri yang bisa dihitung tanpa biaya.
Hanya jawaban yang jatuh di band ambigu (35–65) yang dikirim ke model, karena
di situlah ciri angka tidak cukup untuk memutuskan.

Angka akhir tetap dipegang sistem:

- di bawah ambang bawah, model tidak dipanggil sama sekali;
- di atas ambang atas, model tidak dipanggil sama sekali;
- di antaranya, nilai model dijepit ke ±12 poin dari angka heuristik.

Dengan begitu hasilnya tidak melompat liar antarsiswa, dan biaya tetap
rendah karena sebagian besar jawaban tidak menyentuh model.

Berkas ini dipecah per langkah: baca jawaban → nilai heuristik → minta
pendapat model untuk band ambigu → susun laporan.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.core.errors import InvalidRequest
from app.core.llm import llm
from app.core.tasks import Task
from app.domain.aidetect import HeuristicResult, analyze, band_for
from app.domain.documents import ReportDocument
from app.domain.models import BatchIngest, StudentSubmission
from app.domain.schemas import AiDetectDraft
from app.ingest.batch import JawabanMasuk, muat_jawaban
from app.ingest.vision import buat_pembaca
from app.jobs import Job, JobState, job_store
from app.observability.logger import get_logger
from app.prompts.indikasi import build_indikasi_messages
from app.render.pdf import build_report_pdf, pdf_page_count

logger = get_logger(__name__)

JUDUL_LAPORAN = "Pemeriksaan Penggunaan AI pada Jawaban Siswa"

KOLOM_LAPORAN = ["No", "Email", "Nama", "Persentase Indikasi Penggunaan AI"]
LEBAR_KOLOM = [0.07, 0.31, 0.30, 0.32]
KOLOM_TENGAH = [0, 3]
KOLOM_KECIL = [1]

# Seberapa jauh model boleh menyimpang dari angka heuristik.
_SELISIH_MAKS = 12.0

# Nama band tempat model dilibatkan. Band ini tidak bisa diputuskan oleh
# ciri teks saja, jadi angka heuristik dianggap belum cukup.
_BAND_AMBIGU = "Sedang"

CATATAN_LAPORAN = [
    "Indikasi dihitung dari ciri teks: keseragaman kalimat, frasa "
    "penghubung baku, kerapian tanda baca, dan tidak adanya ragam "
    "santai. Hasil ini bukan bukti penggunaan AI.",
    "Rincian bukti untuk setiap siswa tersedia pada berkas JSON.",
]

DISCLAIMER = (
    "Angka pada laporan ini adalah indikasi statistik, bukan bukti. "
    "Jangan gunakan sebagai satu-satunya dasar tindakan."
)


@dataclass
class IndicationContext:
    """Data guru dan tugas yang menyertai satu permintaan pemeriksaan."""

    pengajar: str
    mata_pelajaran: str
    kelas: str
    judul_tugas: str
    jenjang: str | None = None


@dataclass
class Indication:
    """Hasil pemeriksaan satu siswa."""

    nomor: int
    email: str
    nama: str
    persentase: float
    band: str
    bukti: list[str]
    dinilai_ai: bool


@dataclass
class _ModelVerdict:
    """Penilaian model untuk satu jawaban di band ambigu."""

    persentase: float
    bukti: list[str]


@dataclass
class _IndicationStats:
    """Angka ringkasan yang dipakai deskripsi dan berkas JSON."""

    jumlah_siswa: int
    rata_rata: float
    jumlah_rendah: int
    jumlah_sedang: int
    jumlah_tinggi: int
    perlu_model: int
    dinilai_model: int


# ---------------------------------------------------------------------------
# Langkah 1 — baca jawaban siswa
# ---------------------------------------------------------------------------


async def _read_batch(job: Job, jawaban: JawabanMasuk) -> BatchIngest:
    await job_store.update(
        job.id, progress=10, message="Membaca kumpulan jawaban siswa."
    )

    async def progress(index: int, total: int) -> None:
        await job_store.update(
            job.id,
            progress=10 + int(28 * index / max(1, total)),
            message=f"Membaca berkas jawaban {index} dari {total}.",
        )

    return await muat_jawaban(
        jawaban,
        workdir=job.tmp_dir / "jawaban",
        progress=progress,
        vision_reader=buat_pembaca(job.owner),
    )


def _require_submissions(submissions: list[StudentSubmission]) -> None:
    if not submissions:
        raise InvalidRequest(
            "Tidak ada jawaban siswa yang bisa dibaca.",
            http_status=422,
        )


# ---------------------------------------------------------------------------
# Langkah 2 — nilai setiap jawaban
# ---------------------------------------------------------------------------


async def _ask_model(
    submission: StudentSubmission,
    *,
    heuristik: HeuristicResult,
    bukti: list[str],
    owner: str,
    jenjang: str | None,
) -> _ModelVerdict | None:
    """Minta model menilai satu jawaban di band ambigu.

    Nilai model dijepit ke sekitar angka heuristik. Bila model gagal, `None`
    dikembalikan dan angka heuristik yang dipakai — satu kegagalan tidak
    boleh membatalkan seluruh pemeriksaan.
    """
    low = max(0.0, heuristik.skor - _SELISIH_MAKS)
    high = min(100.0, heuristik.skor + _SELISIH_MAKS)

    try:
        draft, _ = await llm.structured(
            task=Task.AI_DETECT,
            messages=build_indikasi_messages(
                teks=submission.text,
                skor_heuristik=heuristik.skor,
                sinyal=bukti,
                batas_bawah=low,
                batas_atas=high,
                jenjang=jenjang,
            ),
            owner=owner,
            schema=AiDetectDraft,
        )
    except Exception as exc:  # tetap pakai angka heuristik
        logger.warning(
            "Penilai indikasi AI gagal, memakai heuristik",
            student_index=submission.index,
            error=type(exc).__name__,
        )
        return None

    return _ModelVerdict(
        persentase=min(high, max(low, draft.persentase)),
        bukti=draft.bukti[:3] if draft.bukti else bukti,
    )


async def _assess_one(
    submission: StudentSubmission,
    *,
    heuristik: HeuristicResult,
    owner: str,
    jenjang: str | None,
) -> Indication:
    """Nilai satu jawaban: heuristik lebih dulu, model hanya bila ambigu."""
    persentase = heuristik.skor
    bukti = heuristik.bukti()
    dinilai_ai = False

    if heuristik.band == _BAND_AMBIGU:
        verdict = await _ask_model(
            submission,
            heuristik=heuristik,
            bukti=bukti,
            owner=owner,
            jenjang=jenjang,
        )
        if verdict is not None:
            persentase = verdict.persentase
            bukti = verdict.bukti
            dinilai_ai = True

    return Indication(
        nomor=submission.index,
        email=submission.email,
        nama=submission.nama,
        persentase=round(persentase, 2),
        band=band_for(persentase),
        bukti=bukti[:3],
        dinilai_ai=dinilai_ai,
    )


async def _assess_all(
    job: Job,
    submissions: list[StudentSubmission],
    heuristik: list[HeuristicResult],
    *,
    owner: str,
    jenjang: str | None,
) -> list[Indication]:
    """Nilai seluruh jawaban secara paralel, dibatasi `grade_concurrency`."""
    await job_store.update(
        job.id,
        progress=42,
        message=f"Menganalisis {len(submissions)} jawaban siswa.",
    )

    semaphore = asyncio.Semaphore(max(1, settings.grade_concurrency))

    async def nilai_satu(
        submission: StudentSubmission, hasil: HeuristicResult
    ) -> Indication:
        async with semaphore:
            return await _assess_one(
                submission, heuristik=hasil, owner=owner, jenjang=jenjang
            )

    return await asyncio.gather(
        *(
            nilai_satu(submission, hasil)
            for submission, hasil in zip(submissions, heuristik, strict=True)
        )
    )


# ---------------------------------------------------------------------------
# Langkah 3 — angka ringkasan dan narasinya
# ---------------------------------------------------------------------------


def _summarise(
    outcomes: list[Indication], perlu_model: int
) -> _IndicationStats:
    persentase = [item.persentase for item in outcomes]
    return _IndicationStats(
        jumlah_siswa=len(outcomes),
        rata_rata=sum(persentase) / len(persentase),
        jumlah_rendah=sum(1 for value in persentase if value < settings.ai_low),
        jumlah_sedang=sum(
            1
            for value in persentase
            if settings.ai_low <= value <= settings.ai_high
        ),
        jumlah_tinggi=sum(1 for value in persentase if value > settings.ai_high),
        perlu_model=perlu_model,
        dinilai_model=sum(1 for item in outcomes if item.dinilai_ai),
    )


def _build_deskripsi(stats: _IndicationStats) -> list[str]:
    return [
        f"Pemeriksaan dilakukan pada {stats.jumlah_siswa} jawaban siswa. "
        f"Rata-rata indikasi berada pada {stats.rata_rata:.2f}%.",
        f"Sebanyak {stats.jumlah_rendah} siswa berada pada indikasi rendah, "
        f"{stats.jumlah_sedang} pada indikasi sedang, dan "
        f"{stats.jumlah_tinggi} pada indikasi tinggi. Dari {stats.jumlah_siswa} "
        f"jawaban, {stats.perlu_model} di antaranya memerlukan pemeriksaan "
        "tambahan karena berada di rentang yang tidak dapat diputuskan oleh "
        "ciri teks saja.",
        "Angka ini adalah indikasi, bukan bukti. Tulisan siswa yang rapi dan "
        "terlatih dapat memperoleh nilai tinggi tanpa bantuan AI, dan tulisan "
        "hasil AI yang disunting dapat lolos. Gunakan tabel sebagai bahan "
        "pemeriksaan, bukan sebagai dasar sanksi.",
    ]


# ---------------------------------------------------------------------------
# Langkah 4 — susun laporan
# ---------------------------------------------------------------------------


def _report_rows(outcomes: list[Indication]) -> list[list[str]]:
    return [
        [str(item.nomor), item.email, item.nama, f"{item.persentase:.2f}%"]
        for item in outcomes
    ]


def _build_report(
    context: IndicationContext,
    outcomes: list[Indication],
    deskripsi: list[str],
) -> ReportDocument:
    return ReportDocument(
        judul=JUDUL_LAPORAN,
        pengajar=context.pengajar,
        mata_pelajaran=context.mata_pelajaran,
        kelas=context.kelas,
        tugas=context.judul_tugas,
        deskripsi=deskripsi,
        kolom=KOLOM_LAPORAN,
        baris=_report_rows(outcomes),
        lebar_kolom=LEBAR_KOLOM,
        kolom_tengah=KOLOM_TENGAH,
        kolom_kecil=KOLOM_KECIL,
        catatan=CATATAN_LAPORAN,
    )


def _build_payload(
    context: IndicationContext,
    document: ReportDocument,
    outcomes: list[Indication],
    skipped: list[StudentSubmission],
    stats: _IndicationStats,
    pdf_bytes: bytes,
) -> dict[str, object]:
    """Seluruh hasil sebagai data, siap ditulis menjadi berkas JSON."""
    return {
        "judul_laporan": document.judul,
        "pengajar": context.pengajar,
        "mata_pelajaran": context.mata_pelajaran,
        "kelas": context.kelas,
        "tugas": context.judul_tugas,
        "deskripsi_hasil_analisa": document.deskripsi,
        "disclaimer": DISCLAIMER,
        "statistik": {
            "jumlah_diperiksa": stats.jumlah_siswa,
            "rata_rata": round(stats.rata_rata, 2),
            "jumlah_rendah": stats.jumlah_rendah,
            "jumlah_sedang": stats.jumlah_sedang,
            "jumlah_tinggi": stats.jumlah_tinggi,
            "dinilai_model": stats.dinilai_model,
        },
        "siswa": [
            {
                "nomor": item.nomor,
                "email": item.email,
                "nama": item.nama,
                "persentase_indikasi_ai": item.persentase,
                "band": item.band,
                "bukti": item.bukti,
                "dinilai_model": item.dinilai_ai,
            }
            for item in outcomes
        ],
        "tidak_diperiksa": [
            {
                "email": item.email,
                "nama": item.nama,
                "alasan": item.skipped_reason,
            }
            for item in skipped
        ],
        "halaman_pdf": pdf_page_count(pdf_bytes),
    }


async def _write_pdf_artifact(
    job: Job, document: ReportDocument
) -> tuple[Path, bytes]:
    """Susun PDF di luar event loop, lalu simpan di direktori job."""
    pdf_bytes = await asyncio.to_thread(build_report_pdf, document)
    artifact = job.tmp_dir / "laporan-indikasi-ai.pdf"
    artifact.write_bytes(pdf_bytes)
    return artifact, pdf_bytes


def _write_json_artifact(job: Job, payload: dict[str, object]) -> Path:
    result_json = job.tmp_dir / "laporan-indikasi-ai.json"
    result_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result_json


async def _publish(
    job: Job,
    *,
    context: IndicationContext,
    outcomes: list[Indication],
    skipped: list[StudentSubmission],
    stats: _IndicationStats,
) -> None:
    """Susun laporan indikasi AI (PDF + JSON), lalu tandai job selesai."""
    await job_store.update(
        job.id, progress=80, message="Menyusun laporan indikasi penggunaan AI."
    )

    document = _build_report(context, outcomes, _build_deskripsi(stats))
    artifact, pdf_bytes = await _write_pdf_artifact(job, document)
    payload = _build_payload(context, document, outcomes, skipped, stats, pdf_bytes)
    result_json = _write_json_artifact(job, payload)

    await job_store.update(
        job.id,
        state=JobState.DONE,
        progress=100,
        message="Selesai.",
        artifact_path=artifact,
        artifact_name=artifact.name,
        artifact_media_type="application/pdf",
        json_path=result_json,
        result=payload,
    )


# ---------------------------------------------------------------------------
# Fungsi utama
# ---------------------------------------------------------------------------


async def run_ai_usage_job(
    job: Job,
    *,
    owner: str,
    context: IndicationContext,
    jawaban: JawabanMasuk,
) -> None:
    """Periksa indikasi penggunaan AI satu batch jawaban, lalu tulis PDF + JSON.

    Urutan langkah: baca jawaban → hitung ciri heuristik → minta pendapat
    model untuk band ambigu → susun laporan.
    """
    batch = await _read_batch(job, jawaban)
    submissions: list[StudentSubmission] = batch.graded
    _require_submissions(submissions)

    heuristik = [analyze(item.text) for item in submissions]
    perlu_model = sum(1 for item in heuristik if item.band == _BAND_AMBIGU)

    outcomes = await _assess_all(
        job, submissions, heuristik, owner=owner, jenjang=context.jenjang
    )
    stats = _summarise(outcomes, perlu_model)

    await _publish(
        job,
        context=context,
        outcomes=outcomes,
        skipped=batch.skipped,
        stats=stats,
    )

    logger.info(
        "Pemeriksaan indikasi AI selesai",
        job_id=job.id,
        siswa=len(submissions),
        dinilai_model=stats.dinilai_model,
    )
