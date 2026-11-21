"""Mengisi template rekap asesmen menjadi berkas Excel.

Templatenya disimpan sebagai berkas bawaan di `app/assets`, lalu diisi apa
adanya memakai openpyxl. Cara ini menjaga lebar kolom, gaya tulisan, dan kalimat
header yang persis sama dengan yang dipakai sekolah, sehingga berkas hasilnya
bisa langsung dimasukkan ke sistem nilai mereka tanpa disunting.

Ada satu keputusan yang membentuk seluruh modul ini: **posisi sel dicari lewat
label, bukan lewat koordinat tetap**. Template ini sudah pernah diubah sekali,
dan kemungkinan akan diubah lagi. Dengan pencarian label, perubahan itu tidak
menuntut perubahan kode selama kalimat labelnya tetap. Koordinat tetap akan
diam-diam menaruh nilai di baris yang salah begitu ada baris disisipkan.
"""

from __future__ import annotations

import io
from copy import copy
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from app.domain.documents import RekapKoreksi, RekapPlagiasi

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "template-rekap-koreksi.xlsx"
)

# Jenis berkas Excel. Dipakai juga oleh endpoint unduhan untuk menentukan apakah
# permintaan format cocok dengan berkas yang benar-benar ada.
EXCEL_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

# Label tetap di dalam template. Nilainya diambil dari template, bukan ditulis
# ulang di sini, karena kalimatnya milik sekolah.
_LABEL_HEADER = (
    ("Pengajar", "pengajar"),
    ("Mapel", "mapel"),
    ("Kelas", "kelas"),
    ("Tugas", "tugas"),
)

_LABEL_DESKRIPSI = "Deskripsi hasil analisa"

# Kata kunci untuk menemukan baris judul tabel, per jenis rekap.
_KATA_TABEL = {
    "koreksi": ("Nama Siswa", "Nilai"),
    "plagiasi": ("Nama Siswa", "Presentase"),
}

# Kata kunci untuk menemukan lembarnya, supaya urutan lembar di template tidak
# menentukan: kalau nanti lembarnya dibalik, isiannya tetap masuk ke tempat yang
# benar.
_KATA_LEMBAR = {"koreksi": "Capaian", "plagiasi": "Plagiasi"}

# Perkiraan tinggi baris deskripsi, dalam satuan poin per baris teks.
_TINGGI_PER_BARIS = 15.0
# Lebar total kolom A sampai F pada template, dipakai untuk menaksir jumlah
# baris yang dibutuhkan deskripsi.
_LEBAR_TOTAL = 150.0


def build_rekap_koreksi(rekap: RekapKoreksi) -> bytes:
    """Isi lembar koreksi: capaian hasil belajar per siswa (1 lembar saja)."""
    buku = _buka_template()
    lembar = _cari_lembar(buku, "koreksi")
    _sisakan_lembar(buku, lembar)

    _isi_header(
        lembar,
        pengajar=rekap.pengajar,
        mapel=rekap.mapel,
        kelas=rekap.kelas,
        tugas=rekap.tugas,
        deskripsi=rekap.deskripsi,
    )

    baris = [
        [nomor, item.nama, item.email, item.nilai, item.keterangan, item.saran]
        for nomor, item in enumerate(rekap.baris, start=1)
    ]
    _tulis_baris(lembar, "koreksi", baris, kolom_terakhir=6)

    return _simpan(buku)


def build_rekap_plagiasi(rekap: RekapPlagiasi) -> bytes:
    """Isi lembar plagiasi: persentase kemiripan tertinggi tiap siswa (1 lembar saja)."""
    buku = _buka_template()
    lembar = _cari_lembar(buku, "plagiasi")
    _sisakan_lembar(buku, lembar)

    _isi_header(
        lembar,
        pengajar=rekap.pengajar,
        mapel=rekap.mapel,
        kelas=rekap.kelas,
        tugas=rekap.tugas,
        deskripsi=rekap.deskripsi,
    )

    baris = [
        [nomor, item.nama, item.email, item.persentase]
        for nomor, item in enumerate(rekap.baris, start=1)
    ]
    _tulis_baris(lembar, "plagiasi", baris, kolom_terakhir=4)

    return _simpan(buku)


# --------------------------------------------------------------------------- #
# Bagian: membuka, mencari, mengisi
# --------------------------------------------------------------------------- #


def _buka_template() -> Workbook:
    """Buka template sebagai buku kerja."""
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(
            f"Template rekap tidak ditemukan di {TEMPLATE_PATH}. "
            "Berkas itu dibutuhkan untuk menyusun keluaran Excel."
        )

    return openpyxl.load_workbook(TEMPLATE_PATH)


def _cari_lembar(buku: Workbook, jenis: str) -> Worksheet:
    """Lembar yang dipakai untuk satu jenis rekap."""
    for lembar in buku.worksheets:
        if _cari_sel(lembar, _KATA_LEMBAR[jenis]) is not None:
            return lembar

    raise ValueError(
        "Lembar untuk rekap tidak ditemukan di template. "
        f"Kata yang dicari: {_KATA_LEMBAR[jenis]}."
    )


def _sisakan_lembar(buku: Workbook, dipilih: Worksheet) -> None:
    """Hapus semua lembar lain agar berkas unduhan hanya memuat lembar fitur ini."""
    for lembar in list(buku.worksheets):
        if lembar is not dipilih:
            buku.remove(lembar)


