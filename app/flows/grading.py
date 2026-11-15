"""Alur fitur 3 — koreksi jawaban siswa.

Model dinilai per butir, bukan per jawaban utuh. Konsekuensinya:

- nilai akhir tidak pernah dihitung model, melainkan oleh `domain.scoring`,
- kutipan bukti yang tidak benar-benar ada di jawaban siswa dibuang,
- butir yang tidak dinilai model dianggap belum dinilai, bukan nol.

Siswa yang berkasnya bermasalah atau yang penilaiannya gagal tidak diberi
angka karangan. Mereka dilaporkan terpisah sebagai tidak dapat dinilai,
sehingga laporan tidak pernah menampilkan nilai yang tidak pernah dihitung.

Berkas ini dipecah menjadi langkah-langkah kecil. `run_grading_job` hanya
menyusun urutannya; seluruh pekerjaan ada pada fungsi yang namanya
menjelaskan dirinya sendiri.
"""

from __future__ import annotations

import asyncio
import json
import statistics
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.core.errors import AnswerKeyNeedsReview, InvalidRequest
from app.core.llm import llm
from app.core.naming import slugify
from app.core.tasks import Task
from app.domain.documents import BarisRekap, RekapKoreksi
from app.domain.models import BatchIngest, StudentSubmission
from app.domain.schemas import AnswerKeyDraft, ClassSummaryDraft, StudentGradeDraft
from app.domain.scoring import (
    IncompleteGrading,
    compute_score,
    distribute_weights,
    evidence_is_verifiable,
    label_for,
)
from app.flows.answer_keys import extract_key
from app.ingest.batch import JawabanMasuk, muat_jawaban
from app.ingest.vision import buat_pembaca
from app.jobs import Job, JobState, job_store
from app.observability.logger import get_logger
from app.prompts.penilaian import build_penilaian_messages, build_ringkasan_messages
from app.render.excel import EXCEL_MEDIA_TYPE, build_rekap_koreksi

logger = get_logger(__name__)

# Berapa kali satu siswa boleh dinilai ulang bila hasilnya tidak lengkap.
_MAX_GRADE_ATTEMPTS = 2

# Batas token untuk keluaran penilaian satu siswa.
_GRADE_MAX_TOKENS = 2_400

# Judul rekap. Samanya dengan judul di template Excel, yang tetap dipakai apa
# adanya sebagai judul di berkasnya.
JUDUL_REKAP = "Analisa Capaian Hasil Belajar Siswa"



# Saran yang ditulis untuk siswa yang berkasnya tidak bisa dinilai. Ditaruh di
# kolom saran supaya pengajar tahu langkah berikutnya, bukan hanya melihat nilai
# kosong tanpa penjelasan.
_SARAN_TIDAK_DINILAI = "Minta siswa mengirim ulang berkas jawabannya."


@dataclass
class GradingContext:
    """Data guru dan tugas yang menyertai satu permintaan penilaian."""

    # `None` bila guru mengunggah berkas kunci langsung; alur akan
    # mengekstraksinya lebih dulu sebelum menilai.
    answer_key: AnswerKeyDraft | None
    pengajar: str
    mata_pelajaran: str
    kelas: str
    judul_tugas: str
    # True bila kunci dikirim langsung oleh guru dalam bentuk JSON.
    # Kunci seperti itu sudah melewati pemeriksaan manusia, jadi gerbang
    # `needs_review` tidak berlaku untuknya.
    key_verified: bool = False


@dataclass
class ScoringPlan:
    """Semua yang dibutuhkan untuk menilai satu batch.

    Dibungkus dalam satu objek supaya `weights`, `expected`, dan `key_json`
    tidak berkeliling sebagai tiga variabel terpisah ke banyak fungsi.
    """

    weights: dict[int, float]
    expected: set[int]
    key_json: str


@dataclass
class StudentResult:
    """Satu siswa yang berhasil dinilai."""

    nomor: int
    email: str
    nama: str
    nilai: float
    label: str
    saran: str
    detail: list[dict[str, object]]


@dataclass
class FailedStudent:
    """Satu siswa yang tidak bisa dinilai, beserta alasannya."""

    nomor: int
    email: str
    nama: str
    alasan: str


# ---------------------------------------------------------------------------
# Langkah 1 — siapkan kunci jawaban
# ---------------------------------------------------------------------------


