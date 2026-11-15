"""Alur fitur 4 — pemeriksaan kemiripan jawaban antarsiswa.

Angka dasar dihitung sepenuhnya oleh algoritme. Model hanya dipanggil untuk
pasangan yang berada di pita abu-abu, dan nilainya dibatasi di sekitar angka
deterministik. Dengan begitu, kesalahan model tidak bisa mengubah urutan
siswa secara liar, tetapi penilaian tetap bisa menangkap parafrase berat
yang tidak terlihat oleh perbandingan kata.

Satu hal yang sengaja tidak dilakukan: menyebut hasil ini "plagiasi".
Kemiripan tinggi adalah alasan untuk memeriksa, bukan bukti pelanggaran.

Berkas ini dipecah per langkah: baca jawaban → hitung kemiripan → wasiti
pita abu-abu → susun laporan. `run_similarity_job` hanya menyusun urutannya.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.core.errors import InvalidRequest
from app.core.llm import llm
from app.core.naming import slugify
from app.core.tasks import Task
from app.domain.documents import BarisPlagiasi, RekapPlagiasi
from app.domain.models import BatchIngest, StudentSubmission
from app.domain.schemas import PairJudgeDraft
from app.domain.similarity import (
    PairResult,
    SimilarityReport,
    build_pairs,
    describe_similarity,
    finalize,
    select_grey_band,
)
from app.ingest.batch import JawabanMasuk, muat_jawaban
from app.ingest.vision import buat_pembaca
from app.jobs import Job, JobState, job_store
from app.observability.logger import get_logger
from app.prompts.indikasi import build_kemiripan_messages
from app.render.excel import EXCEL_MEDIA_TYPE, build_rekap_plagiasi

logger = get_logger(__name__)

# Judul rekap. Samanya dengan judul di template Excel, yang tetap dipakai apa
# adanya sebagai judul di berkasnya.
JUDUL_REKAP = "Analisa Plagiasi Siswa"

# Seberapa jauh model boleh menyimpang dari angka deterministik.
_SELISIH_MAKS = 15.0

# Pemeriksaan kemiripan tidak bermakna dengan kurang dari dua siswa.
_MIN_STUDENTS = 2


@dataclass
class SimilarityContext:
    """Data guru dan tugas yang menyertai satu permintaan pemeriksaan."""

    pengajar: str
    mata_pelajaran: str
    kelas: str
    judul_tugas: str


@dataclass
class _SimilarityStats:
    """Angka ringkasan yang dipakai deskripsi dan berkas JSON."""

    jumlah_siswa: int
    tertinggi: float
    rata_rata: float
    jumlah_sedang: int
    jumlah_tinggi: int
    pasangan_ai: int


@dataclass
class _SimilarityOutcome:
    """Seluruh bahan yang dibutuhkan untuk menyusun laporan akhir."""

    submissions: list[StudentSubmission]
    report: SimilarityReport
    pairs: list[PairResult]
    skipped: list[StudentSubmission]
    stats: _SimilarityStats


# ---------------------------------------------------------------------------
# Langkah 1 — baca jawaban siswa
# ---------------------------------------------------------------------------


async def _read_batch(
    job: Job, jawaban: JawabanMasuk
) -> BatchIngest:
    await job_store.update(
        job.id, progress=10, message="Membaca kumpulan jawaban siswa."
    )

    async def progress(index: int, total: int) -> None:
        await job_store.update(
            job.id,
            progress=10 + int(30 * index / max(1, total)),
            message=f"Membaca berkas jawaban {index} dari {total}.",
        )

    return await muat_jawaban(
        jawaban,
        workdir=job.tmp_dir / "jawaban",
        progress=progress,
        vision_reader=buat_pembaca(job.owner),
    )


def _require_enough_students(submissions: list[StudentSubmission]) -> None:
    """Perbandingan antarsiswa butuh minimal dua jawaban yang bisa dibaca."""
    if len(submissions) < _MIN_STUDENTS:
        raise InvalidRequest(
            "Pemeriksaan kemiripan membutuhkan minimal dua jawaban siswa yang "
            "bisa dibaca.",
            http_status=422,
            details={"terbaca": len(submissions)},
        )


# ---------------------------------------------------------------------------
# Langkah 2 — hitung kemiripan, lalu wasiti pita abu-abu
# ---------------------------------------------------------------------------


def _pair_similarity(pair: PairResult) -> float:
    """Pengurut untuk `sorted`: dari pasangan paling mirip ke paling beda."""
    return pair.skor


async def _compute_pairs(texts: list[str]) -> list[PairResult]:
    """Hitung kemiripan seluruh pasangan. Deterministik dan tanpa model."""
    return await asyncio.to_thread(
        build_pairs, texts, shingle_size=settings.sim_shingle_size
    )


async def _judge_pair(
    pair: PairResult, *, texts: list[str], owner: str
) -> PairResult:
    """Sempurnakan satu pasangan pita abu-abu dengan bantuan model.

    Kegagalan di sini tidak fatal: pasangan dikembalikan apa adanya dengan
    angka deterministik.
    """
    base = pair.skor * 100
    low = max(0.0, base - _SELISIH_MAKS)
    high = min(100.0, base + _SELISIH_MAKS)

    try:
        draft, completion = await llm.structured(
            task=Task.PAIR_JUDGE,
            messages=build_kemiripan_messages(
                teks_a=texts[pair.a],
                teks_b=texts[pair.b],
                skor_deterministik=base,
                batas_bawah=low,
                batas_atas=high,
            ),
            owner=owner,
            schema=PairJudgeDraft,
        )
    except Exception as exc:  # gagal wasit tetap pakai angka dasar
        logger.warning(
            "Wasit kemiripan gagal, memakai angka deterministik",
            error=type(exc).__name__,
        )
        return pair

    if completion.degraded:
        logger.info("Wasit kemiripan memakai model gratis")

    # Jepit ke rentang supaya model tidak melompat jauh dari hitungan sistem.
    nilai = min(high, max(low, draft.persentase)) / 100
    return PairResult(
        a=pair.a,
        b=pair.b,
        jaccard=pair.jaccard,
        cosine=pair.cosine,
        skor=round(nilai, 4),
        sumber="ai",
    )


async def _refine_grey_band(
    job: Job, pairs: list[PairResult], *, texts: list[str], owner: str
) -> tuple[list[PairResult], int]:
    """Kirim hanya pasangan pita abu-abu ke model.

    Mengembalikan pasangan yang sudah disempurnakan, beserta berapa pasangan
    yang benar-benar diperiksa model (dipakai pada berkas JSON).
    """
    grey = select_grey_band(
        pairs,
        low=settings.sim_llm_low,
        high=settings.sim_llm_high,
        max_per_student=settings.sim_llm_max_pairs,
    )
    if not grey:
        return pairs, 0

    await job_store.update(
        job.id,
        progress=60,
        message=f"Memeriksa {len(grey)} pasangan yang perlu ditinjau.",
    )

    semaphore = asyncio.Semaphore(max(1, settings.grade_concurrency))

    async def wasiti(satu: PairResult) -> PairResult:
        async with semaphore:
            return await _judge_pair(satu, texts=texts, owner=owner)

    refined = await asyncio.gather(*(wasiti(pair) for pair in grey))
    refined_map = {(item.a, item.b): item for item in refined}
    return [refined_map.get((pair.a, pair.b), pair) for pair in pairs], len(grey)


# ---------------------------------------------------------------------------
# Langkah 3 — susun angka ringkasan dan narasinya
# ---------------------------------------------------------------------------


def _summarise(
    report: SimilarityReport, jumlah_siswa: int, pasangan_ai: int
) -> _SimilarityStats:
    nilai = report.tertinggi
    return _SimilarityStats(
        jumlah_siswa=jumlah_siswa,
        tertinggi=max(nilai) if nilai else 0.0,
        rata_rata=sum(nilai) / len(nilai),
        jumlah_sedang=sum(
            1
            for value in nilai
            if settings.sim_llm_low * 100 <= value < settings.sim_llm_high * 100
        ),
        jumlah_tinggi=sum(
            1 for value in nilai if value >= settings.sim_llm_high * 100
        ),
        pasangan_ai=pasangan_ai,
    )


def _build_deskripsi(stats: _SimilarityStats) -> list[str]:
    return [
        f"Pemeriksaan dilakukan pada {stats.jumlah_siswa} jawaban siswa yang "
        f"berhasil dibaca. Setiap jawaban dibandingkan dengan seluruh jawaban "
        f"lain dalam batch ini.",
        f"Rata-rata kemiripan tertinggi per siswa adalah {stats.rata_rata:.2f}%, "
        f"dengan nilai tertinggi {stats.tertinggi:.2f}%. Sebanyak "
        f"{stats.jumlah_sedang} siswa berada pada rentang kemiripan sedang dan "
        f"{stats.jumlah_tinggi} siswa pada rentang tinggi.",
        "Persentase pada tabel menunjukkan kemiripan tertinggi setiap siswa "
        "terhadap satu siswa lain. Kemiripan tinggi adalah alasan untuk "
        "memeriksa lebih lanjut, bukan bukti pelanggaran.",
    ]


def _build_catatan(jumlah_skipped: int) -> list[str]:
    catatan = [
        "Kemiripan dihitung dari potongan kata dan kemiripan kosakata, "
        "bukan dari penilaian benar-salah.",
        "Angka pada tabel adalah kemiripan tertinggi terhadap siswa lain "
        "dalam batch yang sama, bukan rata-rata seluruh kelas.",
    ]
    if jumlah_skipped:
        catatan.append(
            f"{jumlah_skipped} siswa tidak dapat dibaca dan tidak "
            "diperiksa. Rinciannya ada pada berkas JSON."
        )
    return catatan


# ---------------------------------------------------------------------------
# Langkah 4 — susun laporan
# ---------------------------------------------------------------------------


def _excel_baris(
    submissions: list[StudentSubmission], report: SimilarityReport
) -> list[BarisPlagiasi]:
    """Baris rekap: satu siswa satu angka, yaitu kemiripan tertingginya.

    Angka tunggal per siswa inilah yang diminta template. Rincian "mirip dengan
    siapa" tetap ada di berkas JSON, karena satu kolom tidak cukup untuk itu.
    """
    return [
        BarisPlagiasi(
            nama=item.nama,
            email=item.email,
            persentase=round(report.tertinggi[index], 2),
        )
        for index, item in enumerate(submissions)
    ]


def _build_rekap(
    context: SimilarityContext,
    submissions: list[StudentSubmission],
    report: SimilarityReport,
    deskripsi: list[str],
    catatan: list[str],
) -> RekapPlagiasi:
    return RekapPlagiasi(
        pengajar=context.pengajar,
        mapel=context.mata_pelajaran,
        kelas=context.kelas,
        tugas=context.judul_tugas,
        deskripsi=[*deskripsi, *catatan],
        baris=_excel_baris(submissions, report),
    )


def _keterangan_for(value: float) -> str:
    return describe_similarity(
        value, low=settings.sim_llm_low, high=settings.sim_llm_high
    )


def _mirip_dengan(
    submissions: list[StudentSubmission], report: SimilarityReport, index: int
) -> str | None:
    """Nama siswa yang paling mirip, bila ada."""
    partner = report.pasangan_tertinggi[index]
    return submissions[partner].nama if partner is not None else None


def _student_payload_rows(
    submissions: list[StudentSubmission], report: SimilarityReport
) -> list[dict[str, object]]:
    return [
        {
            "nomor": index + 1,
            "email": submissions[index].email,
            "nama": submissions[index].nama,
            "persentase_kemiripan": round(report.tertinggi[index], 2),
            "keterangan": _keterangan_for(report.tertinggi[index]),
            "paling_mirip_dengan": _mirip_dengan(submissions, report, index),
        }
        for index in range(len(submissions))
    ]


def _pair_payload_rows(
    submissions: list[StudentSubmission], pairs: list[PairResult]
) -> list[dict[str, object]]:
    """Hanya pasangan yang kemiripannya layak disebut, dari yang tertinggi."""
    candidates = [pair for pair in pairs if pair.skor >= settings.sim_llm_low]
    candidates = sorted(candidates, key=_pair_similarity, reverse=True)
    return [
        {
            "siswa_a": submissions[pair.a].nama,
            "siswa_b": submissions[pair.b].nama,
            "kemiripan": round(pair.skor * 100, 2),
            "jaccard": round(pair.jaccard * 100, 2),
            "cosine": round(pair.cosine * 100, 2),
            "dinilai_ai": pair.sumber == "ai",
        }
        for pair in candidates
    ]


def _build_payload(
    context: SimilarityContext,
    rekap: RekapPlagiasi,
    outcome: _SimilarityOutcome,
) -> dict[str, object]:
    """Seluruh hasil sebagai data, siap ditulis menjadi berkas JSON."""
    return {
        "judul_laporan": JUDUL_REKAP,
        "pengajar": context.pengajar,
        "mata_pelajaran": context.mata_pelajaran,
        "kelas": context.kelas,
        "tugas": context.judul_tugas,
        "deskripsi_hasil_analisa": rekap.deskripsi,
        "statistik": {
            "jumlah_diperiksa": outcome.stats.jumlah_siswa,
            "rata_rata_tertinggi": round(outcome.stats.rata_rata, 2),
            "tertinggi": round(outcome.stats.tertinggi, 2),
            "jumlah_sedang": outcome.stats.jumlah_sedang,
            "jumlah_tinggi": outcome.stats.jumlah_tinggi,
            "pasangan_diperiksa_ai": outcome.stats.pasangan_ai,
        },
        "siswa": _student_payload_rows(outcome.submissions, outcome.report),
        "pasangan": _pair_payload_rows(outcome.submissions, outcome.pairs),
        "tidak_diperiksa": [
            {
                "email": item.email,
                "nama": item.nama,
                "alasan": item.skipped_reason,
            }
            for item in outcome.skipped
        ],
        "jumlah_baris": len(rekap.baris),
    }


async def _publish(
    job: Job,
    *,
    context: SimilarityContext,
    submissions: list[StudentSubmission],
    report: SimilarityReport,
    pairs: list[PairResult],
    skipped: list[StudentSubmission],
    pasangan_ai: int,
) -> None:
    """Susun rekap Excel dan JSON, lalu tandai job selesai."""
    stats = _summarise(report, len(submissions), pasangan_ai)

    await job_store.update(
        job.id, progress=80, message="Menyusun rekap kemiripan."
    )

    rekap = _build_rekap(
        context,
        submissions,
        report,
        deskripsi=_build_deskripsi(stats),
        catatan=_build_catatan(len(skipped)),
    )
    outcome = _SimilarityOutcome(
        submissions=submissions,
        report=report,
        pairs=pairs,
        skipped=skipped,
        stats=stats,
    )

    artifact = await _write_excel_artifact(job, rekap)
    payload = _build_payload(context, rekap, outcome)
    result_json = _write_json_artifact(job, payload)
    await _finish(job, artifact=artifact, result_json=result_json, payload=payload)


async def _write_excel_artifact(job: Job, rekap: RekapPlagiasi) -> Path:
    """Susun berkas Excel di luar event loop, lalu simpan di direktori job."""
    isi = await asyncio.to_thread(build_rekap_plagiasi, rekap)
    artifact = job.tmp_dir / f"{_nama_berkas(rekap)}.xlsx"
    artifact.write_bytes(isi)
    return artifact


def _nama_berkas(rekap: RekapPlagiasi) -> str:
    return f"rekap-plagiasi-ai-{slugify(rekap.kelas)}-{slugify(rekap.tugas)}"


def _write_json_artifact(job: Job, payload: dict[str, object]) -> Path:
    result_json = job.tmp_dir / "laporan-kemiripan.json"
    result_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result_json


async def _finish(
    job: Job,
    *,
    artifact: Path,
    result_json: Path,
    payload: dict[str, object],
) -> None:
    await job_store.update(
        job.id,
        state=JobState.DONE,
        progress=100,
        message="Selesai.",
        artifact_path=artifact,
        artifact_name=artifact.name,
        artifact_media_type=EXCEL_MEDIA_TYPE,
        json_path=result_json,
        result=payload,
    )


# ---------------------------------------------------------------------------
# Fungsi utama
# ---------------------------------------------------------------------------


async def run_similarity_job(
    job: Job,
    *,
    owner: str,
    context: SimilarityContext,
    jawaban: JawabanMasuk,
) -> None:
    """Periksa kemiripan satu batch jawaban, lalu tulis Excel + JSON.

    Urutan langkah: baca jawaban → hitung pasangan → wasiti pita abu-abu →
    susun rekap. Setiap langkah punya fungsi sendiri di atas.
    """
    batch = await _read_batch(job, jawaban)
    submissions: list[StudentSubmission] = batch.graded
    _require_enough_students(submissions)

    await job_store.update(
        job.id,
        progress=45,
        message=f"Menghitung kemiripan {len(submissions)} jawaban.",
    )

    texts = [item.text for item in submissions]
    pairs = await _compute_pairs(texts)
    pairs, pasangan_ai = await _refine_grey_band(
        job, pairs, texts=texts, owner=owner
    )

    report = finalize(
        len(submissions), pairs, low=settings.sim_llm_low, high=settings.sim_llm_high
    )
    await _publish(
        job,
        context=context,
        submissions=submissions,
        report=report,
        pairs=pairs,
        skipped=batch.skipped,
        pasangan_ai=pasangan_ai,
    )

    logger.info(
        "Pemeriksaan kemiripan selesai",
        job_id=job.id,
        siswa=len(submissions),
        pasangan_ai=pasangan_ai,
    )
