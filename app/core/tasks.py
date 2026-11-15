"""Katalog task LLM.

Setiap panggilan ke model harus menyebut task-nya. Tabel di bawah adalah
satu-satunya tempat yang menentukan:

- apakah task boleh turun ke model gratis saat anggaran berbayar habis,
- berapa `max_tokens` yang dipasang (mencegah jawaban tak terkendali),
- berapa estimasi token untuk keputusan pra-panggilan.

Task yang menyentuh nilai siswa (`KEY_EXTRACT`, `ITEM_GRADE`) sengaja TIDAK
pernah memakai model gratis. Satu batch yang sebagian dinilai model berbeda
menghasilkan laporan dengan dua standar penilaian, dan itu masalah akademik,
bukan masalah teknis.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Task(StrEnum):
    # Fitur 1 & 2
    MATERIAL_CONTENT = "material_content"

    # Fitur 6 — ringkasan dokumen panjang, dua tahap
    DOC_CHUNK_SUMMARY = "doc_chunk_summary"
    DOC_SYNTHESIS = "doc_synthesis"

    # Fitur 3
    KEY_EXTRACT = "key_extract"
    ITEM_GRADE = "item_grade"
    CLASS_SUMMARY = "class_summary"

    # Fitur 4
    PAIR_JUDGE = "pair_judge"

    # Fitur 5
    AI_DETECT = "ai_detect"

    # Pembacaan halaman bergambar, dipakai seluruh fitur
    PAGE_OCR = "page_ocr"


@dataclass(frozen=True)
class TaskSpec:
    task: Task
    may_use_free: bool
    est_in_tokens: int
    est_out_tokens: int
    max_out_tokens: int
    temperature: float


TASK_SPECS: dict[Task, TaskSpec] = {
    # Materi yang dibagikan ke siswa. Boleh turun hanya bila pengajar bersedia
    # menerima kualitas lebih rendah demi menjaga layanan tetap hidup.
    Task.MATERIAL_CONTENT: TaskSpec(
        task=Task.MATERIAL_CONTENT,
        may_use_free=False,
        est_in_tokens=6_000,
        est_out_tokens=3_500,
        max_out_tokens=4_000,
        temperature=0.4,
    ),
    # Ringkasan satu potongan dokumen. Tidak boleh turun walau isinya hanya
    # narasi: dokumen yang sebagian potongannya diringkas oleh model berbeda
    # akan punya dua standar mutu di dalam satu laporan.
    #
    # Angka perkiraannya diukur, bukan ditebak. Dokumen tinjauan 39 halaman
    # (140.477 karakter, 7 potongan) memakai 43.860 token masuk dan 8.117 token
    # keluar untuk delapan panggilan. Angka di sini dibuat sedikit lebih besar
    # supaya pemeriksaan anggaran tidak pernah lolos padahal tidak cukup.
    Task.DOC_CHUNK_SUMMARY: TaskSpec(
        task=Task.DOC_CHUNK_SUMMARY,
        may_use_free=False,
        est_in_tokens=6_000,
        est_out_tokens=1_800,
        max_out_tokens=2_400,
        temperature=0.2,
    ),
    # Menyusun ikhtisar, perbandingan, temuan, keterbatasan, dan daftar
    # istilah. Badan laporan tidak dibuat di sini, sehingga keluarannya jauh
    # lebih kecil daripada satu bagian catatan.
    Task.DOC_SYNTHESIS: TaskSpec(
        task=Task.DOC_SYNTHESIS,
        may_use_free=False,
        est_in_tokens=7_000,
        est_out_tokens=4_000,
        max_out_tokens=6_000,
        temperature=0.3,
    ),
    # Ekstraksi kunci jawaban: tahap pertama fitur 3, menentukan seluruh
    # penilaian di bawahnya. Tidak boleh turun.
    Task.KEY_EXTRACT: TaskSpec(
        task=Task.KEY_EXTRACT,
        may_use_free=False,
        est_in_tokens=8_000,
        est_out_tokens=2_500,
        max_out_tokens=3_000,
        temperature=0.0,
    ),
    # Penilaian per siswa. Tidak boleh turun.
    Task.ITEM_GRADE: TaskSpec(
        task=Task.ITEM_GRADE,
        may_use_free=False,
        est_in_tokens=4_000,
        est_out_tokens=1_200,
        max_out_tokens=1_500,
        temperature=0.0,
    ),
    # Satu panggilan per batch untuk deskripsi analisa kelas.
    # Aman turun: tidak menghasilkan angka, hanya narasi.
    Task.CLASS_SUMMARY: TaskSpec(
        task=Task.CLASS_SUMMARY,
        may_use_free=True,
        est_in_tokens=5_000,
        est_out_tokens=1_200,
        max_out_tokens=1_500,
        temperature=0.3,
    ),
    # Wasit pita abu-abu fitur 4. Persentase tetap dihitung server, jadi
    # kesalahan model di sini tidak mengubah angka apa pun.
    Task.PAIR_JUDGE: TaskSpec(
        task=Task.PAIR_JUDGE,
        may_use_free=True,
        est_in_tokens=2_500,
        est_out_tokens=400,
        max_out_tokens=600,
        temperature=0.1,
    ),
    # Membaca satu halaman bergambar menjadi teks, memakai model penglihatan.
    # Dipakai saat halaman berupa hasil pindai atau foto.
    #
    # Tidak boleh turun walau isinya hanya teks mentah: hasil pembacaan ini
    # menjadi bahan penilaian, dan satu halaman yang dibaca model berbeda akan
    # punya standar berbeda pula. Angka perkiraannya mengikuti pengukuran nyata
    # satu halaman: sekitar 1.100 sampai 2.200 token gambar masuk.
    Task.PAGE_OCR: TaskSpec(
        task=Task.PAGE_OCR,
        may_use_free=False,
        est_in_tokens=2_500,
        est_out_tokens=1_200,
        max_out_tokens=2_000,
        temperature=0.0,
    ),
    # Penilai pita abu-abu fitur 5. Sama seperti PAIR_JUDGE.
    Task.AI_DETECT: TaskSpec(
        task=Task.AI_DETECT,
        may_use_free=True,
        est_in_tokens=2_500,
        est_out_tokens=600,
        max_out_tokens=800,
        temperature=0.1,
    ),
}


def spec_for(task: Task) -> TaskSpec:
    return TASK_SPECS[task]
