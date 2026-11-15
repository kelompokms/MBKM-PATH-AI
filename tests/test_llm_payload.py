"""Test pembacaan respons penyedia: biaya dan gambar.

Dua hal di sini ditemukan dari pengujian dengan model sungguhan, bukan dari
membaca dokumentasi:

1. Model gambar OpenRouter mengembalikan **JPEG**, bukan PNG. Byte yang
   bukan gambar pernah lolos ke penyusun PDF, dan ilustrasinya hilang
   tanpa pesan apa pun.
2. Satu gambar ditagih sekitar **$0,03** dan dilaporkan sebagai 1.120 token
   keluaran. Menghitung biayanya dari harga per token di katalog
   menghasilkan angka sekitar **dua puluh kali lebih kecil** daripada yang
   sebenarnya ditagih. Penyedia mengirim angka pastinya di `usage.cost`,
   dan itulah yang dipakai.
"""

from __future__ import annotations

import base64
import io
import json

import pytest
from app.core.llm import _image_bytes, _to_llm_response
from app.observability.cost_tracker import CostTracker, ModelUsage, PriceEntry
from PIL import Image as PilImage


def data_uri(mime: str, isi: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(isi).decode()}"


def gambar_jpeg() -> bytes:
    buffer = io.BytesIO()
    PilImage.new("RGB", (32, 18), (43, 95, 140)).save(buffer, format="JPEG")
    return buffer.getvalue()


def respons(
    biaya: object = None,
    gambar: list[str] | None = None,
    token_gambar: int | None = None,
) -> dict:
    usage: dict[str, object] = {"prompt_tokens": 24, "completion_tokens": 1120}
    if biaya is not None:
        usage["cost"] = biaya
    if token_gambar is not None:
        usage["completion_tokens_details"] = {"image_tokens": token_gambar}
    return {
        "model": "google/gemini-3.1-flash-lite-image",
        "choices": [
            {
                "message": {
                    "content": "",
                    "images": [{"image_url": {"url": url}} for url in (gambar or [])],
                }
            }
        ],
        "usage": usage,
    }


# --------------------------------------------------------------------------- #
# Biaya
# --------------------------------------------------------------------------- #


def test_biaya_dari_penyedia_dibaca() -> None:
    """`usage.cost` adalah angka yang paling benar untuk biaya."""
    hasil = _to_llm_response(respons(biaya=0.033606), requested_model="x", latency_ms=1.0)

    assert hasil.cost_usd == pytest.approx(0.033606)


def test_tanpa_laporan_biaya_berarti_none() -> None:
    """Jangan menebak: tidak ada laporan berarti tidak diketahui."""
    hasil = _to_llm_response(respons(), requested_model="x", latency_ms=1.0)

    assert hasil.cost_usd is None


class _RegistryPalsu:
    """Pengganti katalog harga, supaya test tidak menyentuh jaringan."""

    def __init__(self, harga: dict[str, PriceEntry] | None) -> None:
        self._harga = harga

    async def get(self) -> dict[str, PriceEntry] | None:
        return self._harga


async def test_pencatat_memakai_angka_yang_dilaporkan() -> None:
    """Angka penyedia menang atas hitungan dari harga katalog."""
    katalog = {"m": PriceEntry(prompt_per_token=0.000001, completion_per_token=0.000001)}
    pencatat = CostTracker(registry=_RegistryPalsu(katalog))

    await pencatat.record("m", in_tokens=24, out_tokens=1120, images=1, cost_usd=0.033606)
    laporan = await pencatat.snapshot()

    baris = laporan["models"]["m"]
    assert baris["usd"] == pytest.approx(0.033606)
    assert baris["usd_source"] == "dilaporkan"
    # Perhitungan lama (dari katalog) hanya menghasilkan 0,001144.
    assert baris["usd"] > 0.001144 * 20


async def test_pencatat_menandai_angka_perkiraan() -> None:
    """Bila tidak dilaporkan, hasilnya ditandai sebagai perkiraan."""
    katalog = {"m": PriceEntry(prompt_per_token=0.0000001, completion_per_token=0.0000002)}
    pencatat = CostTracker(registry=_RegistryPalsu(katalog))

    await pencatat.record("m", in_tokens=1000, out_tokens=500)
    laporan = await pencatat.snapshot()

    baris = laporan["models"]["m"]
    assert baris["usd_source"] == "perkiraan"
    assert baris["usd"] == pytest.approx(1000 * 0.0000001 + 500 * 0.0000002)


