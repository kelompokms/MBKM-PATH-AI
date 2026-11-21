"""Test integrasi chatbot bantuan belajar untuk siswa (`POST /v1/chat`)."""

from __future__ import annotations

import httpx
import pytest
from app.core.config import settings
from app.core.llm import Completion, LLMResponse, llm
from app.core.tasks import Task

from tests.conftest import HEADERS


def _fake_completion(text: str = "Fotosintesis butuh cahaya matahari.") -> Completion:
    return Completion(
        response=LLMResponse(
            content=text,
            model=settings.model_paid,
            prompt_tokens=80,
            completion_tokens=40,
            total_tokens=120,
        ),
        task=Task.STUDENT_CHAT,
        route="paid",
        requested_model=settings.model_paid,
        degraded=False,
    )


async def test_chat_siswa_langsung_mengembalikan_jawaban_json(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    terkirim: list[list[dict]] = []

    async def fake_complete(*, task, messages, owner, **kwargs):
        assert task == Task.STUDENT_CHAT
        terkirim.append(messages)
        return _fake_completion("Cahaya matahari dipakai klorofil untuk membuat glukosa.")

    monkeypatch.setattr(llm, "complete", fake_complete)

    headers = {
        "X-API-Key": HEADERS["X-API-Key"],
        "X-Actor-Id": "siswa:budi@sekolah.id",
    }
    response = await client.post(
        "/v1/chat",
        headers=headers,
        json={
            "pesan": "Kenapa tumbuhan butuh cahaya matahari?",
            "jenjang": "SMP kelas VII",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert "glukosa" in body["jawaban"]
    assert body["berbasis_materi"] is False
    assert body["degraded"] is False
    assert len(terkirim) == 1
    assert "SMP kelas VII" in terkirim[0][0]["content"]


async def test_chat_dengan_materi_dan_riwayat_terkirim_ke_model(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    terkirim: list[list[dict]] = []

    async def fake_complete(*, task, messages, owner, **kwargs):
        terkirim.append(messages)
        return _fake_completion("Betul, prosesnya berlangsung di kloroplas.")

    monkeypatch.setattr(llm, "complete", fake_complete)

    headers = {
        "X-API-Key": HEADERS["X-API-Key"],
        "X-Actor-Id": "siswa:ani@sekolah.id",
    }
    response = await client.post(
        "/v1/chat",
        headers=headers,
        json={
            "pesan": "Di bagian sel mana itu terjadi?",
            "riwayat": [
                {"role": "user", "content": "Apa itu fotosintesis?"},
                {"role": "assistant", "content": "Proses membuat makanan pada tumbuhan."},
            ],
            "isi_materi": "Fotosintesis berlangsung di dalam kloroplas pada daun.",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["berbasis_materi"] is True
    assert "kloroplas" in body["jawaban"]

    msgs = terkirim[0]
    assert msgs[0]["role"] == "system"
    assert "MATERI DARI GURU" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "Apa itu fotosintesis?"}
    assert msgs[2] == {
        "role": "assistant",
        "content": "Proses membuat makanan pada tumbuhan.",
    }
    assert msgs[3] == {"role": "user", "content": "Di bagian sel mana itu terjadi?"}


async def test_chat_pesan_kosong_ditolak(client: httpx.AsyncClient) -> None:
    headers = {
        "X-API-Key": HEADERS["X-API-Key"],
        "X-Actor-Id": "siswa:budi@sekolah.id",
    }
    response = await client.post(
        "/v1/chat",
        headers=headers,
        json={"pesan": "   "},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_toggle_model_gratis_memilih_rute_free_dan_tidak_untuk_penilaian(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Toggle `prefer_free` hanya mengarahkan task non-penilaian ke model gratis."""
    from app.core.llm import prefer_free_model

    monkeypatch.setattr(settings, "model_free", "google/gemma-4-26b-a4b-it:free")

    with prefer_free_model(True):
        route_chat = await llm._decide_route(
            Task.STUDENT_CHAT, "owner-1", 1000, prefer_free=True
        )
        route_materi = await llm._decide_route(
            Task.MATERIAL_CONTENT, "owner-1", 1000, prefer_free=True
        )
        route_grade = await llm._decide_route(
            Task.ITEM_GRADE, "owner-1", 1000, prefer_free=True
        )

    assert route_chat == "free"
    assert route_materi == "free"
    assert route_grade == "paid"
