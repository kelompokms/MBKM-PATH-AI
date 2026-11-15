"""Fixture bersama untuk test.

Environment diset sebelum modul aplikasi diimpor, karena `Settings` membaca
environment saat modul dimuat. Ini juga mengisolasi test dari berkas `.env`
developer: kunci dan model yang dipakai selalu nilai uji.
"""

from __future__ import annotations

import os

os.environ["PATH_AI_API_KEYS"] = "test-key"
os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-test"
os.environ["PATH_AI_MODEL"] = "google/gemma-4-26b-a4b-it"
os.environ["PATH_AI_MODEL_FREE"] = "google/gemma-4-26b-a4b-it:free"
os.environ["PATH_AI_MODEL_IMAGE"] = ""
os.environ["PATH_AI_LOG_LEVEL"] = "WARNING"

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from app.core.config import settings
from app.jobs import job_runner, job_store
from app.main import app

API_KEY = "test-key"
HEADERS = {"X-API-Key": API_KEY}


@pytest.fixture(autouse=True)
def _isolasi_job_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Setiap test memakai direktori job sendiri dan tidak saling menumpuk."""
    monkeypatch.setattr(settings, "job_dir", str(tmp_path / "jobs"))


@pytest.fixture(autouse=True)
def _model_gambar_aktif(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aktifkan jalur ilustrasi.

    Tanpa ini, `generate_image` tidak pernah dipanggil dan test yang
    seharusnya memeriksa perilaku ilustrasi akan lulus secara palsu.
    """
    monkeypatch.setattr(settings, "model_image", "recraft/recraft-v4.1-flash-v2")


@pytest.fixture(autouse=True)
async def _bersihkan_state() -> AsyncIterator[None]:
    yield
    await job_runner.shutdown()
    await job_store.purge_all()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as instance:
        yield instance


async def tunggu_job(
    client: httpx.AsyncClient, job_id: str, *, batas_detik: float = 10.0
) -> dict:
    """Pantau job sampai status terminal."""
    langkah = 0
    while langkah < int(batas_detik / 0.02):
        response = await client.get(f"/v1/jobs/{job_id}", headers=HEADERS)
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in {"done", "failed", "needs_review"}:
            return payload
        await asyncio.sleep(0.02)
        langkah += 1
    raise AssertionError(f"Job {job_id} tidak selesai dalam {batas_detik} detik")