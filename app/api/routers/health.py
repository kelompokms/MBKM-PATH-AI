"""Endpoint kesehatan layanan.

Tidak memerlukan kunci API: dipakai oleh orkestrator container dan pemantau
uptime, yang tidak seharusnya memegang kunci akses pengguna.

Sengaja tidak memanggil OpenRouter. Pemeriksaan dependensi luar akan membuat
/probe gagal ketika penyedia model sedang lambat, padahal aplikasi sendiri
sehat — dan itu memicu restart container yang tidak perlu.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.jobs import job_store

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    service: str
    active_jobs: int


@router.get("/health", response_model=HealthResponse, summary="Status layanan")
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="path-ai",
        active_jobs=await job_store.count_active(),
    )