async def test_pencatat_mengaku_tidak_tahu_bila_tidak_ada_dasarnya() -> None:
    pencatat = CostTracker(registry=_RegistryPalsu(None))

    await pencatat.record("m", in_tokens=1000, out_tokens=500)
    laporan = await pencatat.snapshot()

    assert laporan["models"]["m"]["usd"] is None
    assert laporan["models"]["m"]["usd_source"] == "tidak_diketahui"
    assert laporan["total_usd"] is None


# --------------------------------------------------------------------------- #
# Gambar
# --------------------------------------------------------------------------- #


def test_jpeg_dipakai_apa_adanya() -> None:
    """JPEG tidak diubah: mengonversinya ke PNG membengkakkan ukuran PDF."""
    asli = gambar_jpeg()

    hasil = _image_bytes(data_uri("image/jpeg", asli))

    assert hasil == asli


def test_jenis_berkas_di_pembungkus_tidak_dipercaya() -> None:
    """Yang menentukan format adalah isi byte-nya, bukan label di pembungkus."""
    asli = gambar_jpeg()

    # Pembungkus mengaku PNG, isinya JPEG.
    hasil = _image_bytes(data_uri("image/png", asli))

    assert hasil == asli


def gambar_webp(mode: str = "RGB") -> bytes:
    """WebP dengan atau tanpa transparansi.

    WebP adalah format yang benar-benar dikembalikan Recraft V4.1 Flash V2,
    dan format itu tidak bisa ditempelkan ke PDF apa adanya.
    """
    buffer = io.BytesIO()
    warna = (200, 30, 30, 128) if mode == "RGBA" else (200, 30, 30)
    PilImage.new(mode, (32, 18), warna).save(buffer, format="WEBP")
    return buffer.getvalue()


def test_webp_tanpa_alpha_dikonversi_ke_jpeg() -> None:
    """JPEG dipilih karena jauh lebih kecil, dan itu berarti untuk ukuran PDF.

    Terukur pada satu ilustrasi Recraft: WebP 789 KB menjadi PNG 757 KB,
    tetapi hanya 48 KB sebagai JPEG.
    """
    hasil = _image_bytes(data_uri("image/webp", gambar_webp()))

    assert hasil is not None
    assert hasil.startswith(b"\xff\xd8\xff"), "seharusnya JPEG"

    # Catatan: keunggukan ukurannya TIDAK diuji di sini. Pada gambar uji
    # 32x18 yang serba rata, header JPEG (317 byte) justru lebih besar
    # daripada WebP-nya (82 byte). Keunggukan itu baru terlihat pada gambar
    # sungguhan — lihat angka terukurnya di docstring test ini.


def test_webp_ber_alpha_tetap_png() -> None:
    """Transparansi tidak boleh diratakan ke putih tanpa diminta."""
    hasil = _image_bytes(data_uri("image/webp", gambar_webp("RGBA")))

    assert hasil is not None
    assert hasil.startswith(b"\x89PNG\r\n\x1a\n"), "seharusnya PNG"


def test_format_palet_tanpa_alpha_menjadi_jpeg() -> None:
    buffer = io.BytesIO()
    PilImage.new("RGB", (32, 18), (200, 30, 30)).save(buffer, format="GIF")

    hasil = _image_bytes(data_uri("image/gif", buffer.getvalue()))

    assert hasil is not None
    assert hasil.startswith(b"\xff\xd8\xff")


def test_png_dan_jpeg_dipakai_apa_adanya() -> None:
    png = io.BytesIO()
    PilImage.new("RGB", (16, 9), (10, 20, 30)).save(png, format="PNG")

    assert _image_bytes(data_uri("image/png", png.getvalue())) == png.getvalue()
    assert _image_bytes(data_uri("image/jpeg", gambar_jpeg())) == gambar_jpeg()


@pytest.mark.parametrize(
    "isi",
    [
        pytest.param(b"", id="kosong"),
        pytest.param(b"ini bukan gambar sama sekali", id="teks-biasa"),
        pytest.param(gambar_jpeg()[:40], id="jpeg-terpotong"),
    ],
)
def test_yang_bukan_gambar_ditolak(isi: bytes) -> None:
    """Byte yang tidak bisa dibaca PIL tidak boleh sampai ke penyusun PDF."""
    assert _image_bytes(data_uri("image/png", isi)) is None


