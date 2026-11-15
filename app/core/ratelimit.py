"""Pembatas laju untuk model gratis.

Limit model gratis OpenRouter dihitung PER AKUN, bukan per model atau per
kunci. Satu batch 100 siswa yang mengirim panggilan grey-band berturut-turut
akan langsung menabrak batas per menit dan menghasilkan 429 berantai.

Karena itu kita menahan laju di sisi klien dengan token bucket: panggilan
mengantre singkat, bukan gagal. Ini lebih baik daripada retry-after-failure,
karena retry setelah 429 menghabiskan sisa anggaran waktu job tanpa jaminan.
"""

from __future__ import annotations

import asyncio
import time


class AsyncTokenBucket:
    def __init__(self, rate_per_minute: int, capacity: int | None = None) -> None:
        self._rate = max(1, rate_per_minute) / 60.0  # token per detik
        self._capacity = float(capacity if capacity is not None else rate_per_minute)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._updated = now

    async def acquire(self, tokens: float = 1.0) -> None:
        """Tunggu sampai token tersedia, lalu ambil."""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait_for = deficit / self._rate

            # Lepas lock saat menunggu agar pemanggil lain bisa ikut mengantre.
            await asyncio.sleep(min(wait_for, 5.0))