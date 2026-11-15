"""Endpoint operasional.

Dipakai administrator untuk melihat apakah anggaran masih sehat dan seberapa
sering layanan turun ke model gratis. Ini penting karena peralihan ke model
gratis bersifat senyap bagi pengajar — tanpa angka ini, tidak ada yang tahu
kualitas menurun sampai ada yang mengeluh.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from app.api.deps import CallerDep
from app.core.config import settings
from app.ingest.ocr import info_ocr
from app.observability.cost_tracker import cost_tracker
from app.observability.free_quota import free_quota
from app.observability.ledger import ledger

router = APIRouter(prefix="/v1/ops", tags=["ops"])


@router.get("/usage", summary="Pemakaian token, biaya, dan sisa anggaran")
async def usage(caller: CallerDep) -> dict[str, object]:
    budget = await ledger.status(caller.fingerprint)
    costs = await cost_tracker.snapshot()
    quota = await free_quota.snapshot()
    # Pemeriksaannya sudah dilakukan saat menyala dan hasilnya diingat, jadi
    # baris ini tidak memanggil tesseract lagi.
    ocr = await asyncio.to_thread(info_ocr)

    return {
        # Berguna saat menelusuri "kenapa anggaran saya kosong": anggaran
        # menempel pada pengajar, bukan pada kunci. Tanpa `X-Teacher-Id`,
        # seluruh pemakaian menempel pada kuncinya sendiri.
        # `actor` sudah disamarkan (`b***@sekolah.id`). Email mentahnya tidak
        # pernah dikembalikan, karena badan respons ini bisa tersimpan di log
        # reverse proxy atau tunnel.
        "auth": {
            "role": caller.role,
            "actor": caller.actor,
            "owner": caller.fingerprint,
        },
        "budget": budget.to_dict(),
        "free_quota": quota,
        "models": costs["models"],
        "total_usd": costs["total_usd"],
        "prices_available": costs["prices_available"],
        "degraded_jobs": costs["degraded_jobs"],
        "config": {
            "model_paid": settings.model_paid,
            "model_free": settings.model_free,
            "model_image": settings.model_image,
            # Ditampilkan karena sering ditanyakan saat berkas hasil pindai
            # gagal dibaca: pembaca gambar mana yang dipakai, dan apakah mesin
            # OCR-nya benar-benar siap.
            "model_vision": settings.vision_model or "mati",
            "urutan_pembaca_gambar": settings.image_reader_order,
            "ocr": ocr.ringkas(),
            "ocr_siap": ocr.siap,
            "budget_tokens_per_day": settings.budget_tokens_per_day,
            "max_tokens_per_student": settings.max_tokens_per_student,
            "labels": {
                "sangat_baik": f">={settings.label_sangat_baik_min:g}",
                "baik": f">={settings.label_baik_min:g}",
                "cukup": f">={settings.label_cukup_min:g}",
                "perlu_bimbingan": f"<{settings.label_cukup_min:g}",
            },
        },
    }