async def _prepare_answer_key(
    job: Job,
    *,
    owner: str,
    context: GradingContext,
    kunci_path: Path | None,
) -> AnswerKeyDraft:
    """Ambil kunci jawaban yang akan dipakai menilai.

    Dua sumber: kunci yang sudah ada di konteks (dikirim guru atau hasil
    `/answer-keys/extract`), atau berkas kunci yang perlu diekstraksi dulu.
    """
    if context.answer_key is not None:
        return context.answer_key

    if kunci_path is None:
        raise InvalidRequest(
            "Tidak ada kunci jawaban yang bisa dipakai.",
            http_status=422,
        )

    await job_store.update(
        job.id, progress=4, message="Membaca berkas kunci jawaban."
    )
    hasil = await extract_key(
        kunci_path,
        owner=owner,
        judul_tugas=context.judul_tugas,
        kelas=context.kelas,
        mata_pelajaran=context.mata_pelajaran,
    )

    context.answer_key = hasil.draft
    context.judul_tugas = context.judul_tugas or hasil.draft.judul_tugas
    return hasil.draft


def _ensure_key_usable(
    answer_key: AnswerKeyDraft,
    *,
    context: GradingContext,
    on_low_confidence: str,
) -> None:
    """Hentikan penilaian bila kunci belum diperiksa guru.

    Kunci yang dibaca salah akan membuat seluruh nilai satu kelas salah,
    jadi keadaan itu tidak boleh lewat tanpa keputusan sadar.
    """
    needs_verification = answer_key.needs_review and not context.key_verified
    if needs_verification and on_low_confidence != "continue":
        raise AnswerKeyNeedsReview(
            "Kunci jawaban belum diverifikasi guru "
            f"(keyakinan {answer_key.confidence:.2f}, "
            f"{len(answer_key.issues)} catatan)."
        )


def _build_scoring_plan(
    context: GradingContext, answer_key: AnswerKeyDraft
) -> ScoringPlan:
    """Rangkum kunci menjadi bobot, daftar butir, dan teks JSON-nya."""
    weights = distribute_weights(
        [(item.nomor, item.bobot) for item in answer_key.items]
    )
    return ScoringPlan(
        weights=weights,
        expected=set(weights),
        key_json=_build_key_json(context, answer_key),
    )


