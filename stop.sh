#!/usr/bin/env bash
# Hentikan PATH-AI. Kembaran stop.ps1 untuk Linux dan macOS.
#
# Pemakaian:
#   ./stop.sh
#   ./stop.sh --hapus     sekaligus buang image lokalnya

set -euo pipefail

cd "$(dirname "$0")"

if [ "${1:-}" = "--hapus" ] || [ "${1:-}" = "--remove" ]; then
    docker compose down --rmi local --volumes
else
    docker compose down
fi

echo "PATH-AI dihentikan."
