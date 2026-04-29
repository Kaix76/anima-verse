#!/usr/bin/env bash
# Docker entrypoint: face service im Hintergrund, main app im Vordergrund.
set -euo pipefail

# Face Service im Hintergrund starten
echo "[docker] Starting face service..."
python -m uvicorn face_service.server:app --host 0.0.0.0 --port "${FACE_SERVICE_PORT:-8005}" &

# Kurz warten damit Face Service bereit ist
sleep 1

# Main App im Vordergrund (haelt den Container am Leben)
echo "[docker] Starting main app..."
exec python -m uvicorn app.server:app --host 0.0.0.0 --port 8000