def _build_key_json(context: GradingContext, answer_key: AnswerKeyDraft) -> str:
    """Kunci jawaban sebagai teks JSON, siap ditempelkan ke pesan ke model."""
    return json.dumps(
        {
            "judul_tugas": context.judul_tugas,
            "kelas": context.kelas,
            "mata_pelajaran": context.mata_pelajaran,
            "items": [
                {
                    "nomor": item.nomor,
                    "butir_soal": item.butir_soal,
                    "kunci_jawaban": item.kunci_jawaban,
                    "jenis": item.jenis,
                    "bobot": item.bobot,
                    "kriteria": item.kriteria,
                }
                for item in answer_key.items
            ],
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Langkah 2 — nilai satu siswa
# ---------------------------------------------------------------------------


def _failed(submission: StudentSubmission, alasan: str) -> FailedStudent:
    return FailedStudent(
        nomor=submission.index,
        email=submission.email,
        nama=submission.nama,
        alasan=alasan,
    )


def _result_from(
    submission: StudentSubmission,
    draft: StudentGradeDraft,
    nilai: float,
    detail: list[dict[str, object]],
) -> StudentResult:
    """Rakit hasil satu siswa dari keluaran model dan nilai yang dihitung server."""
    return StudentResult(
        nomor=submission.index,
        email=submission.email,
        nama=submission.nama,
        nilai=nilai,
        label=label_for(nilai),
        saran=draft.saran.strip(),
        detail=detail,
    )


def _detail_row(
    item, weights: dict[int, float], student_text: str
) -> dict[str, object]:
    """Satu baris rincian butir untuk berkas JSON.

    Kutipan bukti dari model hanya dipakai bila benar-benar ditemukan di
    jawaban siswa. Kutipan karangan dibuang, supaya laporan tidak pernah
    menampilkan bukti yang tidak ada.
    """
    kutipan = item.kutipan_bukti.strip()
    terverifikasi = bool(kutipan) and evidence_is_verifiable(kutipan, student_text)
    return {
        "nomor": item.nomor,
        "skor": round(item.skor, 2),
        "bobot": weights.get(item.nomor, 1.0),
        "catatan": item.catatan,
        "kutipan": kutipan if terverifikasi else "",
        "kutipan_terverifikasi": terverifikasi,
    }


def _detail_number(row: dict[str, object]) -> int:
    return int(row["nomor"])


def _collect_scores(
    draft: StudentGradeDraft,
    plan: ScoringPlan,
    student_text: str,
) -> tuple[dict[int, float], list[dict[str, object]]]:
    """Ubah keluaran model menjadi skor per butir dan baris rincian.

    Butir yang tidak ada di kunci diabaikan: model kadang menambah nomor
    yang tidak pernah diminta.
    """
    scores: dict[int, float] = {}
    detail: list[dict[str, object]] = []

    for item in draft.nilai_per_item:
        if item.nomor not in plan.expected:
            continue
        scores[item.nomor] = item.skor
        detail.append(_detail_row(item, plan.weights, student_text))

    detail.sort(key=_detail_number)
    return scores, detail


def _retry_prompt(missing: list[int]) -> dict[str, str]:
    """Pesan tambahan yang menyebut butir mana yang belum dinilai."""
    return {
        "role": "user",
        "content": (
            "Butir berikut belum dinilai: "
            + ", ".join(str(nomor) for nomor in missing)
            + ". Nilai sekarang seluruh butir pada kunci, "
            "termasuk butir yang tidak dijawab siswa (beri 0)."
        ),
    }


def _incomplete_note(missing: list[int]) -> str:
    return (
        "Penilaian tidak lengkap: butir "
        + ", ".join(str(nomor) for nomor in missing)
        + " tidak dinilai."
    )


async def _request_grade(
    submission: StudentSubmission,
    *,
    plan: ScoringPlan,
    owner: str,
    missing: list[int],
    attempt: int,
) -> StudentGradeDraft | None:
    """Minta model menilai satu jawaban.

    Galat tidak dilempar ke atas: satu siswa yang gagal tidak boleh
    mematikan penilaian seluruh kelas. Pemanggil menerima `None` dan
    memutuskan sendiri apakah masih ada kesempatan ulang.
    """
    messages = build_penilaian_messages(
        kunci_json=plan.key_json,
        jawaban_siswa=submission.text,
        nomor_siswa=submission.index,
    )
    if missing:
        messages.append(_retry_prompt(missing))

    try:
        draft, _ = await llm.structured(
            task=Task.ITEM_GRADE,
            messages=messages,
            owner=owner,
            schema=StudentGradeDraft,
            temperature=0.0,
            max_tokens=_GRADE_MAX_TOKENS,
        )
        return draft
    except Exception as exc:  # satu siswa tidak mematikan batch
        logger.warning(
            "Penilaian siswa gagal",
            student_index=submission.index,
            attempt=attempt,
            error=type(exc).__name__,
        )
        return None


async def _grade_student(
    submission: StudentSubmission,
    *,
    plan: ScoringPlan,
    owner: str,
) -> StudentResult | FailedStudent:
    """Nilai satu siswa, dengan satu kesempatan ulang bila perlu.

    Siswa yang tetap gagal dikembalikan sebagai `FailedStudent`, bukan diberi
    nilai nol.
    """
    missing: list[int] = []

    for attempt in range(1, _MAX_GRADE_ATTEMPTS + 1):
        last_attempt = attempt >= _MAX_GRADE_ATTEMPTS

        draft = await _request_grade(
            submission,
            plan=plan,
            owner=owner,
            missing=missing,
            attempt=attempt,
        )
        if draft is None:
            if last_attempt:
                return _failed(submission, "Model AI gagal menilai jawaban ini.")
            continue

        scores, detail = _collect_scores(draft, plan, submission.text)

        try:
            nilai = compute_score(scores, plan.weights, expected=plan.expected)
        except IncompleteGrading as exc:
            # Rumus nilai menolak menghitung selama masih ada butir yang
            # belum dinilai. Kirim pengingat pada percobaan berikutnya.
            if last_attempt:
                return _failed(submission, _incomplete_note(exc.missing))
            missing = exc.missing
            continue

        return _result_from(submission, draft, nilai, detail)

    return _failed(submission, "Penilaian tidak dapat diselesaikan.")


# ---------------------------------------------------------------------------
# Langkah 3 — baca dan nilai satu batch
# ---------------------------------------------------------------------------


async def _read_batch(
    job: Job, jawaban: JawabanMasuk
) -> BatchIngest:
    """Baca ZIP jawaban siswa menjadi daftar kiriman berisi teks."""
    await job_store.update(
        job.id, progress=8, message="Membaca kumpulan jawaban siswa."
    )

    async def progress(index: int, total: int) -> None:
        await job_store.update(
            job.id,
            progress=8 + int(24 * index / max(1, total)),
            message=f"Membaca berkas jawaban {index} dari {total}.",
        )

    return await muat_jawaban(
        jawaban,
        workdir=job.tmp_dir / "jawaban",
        progress=progress,
        vision_reader=buat_pembaca(job.owner),
    )


async def _grade_all_students(
    job: Job,
    batch: BatchIngest,
    *,
    plan: ScoringPlan,
    owner: str,
) -> list[StudentResult | FailedStudent]:
    """Nilai seluruh siswa secara paralel, dibatasi `grade_concurrency`.

    Batas itu penting: tanpa batas, satu batch 100 siswa akan mengirim 100
    permintaan sekaligus dan menabrak pembatasan laju penyedia model.
    """
    students = batch.graded
    total = len(students)

    await job_store.update(
        job.id, progress=34, message=f"Menilai {total} jawaban siswa."
    )
    if total == 0:
        return []

    semaphore = asyncio.Semaphore(max(1, settings.grade_concurrency))
    selesai = 0  # dilindungi `lock`
    lock = asyncio.Lock()

    async def nilai_satu(submission: StudentSubmission):
        nonlocal selesai
        async with semaphore:
            hasil = await _grade_student(submission, plan=plan, owner=owner)
        async with lock:
            selesai += 1
            await job_store.update(
                job.id,
                progress=34 + int(46 * selesai / total),
                message=f"Menilai jawaban siswa {selesai} dari {total}.",
            )
        return hasil

    return await asyncio.gather(*(nilai_satu(item) for item in students))


def _split_outcomes(
    outcomes: list[StudentResult | FailedStudent], batch: BatchIngest
) -> tuple[list[StudentResult], list[FailedStudent]]:
    """Pisahkan siswa yang berhasil dinilai dari yang tidak bisa dinilai.

    Urutan daftar "tidak bisa dinilai" mengikuti keluaran JSON: berkas yang
    bermasalah lebih dulu, lalu yang gagal saat dinilai.
    """
    berhasil = [item for item in outcomes if isinstance(item, StudentResult)]
    gagal_dinilai = [item for item in outcomes if isinstance(item, FailedStudent)]
    berkas_bermasalah = [
        _failed(item, item.skipped_reason or "Berkas tidak dapat dibaca.")
        for item in batch.skipped
    ]
    return berhasil, [*berkas_bermasalah, *gagal_dinilai]


def _ensure_any_graded(results: list[StudentResult]) -> None:
    """Batch yang tidak menghasilkan satu nilai pun tidak diteruskan.

    Lebih baik berhenti dengan pesan yang jelas daripada menulis laporan
    kosong yang membuat pengajar mengira siswa tidak mengumpulkan tugas.
    """
    if not results:
        raise InvalidRequest(
            "Tidak ada siswa yang berhasil dinilai. Periksa kunci jawaban dan "
            "berkas jawaban siswa.",
            http_status=422,
        )


def _average_of(pair: tuple[int, float]) -> float:
    """Pengurut untuk `sorted`: ambil angkanya dari pasangan (nomor, rata-rata)."""
    return pair[1]


def _summarise_class(
    results: list[StudentResult], weights: dict[int, float]
) -> dict[str, object]:
    """Ringkasan angka kelas. Seluruhnya dihitung di sini, bukan oleh model."""
    values = [result.nilai for result in results]
    per_item: dict[int, list[float]] = {nomor: [] for nomor in weights}

    for result in results:
        for row in result.detail:
            nomor = int(row["nomor"])
            if nomor in per_item:
                per_item[nomor].append(float(row["skor"]))

    rata_butir = {
        nomor: round(statistics.fmean(scores), 2)
        for nomor, scores in per_item.items()
        if scores
    }
    terlemah = sorted(rata_butir.items(), key=_average_of)[:3]

    label_count: dict[str, int] = {}
    for result in results:
        label_count[result.label] = label_count.get(result.label, 0) + 1

    return {
        "jumlah_dinilai": len(values),
        "rata_rata": round(statistics.fmean(values), 2) if values else 0.0,
        "nilai_terendah": min(values) if values else 0.0,
        "nilai_tertinggi": max(values) if values else 0.0,
        "median": round(statistics.median(values), 2) if values else 0.0,
        "sebaran_label": label_count,
        "rata_rata_per_butir": rata_butir,
        "butir_terlemah": [
            {"nomor": nomor, "rata_rata": nilai} for nomor, nilai in terlemah
        ],
    }


# ---------------------------------------------------------------------------
# Langkah 4 — ringkasan kelas
# ---------------------------------------------------------------------------


async def _build_class_summary(
    owner: str, stats: dict[str, object], results: list[StudentResult]
) -> tuple[list[str], list[str]]:
    """Minta ringkasan naratif kelas beserta daftar task yang turun kualitas.

    Ringkasan ini pelengkap. Bila gagal, laporan tetap disusun dengan
    keterangan pengganti — angka pada tabel tidak pernah bergantung padanya.
    """
    try:
        ringkasan, completion = await llm.structured(
            task=Task.CLASS_SUMMARY,
            messages=build_ringkasan_messages(
                statistik_json=json.dumps(stats, ensure_ascii=False),
                jumlah_siswa=len(results),
            ),
            owner=owner,
            schema=ClassSummaryDraft,
        )
    except Exception as exc:  # naratif tidak boleh mematikan laporan
        logger.warning("Ringkasan kelas gagal dibuat", error=type(exc).__name__)
        return _fallback_summary(len(results), stats["rata_rata"]), []

    deskripsi = [par.strip() for par in ringkasan.deskripsi if par.strip()]
    degraded = [Task.CLASS_SUMMARY.value] if completion.degraded else []
    return deskripsi, degraded


def _fallback_summary(jumlah_siswa: int, rata_rata: object) -> list[str]:
    return [
        f"Sebanyak {jumlah_siswa} jawaban siswa berhasil dinilai dengan "
        f"rata-rata {rata_rata}.",
        "Ringkasan naratif tidak dapat dibuat pada proses ini. "
        "Angka pada tabel tetap hasil perhitungan sistem.",
    ]


# ---------------------------------------------------------------------------
# Langkah 5 — susun laporan
# ---------------------------------------------------------------------------


def _build_notes(jumlah_tidak_dinilai: int) -> list[str]:
    """Keterangan yang ikut ditulis di bawah deskripsi analisa.

    Template tidak menyediakan baris catatan, jadi keterangan ini menjadi
    paragraf terakhir di sel deskripsi. Yang paling penting dipertahankan:
    laporan ini hasil bantuan AI dan perlu ditinjau pengajar.
    """
    catatan = [
        "Nilai dihitung dari skor per butir dengan bobot pada kunci jawaban.",
        "Rekap ini dihasilkan dengan bantuan AI dan perlu ditinjau pengajar.",
    ]
    if jumlah_tidak_dinilai:
        catatan.append(
            f"{jumlah_tidak_dinilai} siswa tidak dapat dinilai. Nilainya ditulis "
            "0 dan alasannya ada pada kolom keterangan."
        )
    return catatan


def _excel_baris(
    results: list[StudentResult], tidak_dinilai: list[FailedStudent]
) -> list[BarisRekap]:
    """Baris rekap: siswa yang dinilai, lalu siswa yang berkasnya gagal dibaca.

    Siswa yang tidak dapat dinilai tetap ditulis, dengan nilai 0 dan alasannya
    di kolom keterangan. Menghilangkannya akan membuat rekap satu kelas tampak
    lengkap padahal ada siswa yang tidak ikut dinilai.
    """
    baris = [
        BarisRekap(
            nama=result.nama,
            email=result.email,
            nilai=float(result.nilai),
            keterangan=result.label,
            saran=result.saran,
        )
        for result in results
    ]

    baris.extend(
        BarisRekap(
            nama=item.nama,
            email=item.email,
            nilai=0.0,
            keterangan=item.alasan,
            saran=_SARAN_TIDAK_DINILAI,
        )
        for item in tidak_dinilai
    )

    return baris


def _build_payload(
    context: GradingContext,
    deskripsi: list[str],
    stats: dict[str, object],
    results: list[StudentResult],
    tidak_dinilai: list[FailedStudent],
    degraded_tasks: list[str],
    jumlah_baris: int,
) -> dict[str, object]:
    """Seluruh hasil sebagai data, siap ditulis menjadi berkas JSON."""
    return {
        "judul_laporan": JUDUL_REKAP,
        "pengajar": context.pengajar,
        "mata_pelajaran": context.mata_pelajaran,
        "kelas": context.kelas,
        "tugas": context.judul_tugas,
        "deskripsi_hasil_analisa": deskripsi,
        "statistik": stats,
        "siswa": [
            {
                "nomor": result.nomor,
                "email": result.email,
                "nama": result.nama,
                "nilai": result.nilai,
                "label": result.label,
                "saran": result.saran,
                "nilai_per_butir": result.detail,
            }
            for result in results
        ],
        "tidak_dinilai": [
            {
                "nomor": item.nomor,
                "email": item.email,
                "nama": item.nama,
                "alasan": item.alasan,
            }
            for item in tidak_dinilai
        ],
        "jumlah_baris": jumlah_baris,
        "degraded_tasks": degraded_tasks,
    }


async def _publish_results(
    job: Job,
    *,
    context: GradingContext,
    stats: dict[str, object],
    results: list[StudentResult],
    tidak_dinilai: list[FailedStudent],
    deskripsi: list[str],
    degraded_tasks: list[str],
) -> None:
    """Susun berkas Excel dan JSON, lalu tandai job selesai."""
    baris = _excel_baris(results, tidak_dinilai)

    rekap = RekapKoreksi(
        pengajar=context.pengajar,
        mapel=context.mata_pelajaran,
        kelas=context.kelas,
        tugas=context.judul_tugas,
        deskripsi=[*deskripsi, *_build_notes(len(tidak_dinilai))],
        baris=baris,
    )

    isi_excel = await asyncio.to_thread(build_rekap_koreksi, rekap)
    artifact = job.tmp_dir / f"{_nama_berkas(context)}.xlsx"
    artifact.write_bytes(isi_excel)

    payload = _build_payload(
        context, deskripsi, stats, results, tidak_dinilai, degraded_tasks, len(baris)
    )
    result_json = job.tmp_dir / "laporan-koreksi.json"
    result_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

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
        merge_meta={"degraded_tasks": degraded_tasks},
    )


def _nama_berkas(context: GradingContext) -> str:
    """Nama berkas rekap, memuat kelas dan judul tugas supaya mudah dicari."""
    return f"rekap-koreksi-ai-{slugify(context.kelas)}-{slugify(context.judul_tugas)}"


# ---------------------------------------------------------------------------
# Fungsi utama
# ---------------------------------------------------------------------------


async def run_grading_job(
    job: Job,
    *,
    owner: str,
    context: GradingContext,
    jawaban: JawabanMasuk,
    on_low_confidence: str,
    kunci_path: Path | None = None,
) -> None:
    """Nilai satu batch jawaban siswa, lalu tulis Excel + JSON.

    Urutan langkah: siapkan kunci -> pastikan kunci boleh dipakai -> baca
    jawaban -> nilai semua siswa -> pisahkan hasil -> ringkasan kelas ->
    susun rekap. Setiap langkah punya fungsi sendiri di atas.
    """
    answer_key = await _prepare_answer_key(
        job, owner=owner, context=context, kunci_path=kunci_path
    )
    _ensure_key_usable(
        answer_key, context=context, on_low_confidence=on_low_confidence
    )
    plan = _build_scoring_plan(context, answer_key)

    batch = await _read_batch(job, jawaban)
    outcomes = await _grade_all_students(job, batch, plan=plan, owner=owner)

    results, tidak_dinilai = _split_outcomes(outcomes, batch)
    _ensure_any_graded(results)

    stats = _summarise_class(results, plan.weights)

    await job_store.update(
        job.id, progress=84, message="Menyusun ringkasan hasil analisa kelas."
    )
    deskripsi, degraded_tasks = await _build_class_summary(owner, stats, results)

    await job_store.update(job.id, progress=92, message="Menyusun berkas Excel.")
    await _publish_results(
        job, context=context, stats=stats, results=results,
        tidak_dinilai=tidak_dinilai, deskripsi=deskripsi, degraded_tasks=degraded_tasks,
    )

    logger.info(
        "Koreksi selesai",
        job_id=job.id,
        dinilai=len(results),
        tidak_dinilai=len(tidak_dinilai),
        rata_rata=stats["rata_rata"],
    )
