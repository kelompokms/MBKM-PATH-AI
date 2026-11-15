"""Gerbang tunggal ke model bahasa dan model gambar.

Semua panggilan ke OpenRouter melewati kelas ini, sehingga keputusan yang
mahal dan mudah salah tidak tersebar di seluruh kode:

- model mana yang dipakai (berbayar atau gratis),
- apakah anggaran masih cukup (reserve sebelum kirim),
- berapa `max_tokens` (selalu dipasang, tidak pernah dibiarkan kosong),
- berapa kali dicoba ulang dan kapan menyerah,
- pencatatan pemakaian per model,
- penandaan job yang turun kualitas.

Urutan pemilihan model: anggaran berbayar dipakai lebih dulu. Hanya ketika
anggaran tidak cukup, task yang `may_use_free` dialihkan ke model gratis.
Task yang menyentuh nilai siswa tidak pernah dialihkan — ia berhenti dengan
`BudgetExhausted` yang jelas, bukan menghasilkan laporan dengan standar
penilaian berbeda.

Susunan berkas ini, dari dalam ke luar:

    _build_chat_payload → _send_chat_request → _raise_for_status
                                                → _parse_chat_response
    _post            satu panggilan HTTP, tanpa percobaan ulang
    _call_with_retry panggilan + percobaan ulang
    complete         pilih model, kunci anggaran, catat pemakaian
    structured       paksa keluaran sesuai skema Pydantic
    generate_image   jalur ilustrasi (kegagalan selalu dilewati)
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

import httpx
from PIL import Image
from pydantic import BaseModel

from app.core import jsonio
from app.core.config import settings
from app.core.errors import (
    AppError,
    BudgetExhausted,
    LLMAuthError,
    LLMBadResponse,
    LLMCreditsExhausted,
    LLMUnavailable,
)
from app.core.ratelimit import AsyncTokenBucket
from app.core.security import is_student_owner
from app.core.tasks import Task, spec_for
from app.core.tokens import estimate_messages_tokens
from app.observability.cost_tracker import cost_tracker
from app.observability.free_quota import free_quota
from app.observability.ledger import ledger
from app.observability.logger import get_logger

logger = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)
Route = Literal["paid", "free"]

# Status yang layak dicoba ulang. 400/404/422 tidak termasuk: permintaan
# yang salah bentuk tidak akan berhasil walau diulang.
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

# Jatah anggaran yang dikunci sebelum satu ilustrasi dibuat. Nilainya adalah
# perkiraan terburuk, bukan rata-rata: yang dijaga oleh angka ini adalah
# beberapa job yang berjalan bersamaan. Recraft memakai sekitar 4.175 token
# keluaran per gambar, jadi angka lama (2.000) hanya menutup separuh
# kebutuhan dan gerbang anggarannya bisa dilewati.
_IMAGE_TOKEN_RESERVE = 5_000

# Parameter panggilan gambar.
_IMAGE_TEMPERATURE = 0.7
# Batas token keluaran untuk satu gambar. Recraft memakai sekitar 4.175
# token per gambar; batas yang terlalu kecil memotong gambarnya, lalu
# gambarnya ditolak saat divalidasi dan ilustrasinya hilang tanpa pesan.
_IMAGE_MAX_TOKENS = 8_000
# Hanya "image". Menambahkan "text" membuat sebagian model menolak
# permintaan: Recraft hanya mengeluarkan gambar, sehingga permintaan dengan
# modalitas "image, text" dijawab 404 tanpa penjelasan yang berguna.
_IMAGE_MODALITIES = ["image"]

# Panjang keluaran model yang dikirim balik saat meminta perbaikan JSON.
_REPAIR_ECHO_CHARS = 6_000

# Batas panjang bentuk JSON yang disertakan pada putaran perbaikan.
_REPAIR_SCHEMA_MAX_CHARS = 4_000

# Format gambar yang bisa ditempelkan ke PDF apa adanya, supaya byte-nya
# tidak perlu dikodekan ulang.
_EMBEDDABLE_IMAGE_FORMATS = frozenset({"JPEG", "PNG"})

# Kualitas JPEG untuk gambar yang dikodekan ulang. Terukur pada satu
# ilustrasi Recraft: WebP 789 KB menjadi PNG 757 KB, tetapi hanya 48 KB
# sebagai JPEG. Untuk halaman PDF berlatar putih, ukuran itu jauh lebih
# berarti daripada beda mutu yang tidak terlihat.
_IMAGE_JPEG_QUALITY = 90

# Batas percobaan ulang: jeda naik dua kali lipat, maksimal 20 detik.
_BACKOFF_BASE = 2
_BACKOFF_CAP = 20


@dataclass
class LLMResponse:
    """Hasil mentah satu panggilan HTTP ke OpenRouter."""

    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0
    # Sebagian token keluaran adalah token gambar dan ditagih dengan tarif
    # berbeda. Angka ini dilaporkan penyedia di
    # `usage.completion_tokens_details.image_tokens`.
    image_tokens: int = 0
    images: list[str] = field(default_factory=list)
    # Biaya dalam USD seperti dilaporkan OpenRouter. Diisi bila penyedia
    # mengirimkannya; lebih dipercaya daripada hitungan dari harga katalog.
    cost_usd: float | None = None
    raw: dict[str, Any] | None = field(default=None, repr=False)


@dataclass
class Completion:
    """Hasil satu panggilan, beserta jejak model yang benar-benar dipakai."""

    response: LLMResponse
    task: Task
    route: Route
    requested_model: str
    degraded: bool = False

    @property
    def content(self) -> str:
        return self.response.content


@dataclass
class ChatRequest:
    """Isi satu permintaan chat, tanpa nama model.

    Model sengaja terpisah karena ditentukan belakangan, setelah rute
    (berbayar atau gratis) diputuskan.
    """

    messages: list[dict[str, Any]]
    temperature: float
    max_tokens: int
    response_format: dict[str, Any] | None = None
    extra_body: dict[str, Any] | None = None


class _Retryable(Exception):
    """Galat sementara: panggilan yang sama layak dicoba ulang."""

    def __init__(self, message: str, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


# ---------------------------------------------------------------------------
# Penyusunan payload dan pembacaan respons (murni, tanpa I/O)
# ---------------------------------------------------------------------------


def _build_chat_payload(request: ChatRequest, model: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": request.messages,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
    }
    if request.response_format:
        payload["response_format"] = request.response_format
    if request.extra_body:
        payload.update(request.extra_body)
    return payload


def _extract_image_urls(message: dict[str, Any]) -> list[str]:
    """Kumpulkan URL gambar dari `choices[0].message.images[]`."""
    urls: list[str] = []
    for item in message.get("images") or []:
        url = None
        if isinstance(item, dict):
            url = (item.get("image_url") or {}).get("url") or item.get("url")
        elif isinstance(item, str):
            url = item
        if isinstance(url, str):
            urls.append(url)
    return urls


def _to_llm_response(
    data: dict[str, Any], *, requested_model: str, latency_ms: float
) -> LLMResponse:
    """Ubah badan respons OpenRouter menjadi `LLMResponse`."""
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = data.get("usage") or {}

    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)

    dilaporkan = usage.get("cost")
    cost_usd = float(dilaporkan) if isinstance(dilaporkan, (int, float)) else None

    rincian = usage.get("completion_tokens_details") or {}
    image_tokens = int(rincian.get("image_tokens") or 0)

    return LLMResponse(
        content=message.get("content") or "",
        model=str(data.get("model") or requested_model),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens or (prompt_tokens + completion_tokens),
        latency_ms=round(latency_ms, 2),
        image_tokens=image_tokens,
        images=_extract_image_urls(message),
        cost_usd=cost_usd,
        raw=data,
    )


def _decode_data_uri(value: str) -> bytes | None:
    """Buka pembungkus `data:...;base64,` dan kembalikan isinya.

    Jenis berkas yang tertulis di pembungkus itu tidak dipercaya dan tidak
    dipakai; yang menentukan format adalah isi byte-nya sendiri, dan itu
    diperiksa di `_image_bytes`.
    """
    if not value:
        return None
    if value.startswith("data:"):
        _, _, payload = value.partition(",")
    else:
        payload = value
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        logger.warning("Gambar yang dikembalikan model tidak dapat didekode")
        return None


def _image_bytes(value: str) -> bytes | None:
    """Ubah data URI menjadi byte gambar yang sudah dipastikan bisa dipakai.

    Model gambar OpenRouter mengembalikan **JPEG**, bukan PNG. Dua hal
    diperiksa di sini, dan keduanya perlu:

    1. isinya benar-benar gambar yang utuh — dibuka dan dimuat penuh oleh
       PIL, sehingga gambar yang terpotong ikut tertolak;
    2. formatnya termasuk yang bisa ditempelkan ke PDF. JPEG dan PNG dipakai
       apa adanya, selebihnya dikonversi ke PNG.

    Tanpa pemeriksaan ini, byte yang bukan gambar akan lolos ke penyusun PDF
    dan ilustrasinya hilang diam-diam.
    """
    mentah = _decode_data_uri(value)
    if mentah is None:
        return None

    try:
        with Image.open(io.BytesIO(mentah)) as gambar:
            gambar.load()
            if gambar.format in _EMBEDDABLE_IMAGE_FORMATS:
                return mentah
            return _encode_for_pdf(gambar)
    except Exception as exc:  # gambar rusak tidak boleh mematikan job
        logger.warning(
            "Gambar dari model tidak dapat dibaca", error=str(exc)[:160]
        )
        return None


def _encode_for_pdf(gambar: Image.Image) -> bytes:
    """Kodekan ulang gambar ke format yang bisa ditempelkan ke PDF.

    JPEG dipakai bila gambarnya tidak punya transparansi: pada satu ilustrasi
    Recraft, PNG menghasilkan 757 KB sedangkan JPEG hanya 48 KB. Gambar
    ber-alpha tetap disimpan sebagai PNG, karena meratakan transparansi ke
    warna putih akan mengubah tampilannya.
    """
    hasil = io.BytesIO()
    if gambar.mode in ("RGBA", "LA") or "transparency" in gambar.info:
        gambar.save(hasil, format="PNG", optimize=True)
    else:
        gambar.convert("RGB").save(
            hasil, format="JPEG", quality=_IMAGE_JPEG_QUALITY, optimize=True
        )
    return hasil.getvalue()


def _schema_hint(schema: type[BaseModel]) -> str:
    """Bentuk JSON yang diminta, diambil dari skema Pydantic.

    Ini yang membuat putaran perbaikan benar-benar berguna. Tanpa bentuk
    konkret, model hanya diberi tahu nama field yang kurang dan akan
    mengarang strukturnya sendiri lagi — pernah terjadi pada pengujian
    nyata: keluaran berbentuk `{"penilaian": [...], "total_skor": 4}`
    padahal yang diminta `{"nilai_per_item": [...], "saran": "..."}`.

    Perhatikan: `response_format: json_object` hanya menjamin "JSON apa
    saja", bukan bentuk tertentu. Bentuknya harus disebutkan kepada model.
    """
    bentuk = json.dumps(
        schema.model_json_schema(), ensure_ascii=False, indent=None
    )
    if len(bentuk) > _REPAIR_SCHEMA_MAX_CHARS:
        return bentuk[:_REPAIR_SCHEMA_MAX_CHARS] + "…"
    return bentuk


def _repair_messages(
    messages: list[dict[str, Any]],
    previous: str,
    problems: list[str],
    schema: type[BaseModel],
) -> list[dict[str, Any]]:
    """Pesan lanjutan yang meminta model memperbaiki struktur keluarannya.

    Tiga hal dikirim: jawaban sebelumnya, daftar masalah yang konkret, dan
    bentuk JSON yang diminta. Daftar masalah saja tidak cukup.
    """
    return [
        *messages,
        {"role": "assistant", "content": previous[:_REPAIR_ECHO_CHARS]},
        {
            "role": "user",
            "content": (
                "Jawaban sebelumnya tidak lolos validasi struktur. "
                "Perbaiki dan kirim ulang HANYA objek JSON yang valid, "
                "tanpa penjelasan tambahan.\n\n"
                "Masalah yang harus diperbaiki:\n"
                + "\n".join(f"- {problem}" for problem in problems)
                + "\n\nBentuk JSON yang diminta:\n"
                + _schema_hint(schema)
            ),
        },
    ]


class LLMGateway:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._free_bucket = AsyncTokenBucket(settings.free_rate_per_minute)

    async def _http(self) -> httpx.AsyncClient:
        """Klien HTTP tunggal, dibuat sekali saat pertama dipakai."""
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        base_url=settings.openrouter_base_url,
                        headers={
                            "Authorization": f"Bearer {settings.openrouter_api_key}",
                            "Content-Type": "application/json",
                            "HTTP-Referer": settings.openrouter_referer,
                            "X-Title": settings.openrouter_title,
                        },
                        timeout=httpx.Timeout(settings.request_timeout, connect=15.0),
                    )
        return self._client

    # ------------------------------------------------------------------
    # Pemilihan model
    # ------------------------------------------------------------------

    async def _decide_route(self, task: Task, owner: str, estimate: int) -> Route:
        """Tentukan rute berdasarkan sisa anggaran dan boleh-tidaknya task turun."""
        spec = spec_for(task)
        can_go_free = spec.may_use_free and bool(settings.model_free)

        if not can_go_free:
            if await ledger.remaining(owner) < estimate:
                raise BudgetExhausted(
                    "Anggaran token untuk kunci API ini tidak cukup untuk "
                    f"tugas '{task.value}', dan tugas ini tidak boleh memakai "
                    "model gratis agar hasil penilaian tetap konsisten."
                )
            return "paid"

        if await ledger.remaining(owner) >= estimate:
            return "paid"

        # Anggaran berbayar tidak cukup; pertimbangkan model gratis.
        available = await free_quota.available(1)
        if available is False:
            raise BudgetExhausted(
                "Anggaran token habis dan kuota model gratis untuk hari ini "
                "sudah terpakai seluruhnya."
            )

        logger.info("Beralih ke model gratis", task=task.value, owner=owner)
        return "free"

    async def _resolve_route_with_reservation(
        self, task: Task, owner: str, estimate: int
    ) -> tuple[Route, int]:
        """Tentukan rute final sekaligus kunci jatah anggaran bila berbayar.

        Mengembalikan `(rute, jatah)`. Jatah bernilai 0 berarti tidak ada
        apa pun yang perlu dilepas pemanggil.
        """
        route = await self._decide_route(task, owner, estimate)
        if route == "free":
            return "free", 0

        if await ledger.reserve(owner, estimate):
            return "paid", estimate

        # Balapan dengan job lain: anggaran menyusut di antara pemeriksaan
        # dan reserve. Ulangi keputusan sekali, dan bila anggaran memang
        # sudah habis untuk task kritikal, berhenti dengan pesan yang jelas.
        route = await self._decide_route(task, owner, estimate)
        if route == "paid":
            raise BudgetExhausted()
        return "free", 0

    # ------------------------------------------------------------------
    # Panggilan mentah
    # ------------------------------------------------------------------

    async def _post(
        self, request: ChatRequest, model: str
    ) -> LLMResponse:
        """Satu panggilan HTTP ke OpenRouter, tanpa percobaan ulang."""
        payload = _build_chat_payload(request, model)

        client = await self._http()
        started = time.perf_counter()
        response = await _send_chat_request(client, payload)
        latency_ms = (time.perf_counter() - started) * 1000

        _raise_for_status(response)
        data = _parse_chat_response(response)
        return _to_llm_response(
            data, requested_model=model, latency_ms=latency_ms
        )

    async def _call_with_retry(
        self, request: ChatRequest, model: str
    ) -> LLMResponse:
        """Panggil model dengan percobaan ulang dan jeda yang naik bertahap."""
        attempts = settings.max_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                return await self._post(request, model)
            except _Retryable as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                delay = min(_BACKOFF_BASE**attempt * 2, _BACKOFF_CAP)
                logger.warning(
                    "Percobaan ulang panggilan model",
                    model=model,
                    attempt=attempt,
                    delay_s=delay,
                    reason=str(exc)[:160],
                )
                await asyncio.sleep(delay)

        await cost_tracker.record_failure(model)
        raise LLMUnavailable(
            "Model AI tidak merespons setelah beberapa percobaan.",
            details={
                "model": model,
                "last_error": str(last_error)[:200] if last_error else None,
            },
        )

    # ------------------------------------------------------------------
    # API publik
    # ------------------------------------------------------------------

    async def _call_paid(
        self, request: ChatRequest, model: str, owner: str, reserved: int
    ) -> LLMResponse:
        """Panggil model berbayar, lalu ganti jatah dengan pemakaian nyata."""
        try:
            response = await self._call_with_retry(request, model)
        except BaseException:
            # Panggilan gagal sebelum sempat ditagih: kembalikan jatahnya.
            await ledger.release(owner, reserved)
            raise
        await ledger.settle(
            owner, reserved, response.total_tokens, cost_usd=response.cost_usd
        )
        return response

    async def _call_free(self, request: ChatRequest, model: str) -> LLMResponse:
        """Panggil model gratis, ditahan lajunya agar tidak kena 429 beruntun."""
        await self._free_bucket.acquire()
        response = await self._call_with_retry(request, model)
        await free_quota.consume()
        return response

    async def read_image(
        self,
        *,
        task: Task,
        messages: list[dict[str, Any]],
        owner: str,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Baca satu gambar dengan model tertentu, lalu kembalikan teksnya.

        Dipisah dari `complete` karena modelnya bukan model teks utama: model
        penglihatan punya harga dan kemampuan yang berbeda, sedangkan `complete`
        selalu memilih `model_paid`.

        Rute gratis tidak berlaku di sini. Satu halaman yang dibaca model gratis
        dan halaman lain dibaca model berbayar akan menghasilkan teks dengan dua
        mutu berbeda di dalam satu dokumen, dan itu merusak bahan yang dipakai
        untuk menilai.
        """
        spec = spec_for(task)
        request = ChatRequest(
            messages=messages,
            temperature=spec.temperature if temperature is None else temperature,
            max_tokens=max_tokens or spec.max_out_tokens,
        )

        reserved = spec.est_in_tokens + spec.est_out_tokens
        if not await ledger.reserve(owner, reserved):
            raise BudgetExhausted(
                "Anggaran token tidak cukup untuk membaca halaman bergambar.",
                details={"perkiraan_token": reserved},
            )

        response = await self._call_paid(request, model, owner, reserved)
        await self._record_usage(response, model)
        return response.content.strip()

    async def complete(
        self,
        *,
        task: Task,
        messages: list[dict[str, Any]],
        owner: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> Completion:
        """Satu panggilan lengkap: pilih model, kunci anggaran, catat pemakaian.

        `temperature` dan `max_tokens` diambil dari katalog task bila tidak
        disebutkan, supaya tidak ada panggilan tanpa batas keluaran.
        """
        spec = spec_for(task)
        estimate = estimate_messages_tokens(messages) + spec.est_out_tokens

        request = ChatRequest(
            messages=messages,
            temperature=spec.temperature if temperature is None else temperature,
            max_tokens=max_tokens or spec.max_out_tokens,
            response_format=response_format,
            extra_body=extra_body,
        )

        route, reserved = await self._resolve_route_with_reservation(task, owner, estimate)

        if route == "paid":
            model = settings.model_paid
            response = await self._call_paid(request, model, owner, reserved)
        else:
            model = settings.model_free or settings.model_paid
            response = await self._call_free(request, model)

        await self._record_usage(response, model)

        degraded = route == "free"
        if degraded:
            await cost_tracker.mark_degraded()

        return Completion(
            response=response, task=task, route=route,
            requested_model=model, degraded=degraded,
        )

    async def _json_completion(
        self,
        *,
        task: Task,
        messages: list[dict[str, Any]],
        owner: str,
        temperature: float | None,
        max_tokens: int | None,
    ) -> Completion:
        """Panggilan yang meminta keluaran berformat JSON."""
        return await self.complete(
            task=task,
            messages=messages,
            owner=owner,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )

    async def structured(
        self,
        *,
        task: Task,
        messages: list[dict[str, Any]],
        owner: str,
        schema: type[SchemaT],
        max_repairs: int = 1,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[SchemaT, Completion]:
        """Panggil model dan paksa keluarannya sesuai skema Pydantic.

        Bila keluaran tidak sesuai skema, satu putaran perbaikan dikirim:
        jawaban sebelumnya dikembalikan bersama daftar masalah konkret.
        Ini menutup kegagalan yang paling sering terjadi — model menjawab
        dengan bentuk yang mirip tetapi salah, tanpa pernah melaporkan error.
        """
        completion = await self._json_completion(
            task=task, messages=messages, owner=owner,
            temperature=temperature, max_tokens=max_tokens,
        )
        parsed, problems = jsonio.validate_into(completion.content, schema)
        if parsed is not None:
            return parsed, completion

        for repair_index in range(max_repairs):
            logger.warning(
                "Keluaran model tidak sesuai skema, mengirim putaran perbaikan",
                task=task.value,
                repair=repair_index + 1,
                problems=problems[:6],
            )
            completion = await self._json_completion(
                task=task, owner=owner, temperature=0.0, max_tokens=max_tokens,
                messages=_repair_messages(
                    messages, completion.content, problems, schema
                ),
            )
            parsed, problems = jsonio.validate_into(completion.content, schema)
            if parsed is not None:
                return parsed, completion

        raise LLMBadResponse(
            "Model AI tidak menghasilkan struktur yang valid setelah diperbaiki.",
            details={"task": task.value, "problems": problems[:8]},
        )

    async def _call_image(
        self, prompt: str, aspect: str, model: str
    ) -> LLMResponse:
        """Panggilan model gambar, terpisah karena parameternya berbeda."""
        request = ChatRequest(
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"{prompt}\n\nRasio aspek: {aspect}. "
                        "Tanpa teks apa pun di dalam gambar."
                    ),
                }
            ],
            temperature=_IMAGE_TEMPERATURE,
            max_tokens=_IMAGE_MAX_TOKENS,
            extra_body={"modalities": _IMAGE_MODALITIES},
        )
        return await self._call_with_retry(request, model)

    async def _record_usage(self, response: LLMResponse, model: str) -> None:
        """Catat pemakaian satu panggilan, termasuk biaya yang dilaporkan penyedia.

        Jumlah gambar dihitung dari respons, bukan dari niat pemanggil: model
        yang tidak mengembalikan gambar tidak boleh tercatat menghasilkan
        gambar.
        """
        await cost_tracker.record(
            response.model or model,
            in_tokens=response.prompt_tokens,
            out_tokens=response.completion_tokens,
            image_tokens=response.image_tokens,
            images=len(response.images),
            cost_usd=response.cost_usd,
        )

    async def _call_image_safely(
        self,
        prompt: str,
        aspect: str,
        model: str,
        *,
        owner: str,
        reserved: int,
    ) -> LLMResponse | None:
        """Panggil model gambar; bila gagal, jatah anggaran tidak ditinggal.

        Dua jenis kegagalan diperlakukan sama karena akibatnya sama:
        ilustrasi dilewati, jatah dikembalikan, dan job tetap berjalan.
        """
        try:
            return await self._call_image(prompt, aspect, model)
        except AppError as exc:
            logger.warning("Ilustrasi dilewati", model=model, reason=exc.code)
        except Exception as exc:  # ilustrasi tidak boleh mematikan job
            logger.warning("Ilustrasi dilewati", model=model, reason=str(exc)[:160])

        await ledger.release(owner, reserved)
        return None

    async def _reserve_image(self, owner: str, model: str) -> int | None:
        """Siapkan izin dan jatah untuk satu ilustrasi. `None` berarti lewati.

        Dua penjaga, dan keduanya perlu:

        1. **Batas harian per pelajar.** Token bukan pengendali biaya yang
           baik untuk gambar: satu ilustrasi hanya sekitar 4.175 token tetapi
           ditagih sekitar $0,007, jauh lebih mahal daripada token teks.
        2. **Jatah anggaran.** Jatah dikunci lebih dulu, lalu diganti dengan
           pemakaian nyata setelah respons datang.
        """
        batas = (
            settings.max_images_per_student_per_day
            if is_student_owner(owner)
            else None
        )
        if not await ledger.allow_image(owner, batas):
            logger.info(
                "Ilustrasi dilewati karena batas harian pelajar", model=model
            )
            return None

        if not await ledger.reserve(owner, _IMAGE_TOKEN_RESERVE):
            logger.info("Ilustrasi dilewati karena anggaran tidak cukup", model=model)
            return None

        return _IMAGE_TOKEN_RESERVE

    async def generate_image(
        self,
        *,
        prompt: str,
        owner: str,
        aspect: str = "16:9",
    ) -> bytes | None:
        """Buat satu ilustrasi.

        Kegagalan di sini tidak boleh menggagalkan job: ilustrasi adalah
        pelengkap, bukan isi. Semua galat dikembalikan sebagai `None`.

        Tidak ada model gambar gratis di OpenRouter (0 dari 17 model gratis
        menghasilkan gambar), jadi ilustrasi tidak bisa dialihkan ke rute
        gratis. Bila anggaran tidak cukup, ilustrasi dilewati.
        """
        model = settings.model_image
        if not model:
            return None

        reserved = await self._reserve_image(owner, model)
        if reserved is None:
            return None

        response = await self._call_image_safely(
            prompt, aspect, model, owner=owner, reserved=reserved
        )
        if response is None:
            return None

        await ledger.settle(
            owner, reserved, response.total_tokens, cost_usd=response.cost_usd
        )

        await self._record_usage(response, model)

        if not response.images:
            logger.warning("Model gambar tidak mengembalikan gambar", model=model)
            return None

        # Gambar bisa berukuran beberapa megabita; memvalidasi dan
        # mengodekannya ulang adalah pekerjaan CPU yang tidak boleh
        # menyumbat event loop.
        return await asyncio.to_thread(_image_bytes, response.images[0])

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


