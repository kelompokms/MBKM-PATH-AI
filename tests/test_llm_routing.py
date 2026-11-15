"""Test pemilihan model: kapan berbayar, kapan gratis, kapan berhenti.

Ini bagian paling mudah salah dan paling mahal akibatnya: laporan nilai
yang sebagian dihasilkan model berbeda, atau job yang diam-diam turun
kualitas padahal seharusnya berhenti dengan jelas.
"""

from __future__ import annotations

import pytest
from app.core.config import settings
from app.core.errors import BudgetExhausted
from app.core.llm import Completion, LLMResponse, llm
from app.core.tasks import Task
from app.domain.schemas import StudentGradeDraft
from app.observability.free_quota import free_quota
from app.observability.ledger import ledger

OWNER = "ujiowner"


@pytest.fixture(autouse=True)
async def _siapkan(monkeypatch: pytest.MonkeyPatch) -> None:
    await ledger.reset(OWNER)

    async def kuota_tersedia(needed: int = 1):
        return True

    monkeypatch.setattr(free_quota, "available", kuota_tersedia)
    monkeypatch.setattr(free_quota, "consume", _noop)


async def _noop(*_args, **_kwargs) -> None:
    return None


def _pasang_panggilan(monkeypatch: pytest.MonkeyPatch, catatan: list[str]):
    async def fake_call(request, model):
        catatan.append(model)
        return LLMResponse(
            content="{}",
            model=model,
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
        )

    monkeypatch.setattr(llm, "_call_with_retry", fake_call)


async def test_anggaran_cukup_memakai_model_berbayar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "budget_tokens_per_day", 100_000)
    catatan: list[str] = []
    _pasang_panggilan(monkeypatch, catatan)

    hasil = await llm.complete(
        task=Task.ITEM_GRADE,
        messages=[{"role": "user", "content": "nilai jawaban ini"}],
        owner=OWNER,
    )

    assert catatan == [settings.model_paid]
    assert hasil.route == "paid"
    assert hasil.degraded is False
    # Pemakaian sebenarnya dicatat, bukan estimasi.
    status = await ledger.status(OWNER)
    assert status.used == 150
    assert status.reserved == 0


async def test_anggaran_habis_turun_ke_gratis_untuk_task_non_kritikal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "budget_tokens_per_day", 1)
    await ledger.reserve(OWNER, 1)  # kunci seluruh anggaran

    catatan: list[str] = []
    _pasang_panggilan(monkeypatch, catatan)

    hasil = await llm.complete(
        task=Task.PAIR_JUDGE,
        messages=[{"role": "user", "content": "nilai kemiripan"}],
        owner=OWNER,
    )

    assert catatan == [settings.model_free]
    assert hasil.route == "free"
    assert hasil.degraded is True


async def test_anggaran_habis_task_kritikal_berhenti(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "budget_tokens_per_day", 1)
    await ledger.reserve(OWNER, 1)

    catatan: list[str] = []
    _pasang_panggilan(monkeypatch, catatan)

    with pytest.raises(BudgetExhausted) as info:
        await llm.complete(
            task=Task.ITEM_GRADE,
            messages=[{"role": "user", "content": "nilai jawaban ini"}],
            owner=OWNER,
        )

    assert "tidak boleh memakai model gratis" in info.value.message
    # Model tidak pernah dipanggil sama sekali.
    assert catatan == []


async def test_model_gratis_tidak_dikonfigurasi_berarti_tidak_ada_jalur_gratis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "budget_tokens_per_day", 1)
    monkeypatch.setattr(settings, "model_free", None)
    await ledger.reserve(OWNER, 1)

    catatan: list[str] = []
    _pasang_panggilan(monkeypatch, catatan)

    with pytest.raises(BudgetExhausted):
        await llm.complete(
            task=Task.PAIR_JUDGE,
            messages=[{"role": "user", "content": "nilai kemiripan"}],
            owner=OWNER,
        )

    assert catatan == []


async def test_panggilan_gagal_mengembalikan_jatah_anggaran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "budget_tokens_per_day", 100_000)

    async def gagal(request, model):
        raise RuntimeError("jaringan putus")

    monkeypatch.setattr(llm, "_call_with_retry", gagal)

    with pytest.raises(RuntimeError):
        await llm.complete(
            task=Task.ITEM_GRADE,
            messages=[{"role": "user", "content": "nilai"}],
            owner=OWNER,
        )

    status = await ledger.status(OWNER)
    # Jatah dikembalikan; anggaran tidak terbuang oleh kegagalan.
    assert status.reserved == 0
    assert status.used == 0


async def test_putaran_perbaikan_menyertakan_bentuk_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Perbaikan harus menyebut bentuk JSON, bukan hanya nama field.

    Pada pengujian dengan model sungguhan, model pernah menjawab dengan
    bentuk karangannya sendiri — `{"penilaian": [...], "total_skor": 4}` —
    padahal yang diminta `nilai_per_item` dan `saran`. Daftar masalah saja
    tidak cukup untuk membetulkannya; bentuk konkret dari skema Pydantic
    yang membuat putaran perbaikan berguna.
    """
    jawaban = [
        '{"penilaian": [{"nomor": 1, "skor": 100}], "total_skor": 1}',
        (
            '{"nilai_per_item": [{"nomor": 1, "skor": 100, '
            '"kutipan_bukti": "", "catatan": ""}], "saran": "Pertahankan."}'
        ),
    ]
    terkirim: list[list[dict[str, str]]] = []

    async def complete_palsu(*, task, messages, owner, **kwargs):
        terkirim.append(messages)
        return Completion(
            response=LLMResponse(
                content=jawaban[len(terkirim) - 1], model=settings.model_paid
            ),
            task=task,
            route="paid",
            requested_model=settings.model_paid,
        )

    monkeypatch.setattr(llm, "complete", complete_palsu)

    hasil, _ = await llm.structured(
        task=Task.ITEM_GRADE,
        messages=[{"role": "user", "content": "nilai jawaban ini"}],
        owner=OWNER,
        schema=StudentGradeDraft,
    )

    assert hasil.saran == "Pertahankan."
    assert len(terkirim) == 2, "seharusnya tepat satu putaran perbaikan"

    perbaikan = terkirim[1][-1]["content"]
    assert "Bentuk JSON yang diminta" in perbaikan
    # Nama field berasal dari skema, bukan dari jawaban salah model.
    assert "nilai_per_item" in perbaikan
    assert "total_skor" not in perbaikan