def _simpan(buku: Workbook) -> bytes:
    buffer = io.BytesIO()
    buku.save(buffer)
    return buffer.getvalue()


def _cari_sel(lembar: Worksheet, kata: str):
    """Sel pertama yang memuat kata tertentu, tanpa membedakan huruf besar kecil."""
    dicari = kata.lower()

    for baris in lembar.iter_rows():
        for sel in baris:
            if isinstance(sel.value, str) and dicari in sel.value.lower():
                return sel

    return None


def _cari_baris_tabel(lembar: Worksheet, jenis: str) -> int:
    """Baris judul tabel, dicari dari kata kuncinya."""
    for kata in _KATA_TABEL[jenis]:
        sel = _cari_sel(lembar, kata)
        if sel is not None:
            return int(sel.row)

    raise ValueError(
        "Baris judul tabel tidak ditemukan di template. "
        f"Kata yang dicari: {', '.join(_KATA_TABEL[jenis])}."
    )


def _isi_header(
    lembar: Worksheet,
    *,
    pengajar: str,
    mapel: str,
    kelas: str,
    tugas: str,
    deskripsi: list[str],
) -> None:
    """Isi blok identitas di atas tabel, lalu paragraf deskripsinya."""
    nilai = {
        "pengajar": pengajar,
        "mapel": mapel,
        "kelas": kelas,
        "tugas": tugas,
    }

    for label, kunci in _LABEL_HEADER:
        sel = _cari_sel(lembar, label)
        if sel is None:
            continue
        sel.value = _ganti_contoh(str(sel.value), nilai[kunci])

    _isi_deskripsi(lembar, deskripsi)


def _ganti_contoh(teks: str, nilai: str) -> str:
    """Ganti bagian contoh dalam kurung siku dengan nilai sebenarnya.

    Template menulis "Mapel [nama mapel]". Yang diganti hanya bagian dalam
    kurung siku, sehingga gaya penulisan template dipertahankan apa adanya.
    """
    awalan = teks.split("[")[0].rstrip()
    if not nilai.strip():
        return awalan
    return f"{awalan} {nilai.strip()}"


def _isi_deskripsi(lembar: Worksheet, deskripsi: list[str]) -> None:
    """Tulis paragraf deskripsi di sel labelnya, tanpa menambah baris tabel.

    Paragrafnya diletakkan di sel yang sama dengan labelnya, sehingga tabel
    tetap mulai di baris yang sama seperti template. Tinggi barisnya disesuaikan
    supaya paragrafnya ikut terlihat, karena sel gabungan tidak menambah tinggi
    sendiri di Excel.
    """
    paragraf = [teks.strip() for teks in deskripsi if teks and teks.strip()]
    if not paragraf:
        return

    sel = _cari_sel(lembar, _LABEL_DESKRIPSI)
    if sel is None:
        return

    isi = "\n\n".join(paragraf)
    sel.value = f"{sel.value}\n\n{isi}"

    rata = copy(sel.alignment)
    rata.wrap_text = True
    rata.vertical = "top"
    sel.alignment = rata

    lembar.row_dimensions[sel.row].height = _tinggi_baris(isi)


def _tinggi_baris(teks: str) -> float:
    """Perkiraan tinggi baris yang cukup untuk menampilkan paragrafnya."""
    batas = max(1, int(_LEBAR_TOTAL))
    baris = 0

    for paragraf in teks.split("\n"):
        baris += max(1, -(-len(paragraf) // batas))

    return max(_TINGGI_PER_BARIS * 2, baris * _TINGGI_PER_BARIS)


def _tulis_baris(
    lembar: Worksheet, jenis: str, baris: list[list[object]], *, kolom_terakhir: int
) -> None:
    """Tulis baris data mulai satu baris di bawah judul tabel."""
    if not baris:
        return

    awal = _cari_baris_tabel(lembar, jenis) + 1
    contoh = awal
    terakhir_template = lembar.max_row

    for nomor_baris, isi in enumerate(baris):
        sasaran = awal + nomor_baris

        # Di luar baris yang sudah ada di template, gayanya disalin dari baris
        # data pertama supaya garis dan hurufnya tetap seragam.
        if sasaran > terakhir_template:
            _salin_gaya(lembar, sumber=contoh, sasaran=sasaran, sampai=kolom_terakhir)

        for kolom, nilai in enumerate(isi[:kolom_terakhir], start=1):
            lembar.cell(row=sasaran, column=kolom).value = nilai


def _salin_gaya(
    lembar: Worksheet, *, sumber: int, sasaran: int, sampai: int
) -> None:
    """Salin gaya satu baris template ke baris baru di bawahnya."""
    for kolom in range(1, sampai + 1):
        asal = lembar.cell(row=sumber, column=kolom)
        tujuan = lembar.cell(row=sasaran, column=kolom)

        tujuan.font = copy(asal.font)
        tujuan.border = copy(asal.border)
        tujuan.fill = copy(asal.fill)
        tujuan.number_format = asal.number_format
        tujuan.alignment = Alignment(
            horizontal=asal.alignment.horizontal,
            vertical=asal.alignment.vertical,
            wrap_text=asal.alignment.wrap_text,
        )