async def _send_chat_request(
    client: httpx.AsyncClient, payload: dict[str, Any]
) -> httpx.Response:
    """Kirim permintaan; galat jaringan diubah menjadi `_Retryable`."""
    try:
        return await client.post("/chat/completions", json=payload)
    except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
        raise _Retryable(f"timeout/putus: {exc}") from exc
    except httpx.HTTPError as exc:
        raise _Retryable(f"galat jaringan: {exc}") from exc


def _raise_for_status(response: httpx.Response) -> None:
    """Ubah status bukan-200 menjadi galat aplikasi yang tepat.

    Pembedaan penting: 401/402 berhenti tanpa mencoba ulang, sedangkan
    429/5xx dicoba ulang. 400/404/422 juga tidak dicoba ulang karena
    permintaan yang salah bentuk tidak akan berhasil walau diulang.
    """
    if response.status_code == 200:
        return

    body = response.text[:400]

    if response.status_code in (401, 403):
        raise LLMAuthError(
            "OpenRouter menolak kunci API. Periksa OPENROUTER_API_KEY."
        )
    if response.status_code == 402:
        raise LLMCreditsExhausted()
    if response.status_code in _RETRYABLE_STATUS:
        raise _Retryable(
            f"status {response.status_code}: {body}", response.status_code
        )
    raise LLMBadResponse(
        f"OpenRouter menolak permintaan ({response.status_code}).",
        details={"status": response.status_code, "body": body},
    )


def _parse_chat_response(response: httpx.Response) -> dict[str, Any]:
    try:
        return response.json()
    except ValueError as exc:
        raise _Retryable("respons bukan JSON") from exc


llm = LLMGateway()
