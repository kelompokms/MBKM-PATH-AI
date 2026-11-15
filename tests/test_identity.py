"""Test identitas, peran, dan pagar antar-peran.

Layanan ini tidak mengurus akun. Login dan sesi ditangani backend aplikasi,
yang lalu mengirim dua header:

    X-API-Key: <kunci bersama>                  wajib
    X-Actor-Id: guru:ani@sekolah.id             opsional
    X-Actor-Id: siswa:budi@sekolah.id

Yang dijaga ketat:

1. **Kunci bersama wajib.** Tanpa itu, siapa pun yang menjangkau layanan ini
   memakai anggaran model dan membaca laporan pengajar lain.
2. **Peran tidak boleh ditebak.** Awalan yang tidak dikenal ditolak, bukan
   dianggap pengajar — menebak berarti memberi hak penuh kepada pelajar
   hanya karena satu header ditulis salah.
3. **Pelajar tidak menyentuh asesmen.** Laporan di sana memuat nama, email,
   dan nilai seluruh kelas.
4. **Email dinormalkan.** Kalau tidak, satu orang bisa punya dua anggaran.
"""

from __future__ import annotations

import httpx
import pytest
from app.core.llm import llm
from app.core.security import is_student_owner, owner_for, owner_for_key
from app.observability.ledger import ledger

from tests.conftest import API_KEY, HEADERS

GURU = "guru:ani@sekolah.id"
SISWA = "siswa:budi@sekolah.id"


def sebagai(actor: str | None = None, *, kunci: str = API_KEY) -> dict[str, str]:
    headers = {"X-API-Key": kunci}
    if actor is not None:
        headers["X-Actor-Id"] = actor
    return headers


async def identitas(client: httpx.AsyncClient, headers: dict[str, str]) -> dict:
    """Bagian `auth` dari /v1/ops/usage: siapa yang dikenali sistem."""
    response = await client.get("/v1/ops/usage", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["auth"]


# --------------------------------------------------------------------------- #
# Kunci bersama
# --------------------------------------------------------------------------- #


async def test_tanpa_kredensial_ditolak(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/ops/usage")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_kunci_salah_ditolak(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/ops/usage", headers={"X-API-Key": "bukan-kunci"})

    assert response.status_code == 401


async def test_kunci_saja_dianggap_admin(client: httpx.AsyncClient) -> None:
    """Jalur curl dan panel admin: anggaran menempel pada kunci."""
    auth = await identitas(client, HEADERS)

    assert auth["role"] == "admin"
    assert auth["actor"] is None
    assert auth["owner"] == owner_for_key(API_KEY)


async def test_actor_tanpa_kunci_sah_ditolak(client: httpx.AsyncClient) -> None:
    """Header identitas saja tidak boleh cukup. Ini pertahanan utamanya."""
    response = await client.get("/v1/ops/usage", headers={"X-Actor-Id": GURU})

    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Peran dan penyamaran
# --------------------------------------------------------------------------- #


async def test_pengajar_dikenali(client: httpx.AsyncClient) -> None:
    auth = await identitas(client, sebagai(GURU))

    assert auth["role"] == "teacher"
    assert auth["owner"] == owner_for("teacher", "ani@sekolah.id")
    # Email tidak pernah dikembalikan mentah.
    assert auth["actor"] == "a***@sekolah.id"


async def test_pelajar_dikenali(client: httpx.AsyncClient) -> None:
    auth = await identitas(client, sebagai(SISWA))

    assert auth["role"] == "student"
    assert auth["owner"] == owner_for("student", "budi@sekolah.id")
    assert is_student_owner(auth["owner"])
    assert auth["actor"] == "b***@sekolah.id"


async def test_email_dinormalkan(client: httpx.AsyncClient) -> None:
    """`Ani@Sekolah.ID` dan `ani@sekolah.id` harus satu pemilik, bukan dua."""
    a = await identitas(client, sebagai("guru:Ani@Sekolah.ID"))
    b = await identitas(client, sebagai("guru:ani@sekolah.id"))

    assert a["owner"] == b["owner"]


@pytest.mark.parametrize(
    "actor",
    [
        pytest.param("ani@sekolah.id", id="tanpa-awalan"),
        pytest.param("kepsek:ani@sekolah.id", id="awalan-tidak-dikenal"),
        pytest.param("guru:", id="tanpa-identitas"),
        pytest.param("", id="nilai-kosong"),
        pytest.param("   ", id="hanya-spasi"),
        pytest.param(f"guru:{'x' * 200}", id="terlalu-panjang"),
    ],
)
async def test_actor_tidak_sah_ditolak(
    client: httpx.AsyncClient, actor: str
) -> None:
    """Ditolak, bukan ditebak. Menebak bisa memberi hak pengajar ke pelajar."""
    response = await client.get("/v1/ops/usage", headers=sebagai(actor))

    assert response.status_code == 401
    assert "X-Actor-Id" in response.json()["error"]["message"]


# --------------------------------------------------------------------------- #
# Pagar peran
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "jalur",
    ["/v1/assessments/similarity", "/v1/assessments/ai-usage", "/v1/assessments/grade"],
)
async def test_pelajar_ditolak_di_asesmen(
    client: httpx.AsyncClient, jalur: str
) -> None:
    response = await client.post(jalur, headers=sebagai(SISWA))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_for_student"


