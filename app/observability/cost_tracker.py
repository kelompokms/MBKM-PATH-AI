"""Pencatatan pemakaian per model dan perhitungan biaya.

Dua perbaikan dibanding pendekatan lama:

1. Harga TIDAK di-hardcode. Harga diambil dari katalog publik OpenRouter
   (`GET /api/v1/models`) dan di-cache. Bila katalog gagal diambil, biaya
   dilaporkan `null` — lebih baik "tidak diketahui" daripada angka salah.
2. Pemakaian dicatat per model, bukan satu angka global. Tanpa ini, efek
   peralihan ke model gratis tidak bisa diaudit.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.observability.logger import get_logger

logger = get_logger(__name__)

_CATALOG_TTL_SECONDS = 6 * 60 * 60


@dataclass
class ModelUsage:
    calls: int = 0
    in_tokens: int = 0
    out_tokens: int = 0
    # Sebagian token keluaran adalah token gambar, dan ditagih dengan tarif
    # berbeda. Tanpa memisahkannya, perkiraan biaya model gambar bisa dua
    # puluh kali terlalu kecil.
    image_tokens: int = 0
    images: int = 0
    failures: int = 0
    # Jumlah panggilan yang biayanya dilaporkan sendiri oleh OpenRouter.
    # Angka ini yang dipercaya; harga katalog hanya dipakai bila tidak ada.
    reported_calls: int = 0
    reported_usd: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "in": self.in_tokens,
            "out": self.out_tokens,
            "out_image_tokens": self.image_tokens,
            "images": self.images,
            "failures": self.failures,
            "total_tokens": self.in_tokens + self.out_tokens,
            "reported_calls": self.reported_calls,
            "reported_usd": round(self.reported_usd, 6),
        }


@dataclass
class PriceEntry:
    prompt_per_token: float
    completion_per_token: float
    # `pricing.image_output` di katalog. Namanya menyesatkan: nilainya adalah
    # tarif per **token** gambar, bukan per gambar. Satu gambar sekitar 1.120
    # token, jadi tarif $0,00003 menjadi sekitar $0,03 per gambar.
    image_per_token: float = 0.0


class PriceRegistry:
    """Harga per token dari katalog OpenRouter, dengan cache dan kegagalan lunak."""

    def __init__(self) -> None:
        self._prices: dict[str, PriceEntry] | None = None
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def _load(self) -> dict[str, PriceEntry] | None:
        url = f"{settings.openrouter_base_url}/models"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url)
            if response.status_code != 200:
                logger.warning("Katalog harga tidak tersedia", status=response.status_code)
                return None

            payload = response.json()
            rows = payload.get("data")
            if not isinstance(rows, list):
                return None

            prices: dict[str, PriceEntry] = {}
            for row in rows:
                model_id = row.get("id")
                pricing = row.get("pricing") or {}
                if not isinstance(model_id, str):
                    continue
                try:
                    prices[model_id] = PriceEntry(
                        prompt_per_token=float(pricing.get("prompt", 0) or 0),
                        completion_per_token=float(pricing.get("completion", 0) or 0),
                        image_per_token=float(pricing.get("image_output", 0) or 0),
                    )
                except (TypeError, ValueError):
                    continue
            return prices
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Gagal mengambil katalog harga", error=str(exc))
            return None

    async def get(self) -> dict[str, PriceEntry] | None:
        async with self._lock:
            fresh = (
                self._prices is not None
                and (time.monotonic() - self._fetched_at) < _CATALOG_TTL_SECONDS
            )
            if fresh:
                return self._prices

            loaded = await self._load()
            if loaded is not None:
                self._prices = loaded
                self._fetched_at = time.monotonic()
            return self._prices


def _estimate(usage: ModelUsage, price: PriceEntry) -> float:
    """Perkiraan biaya dari harga katalog.

    Token gambar dipisahkan dari token keluaran biasa. Penyedia menagih
    keduanya dengan tarif berbeda: pada satu gambar uji, 1.120 token keluaran
    seluruhnya berupa token gambar dan ditagih $0,03 — dua puluh kali lipat
    tarif token keluaran biasa. Menghitungnya sebagai token keluaran biasa
    menghasilkan perkiraan yang jauh terlalu kecil.
    """
    token_gambar = min(usage.image_tokens, usage.out_tokens)
    token_teks = usage.out_tokens - token_gambar

    return (
        usage.in_tokens * price.prompt_per_token
        + token_teks * price.completion_per_token
        + token_gambar * price.image_per_token
    )


def _cost_row(usage: ModelUsage, price: PriceEntry | None) -> tuple[float | None, str]:
    """Biaya satu model beserta asal angkanya.

    Urutan kepercayaan, dari yang paling benar:

    1. **dilaporkan** — OpenRouter mengirim `usage.cost` untuk setiap
       panggilan. Ini satu-satunya cara menghitung model gambar dengan tepat:
       satu gambar ditagih sekitar $0,03 dan dilaporkan sebagai 1.120 token
       keluaran, sehingga hitungan dari harga katalog bisa dua puluh kali
       lebih kecil.
    2. **perkiraan** — dipakai hanya bila ada panggilan yang belum dilaporkan
       dan harga katalog tersedia.
    3. **sebagian** — sebagian dilaporkan, harga katalog tidak diketahui.
    4. **tidak_diketahui** — tidak ada dasarnya. Dibiarkan `None`; menebak
       angka biaya lebih buruk daripada mengaku tidak tahu.
    """
    lengkap = usage.reported_calls >= usage.calls and usage.reported_calls > 0
    if lengkap:
        return usage.reported_usd, "dilaporkan"

    if price is not None:
        return _estimate(usage, price), "perkiraan"

    if usage.reported_calls > 0:
        return usage.reported_usd, "sebagian"

    return None, "tidak_diketahui"


class CostTracker:
    def __init__(self, registry: PriceRegistry | None = None) -> None:
        self._usage: dict[str, ModelUsage] = {}
        self._registry = registry or PriceRegistry()
        self._degraded_jobs = 0
        self._lock = asyncio.Lock()

    async def record(
        self,
        model: str,
        *,
        in_tokens: int = 0,
        out_tokens: int = 0,
        image_tokens: int = 0,
        images: int = 0,
        cost_usd: float | None = None,
    ) -> None:
        """Catat satu panggilan.

        `cost_usd` diisi dari `usage.cost` yang dikirim OpenRouter. Angka itu
        yang paling benar: model gambar misalnya menagih sekitar $0,03 per
        gambar dan melaporkannya sebagai 1.120 token keluaran, sehingga
        menghitungnya dari harga per token di katalog akan menghasilkan angka
        sekitar dua puluh kali lebih kecil.
        """
        async with self._lock:
            usage = self._usage.get(model)
            if usage is None:
                usage = ModelUsage()
                self._usage[model] = usage
            usage.calls += 1
            usage.in_tokens += max(0, in_tokens)
            usage.out_tokens += max(0, out_tokens)
            usage.image_tokens += max(0, image_tokens)
            usage.images += max(0, images)
            if cost_usd is not None:
                usage.reported_calls += 1
                usage.reported_usd += max(0.0, cost_usd)

    async def record_failure(self, model: str) -> None:
        async with self._lock:
            usage = self._usage.get(model)
            if usage is None:
                usage = ModelUsage()
                self._usage[model] = usage
            usage.failures += 1

    async def mark_degraded(self) -> None:
        async with self._lock:
            self._degraded_jobs += 1

    async def snapshot(self) -> dict[str, object]:
        async with self._lock:
            usage_copy = {
                model: ModelUsage(
                    calls=u.calls,
                    in_tokens=u.in_tokens,
                    out_tokens=u.out_tokens,
                    image_tokens=u.image_tokens,
                    images=u.images,
                    failures=u.failures,
                    reported_calls=u.reported_calls,
                    reported_usd=u.reported_usd,
                )
                for model, u in self._usage.items()
            }
            degraded = self._degraded_jobs

        prices = await self._registry.get()

        models_out: dict[str, dict[str, object]] = {}
        total_usd = 0.0
        ada_biaya = False

        for model, usage in usage_copy.items():
            harga = prices.get(model) if prices else None
            usd, sumber = _cost_row(usage, harga)

            row = usage.to_dict()
            row["usd"] = None if usd is None else round(usd, 6)
            row["usd_source"] = sumber
            models_out[model] = row

            if usd is not None:
                total_usd += usd
                ada_biaya = True

        return {
            "models": models_out,
            "total_usd": round(total_usd, 6) if ada_biaya else None,
            "degraded_jobs": degraded,
            "prices_available": prices is not None,
        }


cost_tracker = CostTracker()