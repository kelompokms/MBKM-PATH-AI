"""Buku besar anggaran token berbayar, per pemilik kunci API.

Mekanisme `reserve` → `settle` dipakai, bukan "potong setelah selesai".
Alasannya konkurensi: dua job yang berjalan bersamaan bisa sama-sama membaca
sisa anggaran yang sama dan sama-sama lolos bila pemotongan baru dilakukan
setelah respons datang. Dengan reserve, salah satu akan ditolak lebih awal.

Anggaran hanya menghitung token model BERBAYAR. Pemakaian model gratis dicatat
terpisah di `cost_tracker` untuk keterlihatan, tetapi tidak mengurangi jatah.

Reset mengikuti hari kalender UTC, sama seperti penghitung kuota gratis
OpenRouter, sehingga dua angka itu berganti pada waktu yang sama.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.core.security import is_student_owner
from app.observability.logger import get_logger

logger = get_logger(__name__)


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _next_reset() -> datetime:
    now = datetime.now(UTC)
    tomorrow = (now + timedelta(days=1)).date()
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=UTC)


@dataclass
class BudgetStatus:
    owner: str
    limit: int
    used: int
    reserved: int
    reset_at: datetime
    # Biaya berbayar yang sudah terpakai pemilik ini, seperti dilaporkan
    # penyedia. Angka ini tidak dipakai untuk membatasi apa pun; ia ada
    # supaya pertanyaan "token ini sebenarnya berapa rupiah" bisa dijawab
    # per pengajar, bukan hanya secara total.
    spent_usd: float = 0.0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used - self.reserved)

    def to_dict(self) -> dict[str, object]:
        return {
            "limit": self.limit,
            "used": self.used,
            "reserved": self.reserved,
            "remaining": self.remaining,
            "spent_usd": round(self.spent_usd, 6),
            "reset_at": self.reset_at.isoformat(),
        }


@dataclass
class _Entry:
    day: str
    used: int = 0
    reserved: int = 0
    spent_usd: float = 0.0
    # Jumlah ilustrasi yang dicoba hari ini. Dihitung terpisah dari token
    # karena biaya gambar tidak sebanding dengan biaya token teks.
    images: int = 0


class TokenLedger:
    def __init__(self, limit: int | None = None) -> None:
        # Batas dibaca dari konfigurasi saat dipakai, bukan dikunci saat
        # modul dimuat. Konfigurasi yang dibekukan pada waktu impor adalah
        # sumber kebingungan: nilainya tidak pernah berubah walau environment
        # sudah diperbarui.
        self._explicit_limit = limit
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    def limit_for(self, owner: str) -> int:
        """Batas harian untuk pemilik ini.

        Pelajar dan pengajar punya batas berbeda karena bentuk pemakaiannya
        berbeda: pengajar menjalankan beberapa batch besar, pelajar mengirim
        banyak pertanyaan kecil. Batasnya dibaca dari awalan pemilik
        (`siswa:` / `guru:`) sehingga tidak perlu daftar pengguna.
        """
        if self._explicit_limit is not None:
            return self._explicit_limit
        if is_student_owner(owner):
            return settings.student_budget_tokens_per_day
        return settings.budget_tokens_per_day

    def _entry(self, owner: str) -> _Entry:
        today = _today()
        entry = self._entries.get(owner)
        if entry is None or entry.day != today:
            entry = _Entry(day=today)
            self._entries[owner] = entry
        return entry

    async def status(self, owner: str) -> BudgetStatus:
        async with self._lock:
            entry = self._entry(owner)
            return BudgetStatus(
                owner=owner,
                limit=self.limit_for(owner),
                used=entry.used,
                reserved=entry.reserved,
                reset_at=_next_reset(),
                spent_usd=entry.spent_usd,
            )

    async def remaining(self, owner: str) -> int:
        return (await self.status(owner)).remaining

    async def reserve(self, owner: str, tokens: int) -> bool:
        """Kunci jatah sebelum panggilan berbayar. False bila tidak cukup."""
        if tokens <= 0:
            return True
        async with self._lock:
            entry = self._entry(owner)
            limit = self.limit_for(owner)
            if entry.used + entry.reserved + tokens > limit:
                logger.info(
                    "Anggaran tidak cukup untuk reserve",
                    owner=owner,
                    need=tokens,
                    used=entry.used,
                    reserved=entry.reserved,
                    limit=limit,
                )
                return False
            entry.reserved += tokens
            return True

    async def settle(
        self,
        owner: str,
        reserved: int,
        actual: int,
        cost_usd: float | None = None,
    ) -> None:
        """Ganti jatah dengan pemakaian sebenarnya setelah respons diterima.

        `cost_usd` adalah biaya yang dilaporkan penyedia. Angka ini dicatat
        terpisah dari jumlah token karena keduanya tidak sebanding: satu
        token gambar jauh lebih mahal daripada satu token teks, sehingga
        jumlah token saja tidak bisa diterjemahkan menjadi biaya.
        """
        async with self._lock:
            entry = self._entry(owner)
            entry.reserved = max(0, entry.reserved - max(0, reserved))
            entry.used += max(0, actual)
            if cost_usd is not None:
                entry.spent_usd += max(0.0, cost_usd)

    async def release(self, owner: str, reserved: int) -> None:
        """Kembalikan jatah bila panggilan gagal sebelum sempat ditagih."""
        if reserved <= 0:
            return
        async with self._lock:
            entry = self._entry(owner)
            entry.reserved = max(0, entry.reserved - reserved)

    async def allow_image(self, owner: str, limit: int | None) -> bool:
        """Izinkan satu ilustrasi bila jatah hariannya masih ada.

        `None` berarti tidak dibatasi — dipakai untuk pengajar, yang jumlah
        ilustrasinya sudah dibatasi per dokumen. Untuk pelajar, `limit` diisi
        dari konfigurasi karena satu ilustrasi menagih sekitar $0,007
        sedangkan satu permintaan teks hanya sepersekian sen.

        Yang dihitung adalah percobaan, bukan hasil: panggilan gambar yang
        gagal tetap memakai jatah, supaya percobaan ulang tidak bisa dipakai
        untuk melewati batas.
        """
        if limit is None:
            return True

        async with self._lock:
            entry = self._entry(owner)
            if entry.images >= max(0, limit):
                logger.info(
                    "Batas ilustrasi harian tercapai", owner=owner, limit=limit
                )
                return False
            entry.images += 1
            return True

    async def reset(self, owner: str | None = None) -> None:
        """Kosongkan catatan. Dipakai test dan operasi manual."""
        async with self._lock:
            if owner is None:
                self._entries.clear()
            else:
                self._entries.pop(owner, None)


ledger = TokenLedger()