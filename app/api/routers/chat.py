"""Endpoint chatbot bantuan AI untuk siswa (jawaban teks langsung, tanpa PDF)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import CallerDep
from app.core.errors import InvalidRequest
from app.core.llm import llm
from app.core.tasks import Task
from app.prompts.chat_siswa import build_chat_messages

router = APIRouter(prefix="/v1/chat", tags=["chat"])


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"] = Field(
        description="Pengirim pesan: 'user' (siswa) atau 'assistant' (AI)"
    )
    content: str = Field(min_length=1, max_length=4000, description="Isi pesan")


class ChatRequestBody(BaseModel):
    pesan: str = Field(
        min_length=1,
        max_length=4000,
        description="Pertanyaan atau pesan terbaru dari siswa (wajib)",
    )
    riwayat: list[ChatMessage] = Field(
        default_factory=list,
        max_length=20,
        description="Riwayat percakapan sebelumnya agar AI ingat konteks (opsional)",
    )
    isi_materi: str = Field(
        default="",
        description=(
            "Teks materi dari guru (opsional). Bila diisi, AI hanya menjawab "
            "berdasarkan materi tersebut."
        ),
    )
    jenjang: str = Field(
        default="",
        max_length=80,
        description="Jenjang pendidikan siswa, mis. SMP kelas VII (opsional)",
    )


class ChatResponseBody(BaseModel):
    jawaban: str
    model: str
    degraded: bool
    berbasis_materi: bool


@router.post(
    "",
    summary="Chat bantuan belajar dengan AI",
    description=(
        "Chatbot langsung (mengembalikan JSON teks, bukan PDF dan tanpa polling job). "
        "Dapat diakses oleh peran `siswa:` maupun `guru:`. "
        "Sertakan `riwayat` untuk percakapan multi-putaran, dan `isi_materi` bila "
        "ingin membatasi jawaban pada materi guru."
    ),
    response_model=ChatResponseBody,
)
async def chat_with_ai(
    caller: CallerDep,
    body: ChatRequestBody,
) -> ChatResponseBody:
    pesan = body.pesan.strip()
    if not pesan:
        raise InvalidRequest("Pesan tidak boleh kosong.")

    messages = build_chat_messages(
        pesan=pesan,
        riwayat=[{"role": m.role, "content": m.content} for m in body.riwayat],
        isi_materi=body.isi_materi,
        jenjang=body.jenjang,
    )

    completion = await llm.complete(
        task=Task.STUDENT_CHAT,
        messages=messages,
        owner=caller.fingerprint,
    )

    return ChatResponseBody(
        jawaban=completion.content.strip(),
        model=completion.response.model,
        degraded=completion.degraded,
        berbasis_materi=bool(body.isi_materi.strip()),
    )