async def test_pelajar_ditolak_di_kunci_jawaban(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/answer-keys/extract", headers=sebagai(SISWA)
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_for_student"


async def test_pelajar_ditolak_membuat_materi(client: httpx.AsyncClient) -> None:
    """Membuat materi memerlukan judul — itu pekerjaan pengajar."""
    response = await client.post(
        "/v1/materials/generate",
        headers=sebagai(SISWA),
        data={"judul": "Siklus Air", "deskripsi_materi": "Materi siklus air"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_for_student"


async def test_pengajar_tidak_diblokir_di_asesmen(
    client: httpx.AsyncClient,
) -> None:
    """Pagar tidak boleh terlalu ketat: pengajar harus tetap bisa masuk."""
    response = await client.post("/v1/assessments/similarity", headers=sebagai(GURU))

    # Ditolak karena badan permintaan kosong, bukan karena peran.
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


async def test_pelajar_boleh_bertanya_materi(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Satu-satunya pintu untuk pelajar."""

    async def model_gagal(**_kwargs: object) -> None:
        raise RuntimeError("model tidak dipanggil pada test ini")

    monkeypatch.setattr(llm, "structured", model_gagal)

    response = await client.post(
        "/v1/materials/explain",
        headers=sebagai(SISWA),
        data={
            "pertanyaan": "Mengapa tumbuhan butuh cahaya matahari?",
            "deskripsi_materi": "Materi fotosintesis kelas VII",
            "isi_materi": "Fotosintesis membutuhkan cahaya matahari.",
        },
    )

    assert response.status_code == 202, response.text


# --------------------------------------------------------------------------- #
# Anggaran per peran
# --------------------------------------------------------------------------- #


async def test_batas_anggaran_berbeda_per_peran(
    client: httpx.AsyncClient,
) -> None:
    from app.core.config import settings

    guru = (await client.get("/v1/ops/usage", headers=sebagai(GURU))).json()
    siswa = (await client.get("/v1/ops/usage", headers=sebagai(SISWA))).json()

    assert guru["budget"]["limit"] == settings.budget_tokens_per_day
    assert siswa["budget"]["limit"] == settings.student_budget_tokens_per_day
    assert siswa["budget"]["limit"] < guru["budget"]["limit"]


async def test_anggaran_terpisah_per_pemilik(client: httpx.AsyncClient) -> None:
    await ledger.reserve(owner_for("student", "budi@sekolah.id"), 5_000)

    siswa = (await client.get("/v1/ops/usage", headers=sebagai(SISWA))).json()
    guru = (await client.get("/v1/ops/usage", headers=sebagai(GURU))).json()

    assert siswa["budget"]["reserved"] == 5_000
    assert guru["budget"]["reserved"] == 0
    assert guru["budget"]["remaining"] == guru["budget"]["limit"]


async def test_biaya_tercatat_per_pemilik(client: httpx.AsyncClient) -> None:
    """Biaya dicatat terpisah dari token, karena keduanya tidak sebanding."""
    await ledger.settle(
        owner_for("student", "budi@sekolah.id"),
        reserved=0,
        actual=4_175,
        cost_usd=0.007,
    )

    siswa = (await client.get("/v1/ops/usage", headers=sebagai(SISWA))).json()
    guru = (await client.get("/v1/ops/usage", headers=sebagai(GURU))).json()

    assert siswa["budget"]["spent_usd"] == pytest.approx(0.007)
    assert siswa["budget"]["used"] == 4_175
    assert guru["budget"]["spent_usd"] == 0


async def test_batas_ilustrasi_pelajar_dihormati() -> None:
    """Token bukan pengendali biaya gambar, jadi batasnya dihitung terpisah."""
    pemilik = owner_for("student", "citra@sekolah.id")
    await ledger.reset(pemilik)

    assert await ledger.allow_image(pemilik, 2) is True
    assert await ledger.allow_image(pemilik, 2) is True
    # Percobaan ketiga ditolak.
    assert await ledger.allow_image(pemilik, 2) is False


async def test_batas_ilustrasi_tidak_berlaku_untuk_pengajar() -> None:
    pemilik = owner_for("teacher", "ani@sekolah.id")
    await ledger.reset(pemilik)

    for _ in range(5):
        assert await ledger.allow_image(pemilik, None) is True


# --------------------------------------------------------------------------- #
# Job terpisah
# --------------------------------------------------------------------------- #


async def test_job_pelajar_lain_tidak_terlihat(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def model_gagal(**_kwargs: object) -> None:
        raise RuntimeError("model tidak dipanggil pada test ini")

    monkeypatch.setattr(llm, "structured", model_gagal)

    budi = sebagai(SISWA)
    citra = sebagai("siswa:citra@sekolah.id")

    response = await client.post(
        "/v1/materials/explain",
        headers=budi,
        data={
            "pertanyaan": "Apa itu fotosintesis?",
            "deskripsi_materi": "Materi fotosintesis kelas VII",
            "isi_materi": "Fotosintesis membutuhkan cahaya matahari.",
        },
    )
    job_id = response.json()["job_id"]

    assert (await client.get(f"/v1/jobs/{job_id}", headers=budi)).status_code == 200

    lain = await client.get(f"/v1/jobs/{job_id}", headers=citra)
    assert lain.status_code == 404
    assert lain.json()["error"]["code"] == "job_not_found"