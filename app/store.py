"""Penyimpanan sementara kunci jawaban hasil ekstraksi.

Kunci jawaban harus bisa diperiksa guru sebelum dipakai menilai. Karena
layanan ini tidak memakai database, penyimpanan berupa kamus di memori
dengan masa berlaku terbatas.

Konsekuensi yang diterima: bila proses restart di antara tahap ekstraksi
dan penilaian, guru perlu mengunggah ulang kunci. Itu lebih baik daripada
menilai siswa dengan kunci yang tidak pernah diperiksa.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.core.errors import AnswerKeyNotFound
from app.domain.schemas import AnswerKeyDraft
from app.jobs import utc_now
from app.observability.logger import get_logger

logger = get_logger(__name__)


@dataclass
class StoredAnswerKey:
    id: str
    owner: str
    draft: AnswerKeyDraft
    sumber: str
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, object]:
        return {
            "answer_key_id": self.id,
            "sumber": self.sumber,
            "dibuat": self.created_at.isoformat(),
            "needs_review": self.draft.needs_review,
            "confidence": self.draft.confidence,
            "issues": self.draft.issues,
            "jumlah_butir": len(self.draft.items),
        }


class AnswerKeyStore:
    def __init__(self) -> None:
        self._items: dict[str, StoredAnswerKey] = {}
        self._lock = asyncio.Lock()

    async def put(
        self, *, owner: str, draft: AnswerKeyDraft, sumber: str
    ) -> StoredAnswerKey:
        record = StoredAnswerKey(
            id=uuid.uuid4().hex, owner=owner, draft=draft, sumber=sumber
        )
        async with self._lock:
            self._items[record.id] = record
        return record

    async def get(self, key_id: str, owner: str) -> StoredAnswerKey:
        async with self._lock:
            record = self._items.get(key_id)

        if record is None or record.owner != owner:
            raise AnswerKeyNotFound()
        if record.created_at < datetime.now(UTC) - timedelta(
            minutes=settings.answer_key_ttl_minutes
        ):
            async with self._lock:
                self._items.pop(key_id, None)
            raise AnswerKeyNotFound(
                "Kunci jawaban sudah kedaluwarsa. Lakukan ekstraksi ulang."
            )
        return record

    async def sweep(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(
            minutes=settings.answer_key_ttl_minutes
        )
        async with self._lock:
            expired = [
                key_id
                for key_id, record in self._items.items()
                if record.created_at < cutoff
            ]
            for key_id in expired:
                self._items.pop(key_id, None)
        if expired:
            logger.info("Kunci jawaban kedaluwarsa dibersihkan", count=len(expired))
        return len(expired)

    async def clear(self) -> None:
        async with self._lock:
            self._items.clear()


answer_key_store = AnswerKeyStore()