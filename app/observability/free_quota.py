"""Pembacaan sisa kuota model gratis dari OpenRouter.

Kuota gratis bergantung pada total kredit yang pernah dibeli akun, dan
angkanya TIDAK ditampilkan di dokumentasi sebagai nilai tetap. Satu-satunya
cara yang benar adalah membacanya saat runtime dari `GET /api/v1/key`, yang
menyediakan `free_model_daily_requests.{used,limit,remaining}`.

Bila pembacaan gagal, kita TIDAK menebak. `available()` mengembalikan
`None` yang berarti "tidak diketahui", dan pemanggil memutuskan sendiri:
untuk task non-kritikal lebih baik tetap mencoba, karena kegagalan paling
buruk adalah satu 429 yang sudah ditangani fallback.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from app.core.config import settings
from app.observability.logger import get_logger

logger = get_logger(__name__)

_CACHE_TTL_SECONDS = 5 * 60


class FreeQuota:
    def __init__(self) -> None:
        self._remaining: int | None = None
        self._limit: int | None = None
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def _fetch(self) -> None:
        url = f"{settings.openrouter_base_url}/key"
        headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("Gagal membaca kuota gratis", error=str(exc))
            return

        if response.status_code != 200:
            logger.warning("Kuota gratis tidak terbaca", status=response.status_code)
            return

        try:
            payload = response.json().get("data") or {}
            daily = payload.get("free_model_daily_requests") or {}
            remaining = daily.get("remaining")
            limit = daily.get("limit")
        except (ValueError, AttributeError):
            return

        if isinstance(remaining, int):
            self._remaining = remaining
        if isinstance(limit, int):
            self._limit = limit
        self._fetched_at = time.monotonic()

    async def _refresh_if_stale(self, force: bool = False) -> None:
        async with self._lock:
            stale = (time.monotonic() - self._fetched_at) > _CACHE_TTL_SECONDS
            if force or stale or self._remaining is None:
                await self._fetch()

    async def remaining(self) -> int | None:
        await self._refresh_if_stale()
        return self._remaining

    async def available(self, needed: int = 1) -> bool | None:
        """True/False bila diketahui, None bila tidak terbaca."""
        remaining = await self.remaining()
        if remaining is None:
            return None
        return remaining >= needed

    async def consume(self, count: int = 1) -> None:
        """Kurangi perkiraan lokal segera setelah panggilan gratis berhasil.

        Penghitung sebenarnya ada di sisi OpenRouter; ini hanya mengurangi
        frekuensi pembacaan endpoint.
        """
        if self._remaining is not None:
            self._remaining = max(0, self._remaining - count)

    async def snapshot(self) -> dict[str, object]:
        await self._refresh_if_stale()
        return {
            "used": None
            if self._remaining is None or self._limit is None
            else max(0, self._limit - self._remaining),
            "limit": self._limit,
            "remaining": self._remaining,
        }


free_quota = FreeQuota()