# ── Stage 1: Builder ─────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ cmake build-essential \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Dependencies zuerst (fuer Layer-Cache)
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    "openai>=1.0.0" \
    "sse-starlette>=1.6.1" \
    "fastapi>=0.95.0" \
    "uvicorn>=0.22.0" \
    python-dotenv \
    "jinja2>=3.1.2" \
    python-multipart \
    "bcrypt>=4.0.0" \
    "pyjwt>=2.8.0" \
    apscheduler \
    opencv-python-headless \
    insightface \
    onnxruntime \
    aiohttp \
    gradio_client

# ── Stage 2: Runtime ────────────────────────────────────────────────────────
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python-Packages aus Builder kopieren
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# App-Code
COPY app/ ./app/
COPY face_service/ ./face_service/
COPY static/ ./static/
COPY templates/ ./templates/
COPY start.sh ./start.sh
COPY docker/docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./start.sh ./docker-entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["./docker-entrypoint.sh"]
