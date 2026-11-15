"""Identitas pemanggil.

Layanan ini tidak mengurus akun. Login, password, dan sesi sepenuhnya milik
backend aplikasi. Tugas PATH-AI hanya satu: menerima pernyataan identitas
dari backend lalu memakainya sebagai pemilik anggaran dan pemilik job.

Dua masukan yang diterima:

1. `X-API-Key` — kunci bersama server-ke-server. Membuktikan bahwa pemanggil
   adalah backend aplikasi, bukan proses lain di jaringan yang sama.
2. `X-Teacher-Id` — id pengajar yang ditulis backend dari sesinya sendiri.
   Tidak ditandatangani, dan memang tidak perlu: yang menjaganya adalah
   aturan bahwa header ini hanya boleh datang dari backend, bukan dari
   browser. Itu sebabnya `X-Teacher-Id` sengaja TIDAK ada di daftar header
   yang diizinkan CORS.

Isi `X-Teacher-Id` harus berupa id yang stabil dan opaque (mis. `guru:1024`),
bukan nama atau email. Anggaran dan daftar job dikunci pada id itu, jadi nama
yang berubah akan mengosongkan anggaran dan menyembunyikan job lama pemiliknya.
"""

from __future__ import annotations

import hashlib
import secrets

from app.core.config import settings

_FINGERPRINT_CHARS = 16

# Pemilik anggaran selalu ditulis `<peran>:<sidik jari>`. Peran ada di luar
# sidik jari supaya buku besar bisa menentukan batas mana yang berlaku tanpa
# perlu menyimpan daftar pengguna — dan tanpa email ikut tersimpan.
OWNER_ADMIN = "admin"
OWNER_TEACHER = "guru"
OWNER_STUDENT = "siswa"

_OWNER_PREFIX = {
    "admin": OWNER_ADMIN,
    "teacher": OWNER_TEACHER,
    "student": OWNER_STUDENT,
}


def fingerprint(value: str) -> str:
    """Sidik jari pendek dan stabil untuk sebuah kunci atau identitas."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:_FINGERPRINT_CHARS]


def owner_for(role: str, identity: str) -> str:
    """Pemilik anggaran untuk sebuah identitas, mis. `siswa:1a2b3c4d5e6f7a8b`.

    Email atau id apa pun yang menjadi `identity` tidak ikut tersimpan;
    yang tersisa hanya 16 karakter sidik jarinya.
    """
    return f"{_OWNER_PREFIX[role]}:{fingerprint(identity)}"


def owner_for_key(api_key: str) -> str:
    """Pemilik anggaran untuk sebuah kunci server, mis. `admin:...`."""
    return f"{OWNER_ADMIN}:{fingerprint(api_key)}"


def is_student_owner(owner: str) -> bool:
    return owner.startswith(f"{OWNER_STUDENT}:")


def is_valid_key(candidate: str) -> bool:
    """Perbandingan waktu-konstan terhadap seluruh kunci yang terdaftar."""
    if not candidate:
        return False

    matched = False
    for key in settings.api_key_list:
        # Tetap evaluasi semua kunci supaya waktu eksekusi tidak bergantung
        # pada posisi kunci yang cocok.
        if secrets.compare_digest(candidate, key):
            matched = True
    return matched
