#!/usr/bin/env bash
# Jalankan PATH-AI di Linux atau macOS.
#
# Ini kembaran start.ps1 untuk sistem yang tidak punya PowerShell. Dipakai di
# VPS: skrip PowerShell tidak bisa dijalankan di sana.
#
# Pemakaian:
#   ./start.sh              build lalu jalankan container (port 8001)
#   ./start.sh --lokal      jalankan dari virtualenv, tanpa Docker
#   ./start.sh --port 8002
#
# Pada mode container, Tesseract beserta data bahasa Indonesia sudah ikut di
# dalam image, jadi tidak ada yang perlu dipasang di server.

set -euo pipefail

cd "$(dirname "$0")"

MODE="docker"
PORT=8001

while [ $# -gt 0 ]; do
    case "$1" in
        --lokal|--local) MODE="lokal" ;;
        --port) PORT="$2"; shift ;;
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "Argumen tidak dikenal: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ ! -f .env ]; then
    echo "Berkas .env belum ada. Menyalin dari .env.example..." >&2
    cp .env.example .env
    echo "Isi nilainya dulu (terutama OPENROUTER_API_KEY dan PATH_AI_API_KEYS), lalu jalankan ulang." >&2
    exit 1
fi

if [ "$MODE" = "lokal" ]; then
    PYTHON=".venv/bin/python"

    if [ ! -x "$PYTHON" ]; then
        echo "Virtualenv belum ada. Membuat dan memasang dependensi..." >&2
        python3 -m venv .venv
        "$PYTHON" -m pip install --upgrade pip --quiet
        "$PYTHON" -m pip install -r requirements.txt
    fi

    # Keadaan OCR dilaporkan lebih dulu, supaya berkas hasil pindai yang tidak
    # terbaca tidak baru ketahuan saat guru mengunggahnya.
    PYTHONPATH="$PWD" "$PYTHON" -m app.ingest.ocr || true

    echo "PATH-AI berjalan lokal di http://127.0.0.1:$PORT (workers 1)"
    PYTHONPATH="$PWD" exec "$PYTHON" -m uvicorn app.main:app \
        --host 127.0.0.1 --port "$PORT" --workers 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker tidak ditemukan. Pasang Docker lebih dulu, atau jalankan ./start.sh --lokal." >&2
    exit 1
fi

echo "Membangun dan menjalankan container path-ai-mbkm..."
docker compose up -d --build

echo
echo "Container berjalan. Perintah berguna:"
echo "  docker compose logs -f"
echo "  docker compose ps"
echo "  ./stop.sh"
echo
echo "Layanan:"
echo "  http://localhost:$PORT/health"
echo "  http://localhost:$PORT/docs"
echo
echo "Keadaan OCR tercatat di log saat layanan menyala:"
echo "  docker compose logs | grep -i ocr"
