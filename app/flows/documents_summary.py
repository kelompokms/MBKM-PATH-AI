"""Alur fitur 6 — ringkasan dokumen panjang.

Kenapa dokumennya tidak dibaca sekaligus. Dokumen 39 halaman berisi sekitar
40.000 token. Model kecil yang menerima sebanyak itu cenderung hanya mengingat
bagian awal dan akhir, sedangkan bagian tengahnya hilang. Percobaan nyata
memperlihatkannya: laporan hanya menyebut nama jurnal, penulis, dan afiliasi
penelitinya, sedangkan metode dan pembahasannya tidak muncul sama sekali.

Karena itu dokumen dipotong lebih dulu, setiap potongan diringkas sendiri-sendiri
secara paralel, lalu ringkasan-ringkasannya digabung menjadi satu telaah.
Jumlah token yang dibaca tetap sebanyak dokumennya; yang berbeda adalah cara
model membacanya.

Kegagalan satu potongan tidak menggagalkan seluruh pekerjaan. Potongan yang
gagal dicatat, dan laporan menyebutkannya apa adanya, supaya tidak ada laporan
yang terlihat membahas seluruh dokumen padahal ada bagian yang tidak terbaca.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.core.errors import BudgetExhausted, InvalidRequest
from app.core.llm import Completion, llm
from app.core.naming import slugify
from app.core.tasks import Task, spec_for
from app.core.tokens import split_into_chunks, truncate_to_tokens
from app.domain.documents import ReviewDocument
from app.domain.schemas import ChunkSummary, DocumentOverview, ReviewSection
from app.ingest.documents import ExtractedDocument, extract_document
from app.ingest.vision import buat_pembaca
from app.jobs import Job, JobState, job_store
from app.observability.ledger import ledger
from app.observability.logger import get_logger
from app.prompts.ringkasan import build_chunk_messages, build_synthesis_messages
from app.render.pdf import build_review_pdf, pdf_page_count

logger = get_logger(__name__)

# Rentang laporan kemajuan untuk tahap meringkas potongan.
_PROGRESS_AWAL = 15
_PROGRESS_AKHIR = 70

_KOLOM_PETA = ["Bagian", "Letak di dokumen", "Judul bagian", "Status"]


@dataclass
class DocumentRequest:
    """Masukan fitur ringkasan dokumen."""

    materi_path: Path
    judul: str | None = None
    fokus: str | None = None
    jenjang: str | None = None
    diagram: bool = False


@dataclass
class _Potongan:
    """Satu potongan dokumen beserta hasil ringkasannya.

    `letak` adalah penanda posisi di dokumen aslinya: rentang halaman untuk
    dokumen berhalaman, atau nomor bagian untuk TXT dan DOCX yang tidak punya
    halaman.
    """

    nomor: int
    letak: str
    ringkasan: ChunkSummary | None = None
    galat: Exception | None = None

    @property
    def berhasil(self) -> bool:
        return self.ringkasan is not None


@dataclass
class _Bahan:
    """Hasil tahap pembacaan: potongan teks dokumen beserta letaknya."""

    dokumen: ExtractedDocument
    terpotong: bool
    halaman_dibaca: int
    potongan: list[_Potongan]
    teks_potongan: list[str]


async def run_summarize_job(
    job: Job, *, request: DocumentRequest, owner: str
) -> None:
    """Ringkas dokumen panjang secara bertahap, lalu susun PDF-nya."""
    bahan = await _siapkan(job, request=request, owner=owner)

    await _ringkas_semua(
        job,
        potongan=bahan.potongan,
        teks=bahan.teks_potongan,
        owner=owner,
    )

    if not any(item.berhasil for item in bahan.potongan):
        raise _galat_pertama(bahan.potongan)

    wawasan, completion = await _minta_ikhtisar(
        job, request=request, bahan=bahan, owner=owner
    )

    # Badan laporan disusun dari catatan per bagian dokumen, bukan dari keluaran
    # tahap penggabungan. Dengan begitu tidak ada isi yang dipadatkan dua kali,
    # dan panjang laporan mengikuti panjang dokumennya.
    bagian = _susun_bagian(bahan.potongan)

    await _publikasikan(
        job,
        request=request,
        bahan=bahan,
        wawasan=wawasan,
        bagian=bagian,
        completion=completion,
    )


async def _siapkan(
    job: Job, *, request: DocumentRequest, owner: str
) -> _Bahan:
    """Baca dokumen, potong, dan pastikan anggarannya cukup sebelum mulai."""
    await job_store.update(job.id, progress=5, message="Membaca dokumen.")

    dokumen = await extract_document(
        request.materi_path,
        vision_reader=buat_pembaca(owner),
    )
    teks_penuh = dokumen.text.strip()

    if not teks_penuh:
        raise InvalidRequest(
            "Dokumen tidak menghasilkan teks. Bila berkasnya hasil pindai, "
            "pastikan gambarnya terbaca jelas."
        )

    teks, terpotong = _batasi(teks_penuh)
    teks_potongan = _potong(teks)
    potongan = [
        _Potongan(
            nomor=index + 1,
            letak=_penanda_bagian(index, len(teks_potongan), dokumen.pages),
        )
        for index in range(len(teks_potongan))
    ]

    # Diperiksa sebelum satu pun bagian diringkas. Tanpa ini, seluruh bagian
    # terlanjur diringkas lebih dulu, lalu ikhtisarnya gagal karena anggaran
    # habis, dan seluruh token untuk bagian-bagian itu terbuang.
    await _pastikan_anggaran(owner, jumlah_potongan=len(potongan))

    await job_store.update(
        job.id,
        progress=12,
        message=f"Membaca {len(potongan)} bagian dokumen.",
    )

    return _Bahan(
        dokumen=dokumen,
        terpotong=terpotong,
        halaman_dibaca=_halaman_dibaca(
            dokumen.pages,
            teks,
            teks_penuh,
            dilewati=dokumen.ocr_skipped,
        ),
        potongan=potongan,
        teks_potongan=teks_potongan,
    )


async def _minta_ikhtisar(
    job: Job, *, request: DocumentRequest, bahan: _Bahan, owner: str
) -> tuple[DocumentOverview, Completion]:
    """Minta bagian pembuka dan penutup laporan kepada model."""
    await job_store.update(
        job.id,
        progress=76,
        message="Menyusun ikhtisar dari seluruh bagian dokumen.",
    )

    wawasan, completion = await llm.structured(
        task=Task.DOC_SYNTHESIS,
        messages=build_synthesis_messages(
            judul=request.judul or _judul_dari(request.materi_path),
            catatan=_susun_catatan(bahan.potongan),
            fokus=request.fokus,
            jenjang=request.jenjang,
            minta_diagram=request.diagram,
        ),
        owner=owner,
        schema=DocumentOverview,
    )

    # Diagram hanya ditempel bila memang diminta. Ilustrasi sengaja tidak ada
    # di fitur ini: satu gambar berbiaya $0,007, jauh lebih mahal daripada
    # seluruh panggilan teksnya, dan tidak menambah isi telaah.
    if not request.diagram:
        wawasan.diagram = None

    return wawasan, completion


async def _publikasikan(
    job: Job,
    *,
    request: DocumentRequest,
    bahan: _Bahan,
    wawasan: DocumentOverview,
    bagian: list[ReviewSection],
    completion: Completion,
) -> None:
    """Susun PDF dan JSON, lalu tandai pekerjaan selesai."""
    await job_store.update(job.id, progress=88, message="Menyusun berkas PDF.")

    gagal = sum(1 for item in bahan.potongan if not item.berhasil)
    artifact, pdf_bytes, json_path = await _susun_artefak(
        job, request=request, bahan=bahan, wawasan=wawasan, bagian=bagian
    )

    await job_store.update(
        job.id,
        state=JobState.DONE,
        progress=100,
        message="Selesai.",
        artifact_path=artifact,
        artifact_name=artifact.name,
        artifact_media_type="application/pdf",
        json_path=json_path,
        result=_hasil(
            wawasan,
            bagian,
            bahan.potongan,
            bahan.dokumen,
            pdf_bytes,
            completion,
            gagal,
        ),
        merge_meta={
            "degraded_tasks": [Task.DOC_SYNTHESIS.value]
            if completion.degraded
            else []
        },
    )

    logger.info(
        "Ringkasan dokumen selesai",
        job_id=job.id,
        potongan=len(bahan.potongan),
        gagal=gagal,
        halaman=bahan.dokumen.pages,
    )


# --------------------------------------------------------------------------- #
# Tahap pertama: memotong dan meringkas per bagian
# --------------------------------------------------------------------------- #


def _batasi(teks_penuh: str) -> tuple[str, bool]:
    """Batasi panjang dokumen agar jumlah potongannya tidak membengkak."""
    batas = settings.doc_chunk_tokens * settings.doc_max_chunks
    teks = truncate_to_tokens(teks_penuh, batas)
    return teks, len(teks) < len(teks_penuh)


async def _pastikan_anggaran(owner: str, *, jumlah_potongan: int) -> None:
    """Tolak lebih dulu bila anggaran tidak cukup untuk seluruh dokumen.

    Pemeriksaan ini memakai angka yang sama dengan yang dipakai gerbang model
    untuk setiap panggilan, sehingga hasilnya sejalan: yang lolos di sini pasti
    lolos di sana, kecuali bagian terakhir yang memang diperiksa ulang.
    """
    perkiraan = _perkiraan_token(jumlah_potongan)
    sisa = (await ledger.status(owner)).remaining

    if perkiraan <= sisa:
        return

    raise BudgetExhausted(
        "Anggaran token tidak cukup untuk membaca seluruh dokumen ini. "
        f"Perkiraan kebutuhan sekitar {perkiraan:,} token, sedangkan sisa "
        f"anggaran {sisa:,} token. Anggaran diperbarui setiap tengah malam UTC.",
        details={"perkiraan_token": perkiraan, "sisa_token": sisa},
    )


def _perkiraan_token(jumlah_potongan: int) -> int:
    """Perkiraan kebutuhan token satu dokumen utuh."""
    potongan = spec_for(Task.DOC_CHUNK_SUMMARY)
    telaah = spec_for(Task.DOC_SYNTHESIS)

    return (
        (potongan.est_in_tokens + potongan.est_out_tokens) * jumlah_potongan
        + telaah.est_in_tokens
        + telaah.est_out_tokens
    )


def _potong(teks: str) -> list[str]:
    """Potong teks menjadi bagian-bagian, lalu jaga jumlahnya tetap terbatas."""
    potongan = split_into_chunks(
        teks,
        max_tokens=settings.doc_chunk_tokens,
        overlap_tokens=settings.doc_chunk_overlap_tokens,
    )
    # Tumpang tindih bisa membuat jumlah potongan sedikit melebihi perkiraan,
    # jadi jumlahnya ditegakkan di sini, bukan diandaikan.
    return potongan[: settings.doc_max_chunks]


async def _ringkas_semua(
    job: Job, *, potongan: list[_Potongan], teks: list[str], owner: str
) -> None:
    """Ringkas setiap potongan secara bersamaan, dengan batas jumlah sekaligus.

    Bersamaan, bukan berurutan: tujuh panggilan berurutan akan membuat
    pengguna menunggu berkali-kali lebih lama tanpa manfaat apa pun.
    """
    semaphore = asyncio.Semaphore(settings.doc_chunk_concurrency)
    selesai = 0

    async def satu(index: int) -> None:
        nonlocal selesai

        async with semaphore:
            try:
                ringkasan, _ = await llm.structured(
                    task=Task.DOC_CHUNK_SUMMARY,
                    messages=build_chunk_messages(
                        bagian_ke=index + 1,
                        jumlah_bagian=len(teks),
                        teks=teks[index],
                    ),
                    owner=owner,
                    schema=ChunkSummary,
                )
                potongan[index].ringkasan = ringkasan
            except Exception as exc:  # satu bagian gagal bukan akhir semuanya
                logger.warning(
                    "Bagian dokumen gagal diringkas",
                    bagian=index + 1,
                    error=str(exc)[:160],
                )
                potongan[index].galat = exc

        selesai += 1
        await job_store.update(
            job.id,
            progress=_PROGRESS_AWAL
            + int((_PROGRESS_AKHIR - _PROGRESS_AWAL) * selesai / len(teks)),
            message=f"Meringkas bagian {selesai} dari {len(teks)}.",
        )

    await asyncio.gather(*(satu(index) for index in range(len(teks))))


def _galat_pertama(potongan: list[_Potongan]) -> Exception:
    """Galat pertama yang tercatat, untuk dilaporkan bila semuanya gagal."""
    for item in potongan:
        if item.galat is not None:
            return item.galat
    return InvalidRequest("Dokumen tidak dapat diringkas.")


def _susun_catatan(potongan: list[_Potongan]) -> str:
    """Catatan per bagian, siap diserahkan ke tahap penggabungan.

    Potongan yang gagal tetap disebut. Menyembunyikannya akan membuat telaah
    terlihat membahas seluruh dokumen, padahal ada bagian yang tidak terbaca.
    """
    blok: list[str] = []

    for item in potongan:
        if item.ringkasan is None:
            blok.append(
                f"BAGIAN {item.nomor} ({item.letak}) — TIDAK TERBACA. "
                "Bagian ini gagal diringkas, sehingga isinya tidak diketahui."
            )
            continue

        isi = item.ringkasan
        baris = [
            f"BAGIAN {item.nomor} ({item.letak}) — {isi.judul_bagian}",
            "",
            *isi.ringkasan,
        ]

        if isi.poin:
            baris.append("Poin: " + "; ".join(isi.poin))
        if isi.metode:
            baris.append("Metode, alat, atau dataset: " + "; ".join(isi.metode))
        if isi.istilah:
            baris.append(
                "Istilah: "
                + "; ".join(f"{kata.nama} — {kata.arti}" for kata in isi.istilah)
            )

        blok.append("\n".join(baris))

    return "\n\n".join(blok)


# --------------------------------------------------------------------------- #
# Tahap kedua: menyusun dokumen cetak
# --------------------------------------------------------------------------- #


async def _susun_artefak(
    job: Job,
    *,
    request: DocumentRequest,
    bahan: _Bahan,
    wawasan: DocumentOverview,
    bagian: list[ReviewSection],
) -> tuple[Path, bytes, Path]:
    """Susun PDF dan JSON, lalu kembalikan ketiga berkasnya."""
    dokumen_telaah = _susun_dokumen(
        request,
        wawasan,
        bagian,
        bahan.potongan,
        judul=request.judul or wawasan.judul,
        dokumen=bahan.dokumen,
        halaman_dibaca=bahan.halaman_dibaca,
        terpotong=bahan.terpotong,
    )
    artifact, pdf_bytes = await _tulis_pdf(job, dokumen_telaah)
    json_path = _tulis_json(
        job,
        wawasan,
        bagian,
        bahan.potongan,
        bahan.halaman_dibaca,
        bahan.terpotong,
    )
    return artifact, pdf_bytes, json_path


def _susun_bagian(potongan: list[_Potongan]) -> list[ReviewSection]:
    """Ubah catatan tiap potongan menjadi bagian laporan, tanpa dipadatkan.

    Satu potongan dokumen menjadi satu bagian laporan. Inilah yang membuat
    laporan benar-benar membahas seluruh dokumen: tidak ada tahap yang
    menggabungkan tujuh bagian menjadi lima, sehingga tidak ada isi yang hilang
    di tengah jalan.
    """
    bagian: list[ReviewSection] = []

    for item in potongan:
        isi = item.ringkasan

        if isi is None:
            continue

        paragraf = list(isi.ringkasan)

        if isi.metode:
            paragraf.append(
                "Metode, alat, atau dataset yang disebut bagian ini: "
                + "; ".join(isi.metode)
                + "."
            )

        bagian.append(
            ReviewSection(
                judul=f"{isi.judul_bagian} ({item.letak})",
                paragraf=paragraf,
                poin=isi.poin,
            )
        )

    return bagian


def _susun_dokumen(
    request: DocumentRequest,
    wawasan: DocumentOverview,
    bagian: list[ReviewSection],
    potongan: list[_Potongan],
    *,
    judul: str,
    dokumen: ExtractedDocument,
    halaman_dibaca: int,
    terpotong: bool,
) -> ReviewDocument:
    """Rakit data siap cetak, termasuk catatan cakupan pembacaan."""
    return ReviewDocument(
        judul=judul,
        ringkasan_eksekutif=wawasan.ringkasan_eksekutif,
        bagian=bagian,
        temuan_utama=wawasan.temuan_utama,
        keterbatasan=wawasan.keterbatasan,
        istilah=wawasan.istilah,
        sumber=request.materi_path.name,
        fokus=request.fokus,
        halaman_dokumen=dokumen.pages,
        halaman_dibaca=halaman_dibaca,
        tabel_perbandingan=wawasan.tabel_perbandingan,
        diagram=wawasan.diagram,
        kolom_peta=_KOLOM_PETA,
        peta_bagian=_peta_bagian(potongan),
        catatan=_catatan_cakupan(potongan, dokumen=dokumen, terpotong=terpotong),
    )


def _peta_bagian(potongan: list[_Potongan]) -> list[list[str]]:
    """Tabel kerja: potongan mana diproses dari bagian mana dokumennya."""
    return [
        [
            str(item.nomor),
            item.letak,
            item.ringkasan.judul_bagian if item.ringkasan else "—",
            "Dibaca" if item.berhasil else "Gagal dibaca",
        ]
        for item in potongan
    ]


def _catatan_cakupan(
    potongan: list[_Potongan],
    *,
    dokumen: ExtractedDocument,
    terpotong: bool,
) -> list[str]:
    """Keterangan apa adanya bila ada bagian yang tidak terbaca."""
    catatan: list[str] = []

    gagal = [item.nomor for item in potongan if not item.berhasil]
    if gagal:
        catatan.append(
            "Bagian yang gagal diringkas: "
            + ", ".join(str(nomor) for nomor in gagal)
            + ". Isi bagian itu tidak ikut dalam telaah ini."
        )

    # Berkas hasil pindai atau foto: halaman bergambar dibaca lewat OCR, dan
    # jumlahnya dibatasi. Tanpa keterangan ini, laporan terlihat membahas
    # seluruh dokumen padahal sebagian halamannya tidak pernah dibaca.
    if dokumen.ocr_skipped > 0:
        catatan.append(
            "Dokumen ini sebagian berupa gambar. Halaman yang dibaca lewat "
            f"pengenalan gambar {dokumen.ocr_pages}, dilewati "
            f"{dokumen.ocr_skipped} dari {dokumen.pages} halaman. Naikkan "
            "PATH_AI_MAX_OCR_PAGES bila seluruh halaman perlu dibaca."
        )

    if terpotong:
        catatan.append(
            "Dokumen ini lebih panjang daripada batas pemrosesan, sehingga baru "
            "sebagian yang dibaca. Naikkan PATH_AI_DOC_MAX_CHUNKS bila seluruh "
            "dokumen perlu dibaca sekaligus."
        )

    return catatan


async def _tulis_pdf(job: Job, dokumen: ReviewDocument) -> tuple[Path, bytes]:
    """Susun PDF di luar event loop, lalu simpan di direktori job."""
    pdf_bytes = await asyncio.to_thread(build_review_pdf, dokumen)
    artifact = job.tmp_dir / f"{slugify(dokumen.judul, fallback='telaah-dokumen')}.pdf"
    artifact.write_bytes(pdf_bytes)
    return artifact, pdf_bytes


def _tulis_json(
    job: Job,
    wawasan: DocumentOverview,
    bagian: list[ReviewSection],
    potongan: list[_Potongan],
    halaman_dibaca: int,
    terpotong: bool,
) -> Path:
    """Rincian lengkap, termasuk catatan mentah tiap potongan dokumen.

    Berguna saat guru ingin memeriksa bagian mana yang menjadi dasar sebuah
    butir di laporan.
    """
    payload = {
        "judul": wawasan.judul,
        "ringkasan_eksekutif": wawasan.ringkasan_eksekutif,
        "bagian": [potongan_bagian.model_dump() for potongan_bagian in bagian],
        "tabel_perbandingan": (
            wawasan.tabel_perbandingan.model_dump()
            if wawasan.tabel_perbandingan
            else None
        ),
        "temuan_utama": wawasan.temuan_utama,
        "keterbatasan": wawasan.keterbatasan,
        "istilah": [kata.model_dump() for kata in wawasan.istilah],
        "cakupan": {
            "halaman_dibaca": halaman_dibaca,
            "dokumen_terpotong": terpotong,
            "potongan": [_potongan_ke_dict(item) for item in potongan],
        },
    }

    path = job.tmp_dir / "telaah-dokumen.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def _potongan_ke_dict(item: _Potongan) -> dict[str, object]:
    isi = item.ringkasan

    return {
        "nomor": item.nomor,
        "letak": item.letak,
        "berhasil": item.berhasil,
        "judul_bagian": isi.judul_bagian if isi else None,
        "ringkasan": isi.ringkasan if isi else [],
        "poin": isi.poin if isi else [],
        "metode": isi.metode if isi else [],
        "istilah": [kata.model_dump() for kata in isi.istilah] if isi else [],
    }


def _hasil(
    wawasan: DocumentOverview,
    bagian: list[ReviewSection],
    potongan: list[_Potongan],
    dokumen: ExtractedDocument,
    pdf_bytes: bytes,
    completion: Completion,
    gagal: int,
) -> dict[str, object]:
    """Ringkasan hasil untuk berkas status job."""
    return {
        "judul": wawasan.judul,
        "jumlah_bagian": len(bagian),
        "jumlah_paragraf": sum(len(item.paragraf) for item in bagian),
        "potongan_total": len(potongan),
        "potongan_dibaca": len(potongan) - gagal,
        "potongan_gagal": gagal,
        "halaman_dokumen": dokumen.pages,
        "halaman_pdf": pdf_page_count(pdf_bytes),
        "elemen": {
            "tabel_perbandingan": 1 if wawasan.tabel_perbandingan else 0,
            "diagram": 1 if wawasan.diagram else 0,
            "istilah": len(wawasan.istilah),
            "temuan_utama": len(wawasan.temuan_utama),
        },
        "model": completion.response.model,
        "degraded": completion.degraded or gagal > 0,
    }


# --------------------------------------------------------------------------- #
# Hitungan kecil
# --------------------------------------------------------------------------- #


def _halaman_dibaca(
    halaman: int, teks: str, teks_penuh: str, *, dilewati: int = 0
) -> int:
    """Perkiraan halaman yang benar-benar dibaca.

    Dua hal bisa mengurangi cakupan, dan keduanya dihitung di sini: dokumen
    yang dipotong karena terlalu panjang, dan halaman bergambar yang dilewati
    karena melewati batas pengenalan gambar.
    """
    terbaca = halaman - max(0, dilewati)

    if terbaca <= 0:
        return 0

    if len(teks_penuh) <= 0:
        return terbaca

    proporsi = min(1.0, len(teks) / len(teks_penuh))
    return max(1, round(terbaca * proporsi))


def _penanda_bagian(index: int, jumlah: int, halaman: int) -> str:
    """Penanda letak bagian ini di dokumen aslinya.

    Untuk dokumen berhalaman, penandanya adalah rentang halaman, dan disebut
    "perkiraan" karena potongan dibagi menurut jumlah karakter sedangkan
    kepadatan teks tiap halaman tidak sama.

    Untuk TXT dan DOCX, yang dibaca sebagai satu blok tanpa halaman, menyebut
    "(halaman 1)" untuk semua bagian justru menyesatkan. Karena itu penandanya
    memakai nomor bagian.
    """
    if jumlah <= 0:
        return "—"

    if halaman <= 1:
        return f"bagian {index + 1} dari {jumlah}"

    mulai = math.floor(index / jumlah * halaman) + 1
    akhir = max(mulai, math.ceil((index + 1) / jumlah * halaman))
    return str(mulai) if akhir == mulai else f"halaman {mulai}–{akhir}"


def _judul_dari(path: Path) -> str:
    """Judul seadanya dari nama berkas, bila pengguna tidak mengisinya."""
    nama = path.stem.replace("-", " ").replace("_", " ").strip()
    return nama or "Dokumen"