def test_json_mentah_tidak_disimpan_di_dict() -> None:
    """Sanity check: respons dengan gambar terbaca sebagai dua gambar."""
    hasil = _to_llm_response(
        respons(gambar=["data:image/jpeg;base64,AAAA", "data:image/jpeg;base64,BBBB"]),
        requested_model="x",
        latency_ms=2.0,
    )

    assert len(hasil.images) == 2
    json.dumps(hasil.raw)  # tetap bisa diserialisasi


def test_model_usage_memuat_biaya_dilaporkan() -> None:
    usage = ModelUsage(calls=2, reported_calls=2, reported_usd=0.06)

    baris = usage.to_dict()

    assert baris["reported_calls"] == 2
    assert baris["reported_usd"] == pytest.approx(0.06)


async def test_perkiraan_biaya_gambar_memakai_tarif_token_gambar() -> None:
    """Perkiraan harus memakai tarif token gambar, bukan tarif token biasa.

    Angka di test ini berasal dari satu panggilan sungguhan: OpenRouter
    melaporkan prompt 24 token, keluaran 1.120 token yang seluruhnya token
    gambar, dan biaya `upstream_inference_completions_cost` sebesar
    $0,033600. Tarif token keluaran biasa untuk model ini hanya $0,0000015,
    jadi menghitungnya sebagai token biasa menghasilkan angka 20x lipat
    lebih kecil.
    """
    katalog = {
        "gambar": PriceEntry(
            prompt_per_token=0.00000025,
            completion_per_token=0.0000015,
            image_per_token=0.00003,
        )
    }
    pencatat = CostTracker(registry=_RegistryPalsu(katalog))

    # Tanpa laporan biaya dari penyedia: yang dipakai hanya perkiraan.
    await pencatat.record(
        "gambar", in_tokens=24, out_tokens=1120, image_tokens=1120, images=1
    )
    laporan = await pencatat.snapshot()
    baris = laporan["models"]["gambar"]

    assert baris["usd_source"] == "perkiraan"
    # $0,000006 (24 token masuk) + $0,033600 (1.120 token gambar).
    # Angka ini sama persis dengan `upstream_inference_cost` yang dilaporkan
    # penyedia pada panggilan itu, jadi rumusnya memang tepat.
    assert baris["usd"] == pytest.approx(0.033606)
    assert baris["out_image_tokens"] == 1120


async def test_perkiraan_memisahkan_token_teks_dan_token_gambar() -> None:
    katalog = {
        "m": PriceEntry(
            prompt_per_token=0.000001,
            completion_per_token=0.000002,
            image_per_token=0.0001,
        )
    }
    pencatat = CostTracker(registry=_RegistryPalsu(katalog))

    # 100 token keluaran: 40 token gambar, 60 token teks.
    await pencatat.record("m", in_tokens=10, out_tokens=100, image_tokens=40, images=1)
    laporan = await pencatat.snapshot()

    diharapkan = 10 * 0.000001 + 60 * 0.000002 + 40 * 0.0001
    assert laporan["models"]["m"]["usd"] == pytest.approx(diharapkan)


async def test_token_gambar_tidak_boleh_melebihi_token_keluaran() -> None:
    """Data penyedia yang aneh tidak boleh membuat biaya jadi negatif."""
    katalog = {
        "m": PriceEntry(
            prompt_per_token=0.0, completion_per_token=0.000002, image_per_token=0.0001
        )
    }
    pencatat = CostTracker(registry=_RegistryPalsu(katalog))

    await pencatat.record("m", in_tokens=0, out_tokens=10, image_tokens=999, images=1)
    laporan = await pencatat.snapshot()

    # Seluruh 10 token keluaran dianggap token gambar; sisanya nol, bukan negatif.
    assert laporan["models"]["m"]["usd"] == pytest.approx(10 * 0.0001)


def test_rincian_token_gambar_dibaca_dari_penyedia() -> None:
    """`usage.completion_tokens_details.image_tokens` dilaporkan penyedia.

    Angka ini yang membedakan token gambar dari token keluaran biasa, dan
    tanpa membacanya perkiraan biaya model gambar salah sekitar dua puluh kali.
    """
    hasil = _to_llm_response(
        respons(biaya=0.033606, token_gambar=1120), requested_model="x", latency_ms=1.0
    )

    assert hasil.image_tokens == 1120


def test_tanpa_rincian_token_gambar_berarti_nol() -> None:
    hasil = _to_llm_response(respons(), requested_model="x", latency_ms=1.0)

    assert hasil.image_tokens == 0
