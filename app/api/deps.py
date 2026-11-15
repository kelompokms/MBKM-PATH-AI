"""Dependensi bersama untuk endpoint.

Identitas datang dari backend aplikasi, bukan dari browser:

    X-API-Key: <kunci bersama>              wajib, membuktikan pemanggilnya backend
    X-Actor-Id: guru:ani@sekolah.id         opsional, menentukan siapa yang bertindak
    X-Actor-Id: siswa:budi@sekolah.id

`X-API-Key` wajib ada pada setiap permintaan. Tanpa itu, siapa pun yang bisa
menjangkau layanan ini dapat memakai anggaran model dan membaca laporan
pengajar lain.

Peran dibaca dari awalan `X-Actor-Id` dan menentukan dua hal: batas anggaran,
dan endpoint mana yang boleh dipakai. Pelajar tidak boleh menyentuh asesmen,
karena laporan di sana memuat nama, email, dan nilai seluruh kelas.

Email sengaja dinormalkan (huruf kecil, dipangkas). Kalau tidak, `Ani@Sekolah.id`
dan `ani@sekolah.id` menjadi dua pemilik berbeda: anggaran terbelah dan job
yang dibuat lewat satu ejaan tidak terlihat lewat ejaan lain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Header

from app.core.errors import ForbiddenForStudent, Unauthorized
from app.core.security import is_valid_key, owner_for, owner_for_key

Role = Literal["admin", "teacher", "student"]

_STUDENT_PREFIX = "siswa"
_TEACHER_PREFIX = "guru"

# Cukup untuk email panjang, tidak cukup untuk menyalahgunakan header sebagai
# tempat menyimpan data lain.
_MAX_ACTOR_CHARS = 120


@dataclass(frozen=True)
class Caller:
    """Pemanggil yang sudah dikenali.

    `fingerprint` adalah kunci anggaran dan kepemilikan job. `actor` sudah
    **disamarkan**; email atau id mentahnya tidak pernah keluar dari modul ini.
    """

    fingerprint: str
    role: Role
    actor: str | None = None


def mask_identity(identity: str) -> str:
    """Samarkan identitas untuk ditampilkan: `b***@sekolah.id`.

    Dipakai di `/v1/ops/usage`, yang badan responsnya bisa tersimpan di log
    reverse proxy atau tunnel. Cukup untuk menjawab "ini akun yang mana",
    tidak cukup untuk mengumpulkan alamat email.
    """
    nama, _, domain = identity.partition("@")
    if not domain:
        return identity if len(identity) <= 8 else f"{identity[:4]}…"
    return f"{nama[:1]}***@{domain}"


def parse_actor(value: str | None) -> tuple[Role, str] | None:
    """Baca `guru:<id>` atau `siswa:<id>`; `None` bila bentuknya tidak sah.

    Awalan wajib. Tanpa awalan yang dikenal, permintaan tidak ditebak
    perannya — menebak berarti bisa memberi hak pengajar kepada pelajar
    hanya karena satu header ditulis salah.
    """
    if value is None:
        return None

    bersih = value.strip()
    if not bersih or len(bersih) > _MAX_ACTOR_CHARS:
        return None

    awalan, _, identitas = bersih.partition(":")
    identitas = identitas.strip().lower()
    if not identitas:
        return None

    awalan = awalan.strip().lower()
    if awalan == _STUDENT_PREFIX:
        return "student", identitas
    if awalan == _TEACHER_PREFIX:
        return "teacher", identitas
    return None


async def current_caller(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
) -> Caller:
    """Kenali pemanggil dari kunci bersama, lalu tentukan perannya.

    Kunci diperiksa lebih dulu dan menjadi syarat mutlak. `X-Actor-Id` hanya
    bermakna setelah kunci itu sah: tanpa aturan ini, satu header buatan sudah
    cukup untuk memakai anggaran dan membaca laporan siapa pun.
    """
    if not x_api_key or not is_valid_key(x_api_key):
        raise Unauthorized(
            "Kunci API tidak valid atau tidak dikirim pada header X-API-Key."
        )

    # Header tidak dikirim sama sekali: jalur curl dan panel admin, anggarannya
    # menempel pada kunci. Bedanya dengan header kosong penting: header yang
    # ada tetapi tidak sah DITOLAK, karena mundur ke jalur admin berarti
    # memberi hak penuh kepada pengirim yang headernya salah tulis.
    if x_actor_id is None:
        return Caller(fingerprint=owner_for_key(x_api_key), role="admin")

    dibaca = parse_actor(x_actor_id)
    if dibaca is None:
        raise Unauthorized(
            "Header X-Actor-Id harus berbentuk 'guru:<id>' atau 'siswa:<id>'."
        )

    role, identitas = dibaca
    return Caller(
        fingerprint=owner_for(role, identitas),
        role=role,
        actor=mask_identity(identitas),
    )


CallerDep = Annotated[Caller, Depends(current_caller)]


async def require_teacher(caller: CallerDep) -> Caller:
    """Tolak pelajar dari endpoint yang memuat data siswa lain.

    Dipasang pada seluruh router asesmen dan kunci jawaban, bukan pada
    masing-masing fungsi, supaya endpoint baru di router itu ikut terlindungi
    secara otomatis.
    """
    if caller.role == "student":
        raise ForbiddenForStudent()
    return caller


TeacherDep = Annotated[Caller, Depends(require_teacher)]