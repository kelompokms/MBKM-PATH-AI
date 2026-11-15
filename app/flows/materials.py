"""Alur fitur 1 dan 2 — menjelaskan dan membuat materi.

Keduanya satu pipeline. Perbedaannya hanya apakah pengajar menentukan judul
atau membiarkan model menentukannya dari pertanyaan.

Urutannya: baca berkas rujukan (opsional) → minta model menyusun materi
terstruktur → tegakkan flag elemen di sisi server → buat diagram dan
ilustrasi → susun PDF. Setiap kegagalan di tahap gambar tidak menggagalkan
dokumen, karena gambar adalah pelengkap, bukan isi.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.core.errors import InvalidRequest
from app.core.llm import Completion, llm
from app.core.naming import slugify
from app.core.tasks import Task
from app.core.tokens import truncate_to_tokens
from app.domain.documents import IllustrationImage, MaterialDocument
from app.domain.schemas import IllustrationSpec, MaterialOutput
from app.ingest.documents import extract_document
from app.ingest.vision import buat_pembaca
from app.jobs import Job, JobState, job_store
from app.observability.logger import get_logger
from app.prompts.materi import build_materi_messages
from app.render.pdf import build_material_pdf, pdf_page_count

logger = get_logger(__name__)

_MAX_ILLUSTRATION_CONCURRENCY = 2


@dataclass
class MaterialRequest:
    pertanyaan: str | None = None
    judul: str | None = None
    deskripsi_materi: str = ""
    jenjang: str | None = None
    tabel: bool = False
    diagram: bool = False
    ilustrasi: bool = False
    kombinasi: bool = False
    materi_path: Path | None = None
    # Teks materi yang dikirim langsung, tanpa berkas. Dipakai jalur pelajar:
    # backend sudah memegang materi guru, jadi tidak perlu mengunggah berkas
    # yang sama pada setiap pertanyaan.
    materi_text: str | None = None
    # "teacher" atau "student". Menentukan prompt yang dipakai.
    audience: str = "teacher"


def resolve_flags(request: MaterialRequest) -> dict[str, bool]:
    """`kombinasi=true` menyalakan ketiga elemen sekaligus.

    Bila kombinasi mati, flag satu per satu yang berlaku. Ini satu-satunya
    tempat aturan itu diterapkan, supaya model tidak pernah menerima flag
    mentah dan menafsirkannya sendiri.
    """
    if request.kombinasi:
        return {"tabel": True, "diagram": True, "ilustrasi": True}
    return {
        "tabel": request.tabel,
        "diagram": request.diagram,
        "ilustrasi": request.ilustrasi,
    }


def _enforce_flags(output: MaterialOutput, flags: dict[str, bool]) -> MaterialOutput:
    """Buang elemen yang tidak diminta, walaupun model menambahkannya.

    Model sering tetap menyisipkan tabel atau diagram karena bentuk JSON-nya
    menyediakan tempat. Keputusan akhir tetap di server.
    """
    if not flags.get("tabel"):
        for section in output.isi:
            section.tabel = None
    if not flags.get("diagram"):
        output.diagram = None
    if not flags.get("ilustrasi"):
        output.ilustrasi = []
    else:
        output.ilustrasi = output.ilustrasi[: settings.max_illustrations]
    return output


async def _reference_text(
    path: Path | None, text: str | None = None, *, owner: str
) -> str | None:
    """Teks materi rujukan, dari berkas atau dari teks yang dikirim langsung.

    Dua jalur ini menghasilkan hal yang sama. Berkas dipakai saat pengajar
    mengunggah materi; teks langsung dipakai saat backend mengirim ulang
    materi yang sudah dibaca sebelumnya, sehingga berkas yang sama tidak
    perlu diunggah pada setiap pertanyaan pelajar.
    """
    if path is not None:
        document = await extract_document(path, vision_reader=buat_pembaca(owner))
        isi = document.text.strip()
        if not isi:
            raise InvalidRequest(
                "Berkas materi rujukan tidak menghasilkan teks. Bila berkas "
                "berupa hasil pindai, pastikan gambar terbaca jelas."
            )
        return truncate_to_tokens(isi, settings.max_reference_tokens)

    if text and text.strip():
        return truncate_to_tokens(text.strip(), settings.max_reference_tokens)

    return None


async def _render_illustrations(
    output: MaterialOutput, owner: str
) -> list[IllustrationImage]:
    """Buat ilustrasi secara paralel, lalu buang yang gagal.

    Kegagalan satu ilustrasi tidak menggagalkan yang lain maupun dokumen:
    hasil yang gagal hanya menghasilkan `None` yang disaring di akhir.
    """
    if not output.ilustrasi or not settings.model_image:
        return []

    semaphore = asyncio.Semaphore(_MAX_ILLUSTRATION_CONCURRENCY)

    async def one(spec: IllustrationSpec) -> IllustrationImage | None:
        async with semaphore:
            gambar = await llm.generate_image(
                prompt=spec.prompt_gambar, owner=owner
            )
        if not gambar:
            return None
        return IllustrationImage(
            judul=spec.judul, keterangan=spec.keterangan, image=gambar
        )

    results = await asyncio.gather(
        *(one(spec) for spec in output.ilustrasi), return_exceptions=True
    )

    images: list[IllustrationImage] = []
    for result in results:
        if isinstance(result, IllustrationImage):
            images.append(result)
    return images


# --------------------------------------------------------------------------- #
# Langkah penyusunan materi
# --------------------------------------------------------------------------- #


async def _request_material(
    request: MaterialRequest,
    flags: dict[str, bool],
    reference: str | None,
    owner: str,
) -> tuple[MaterialOutput, Completion]:
    """Minta model menyusun materi, lalu buang elemen yang tidak diminta."""
    messages = build_materi_messages(
        pertanyaan=request.pertanyaan,
        judul=request.judul,
        deskripsi_materi=request.deskripsi_materi,
        materi_rujukan=reference,
        jenjang=request.jenjang,
        flags=flags,
        audience=request.audience,
    )
    output, completion = await llm.structured(
        task=Task.MATERIAL_CONTENT,
        messages=messages,
        owner=owner,
        schema=MaterialOutput,
    )
    return _enforce_flags(output, flags), completion


def _build_document(
    request: MaterialRequest,
    output: MaterialOutput,
    images: list[IllustrationImage],
) -> MaterialDocument:
    """Rakit dokumen siap cetak. Judul dari pengajar menang atas judul model."""
    return MaterialDocument(
        judul=request.judul or output.judul,
        deskripsi_singkat=output.deskripsi_singkat,
        pertanyaan=request.pertanyaan,
        isi=output.isi,
        kesimpulan=output.kesimpulan,
        diagram=output.diagram,
        ilustrasi=images,
    )


async def _write_pdf_artifact(
    job: Job, document: MaterialDocument
) -> tuple[Path, bytes]:
    """Susun PDF di luar event loop, lalu simpan di direktori job."""
    pdf_bytes = await asyncio.to_thread(build_material_pdf, document)
    artifact = job.tmp_dir / f"{slugify(document.judul, fallback='materi')}.pdf"
    artifact.write_bytes(pdf_bytes)
    return artifact, pdf_bytes


def _build_result(
    document: MaterialDocument,
    images: list[IllustrationImage],
    flags: dict[str, bool],
    completion: Completion,
    pdf_bytes: bytes,
) -> dict[str, object]:
    """Ringkasan hasil untuk berkas status job.

    `diagram_diminta` dan `ilustrasi_diminta` dicatat terpisah dari jumlah
    yang benar-benar jadi: ilustrasi yang gagal dibuat tetap terlihat
    sebagai "diminta", supaya kegagalan tidak tersembunyi.
    """
    return {
        "judul": document.judul,
        "deskripsi_singkat": document.deskripsi_singkat,
        "jumlah_bagian": len(document.isi),
        "elemen": {
            "tabel": sum(1 for s in document.isi if s.tabel is not None),
            "diagram": 1 if document.diagram else 0,
            "ilustrasi": len(images),
            "diagram_diminta": flags["diagram"],
            "ilustrasi_diminta": flags["ilustrasi"],
        },
        "halaman_pdf": pdf_page_count(pdf_bytes),
        "model": completion.response.model,
        "degraded": completion.degraded,
    }


async def _finish(
    job: Job,
    *,
    result: dict[str, object],
    artifact: Path,
    degraded: bool,
) -> None:
    await job_store.update(
        job.id,
        state=JobState.DONE,
        progress=100,
        message="Selesai.",
        artifact_path=artifact,
        artifact_name=artifact.name,
        artifact_media_type="application/pdf",
        result=result,
        merge_meta={
            "degraded_tasks": [Task.MATERIAL_CONTENT.value] if degraded else []
        },
    )


# --------------------------------------------------------------------------- #
# Fungsi utama
# --------------------------------------------------------------------------- #


async def run_material_job(
    job: Job, *, request: MaterialRequest, owner: str
) -> None:
    """Susun materi untuk fitur 1 dan 2, lalu tulis PDF-nya.

    Urutan: baca berkas rujukan → minta model → tegakkan flag elemen →
    buat ilustrasi → susun PDF dan tandai job selesai.
    """
    flags = resolve_flags(request)

    await job_store.update(
        job.id, progress=8, message="Membaca berkas materi rujukan."
    )
    reference = await _reference_text(
        request.materi_path, request.materi_text, owner=owner
    )

    await job_store.update(
        job.id, progress=25, message="Menyusun materi dengan AI."
    )
    output, completion = await _request_material(request, flags, reference, owner)

    await job_store.update(
        job.id, progress=62, message="Menyiapkan diagram dan ilustrasi."
    )
    images = await _render_illustrations(output, owner)

    await job_store.update(job.id, progress=82, message="Menyusun berkas PDF.")
    document = _build_document(request, output, images)
    artifact, pdf_bytes = await _write_pdf_artifact(job, document)

    await _finish(
        job,
        result=_build_result(document, images, flags, completion, pdf_bytes),
        artifact=artifact,
        degraded=completion.degraded,
    )

    logger.info(
        "Materi selesai",
        job_id=job.id,
        bagian=len(document.isi),
        ilustrasi=len(images),
        degraded=completion.degraded,
    )