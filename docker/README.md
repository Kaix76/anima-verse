# Docker Deployment

## Quick Start

```bash
# 1. Create configuration
cp .env.example .env

# Edit .env: Adjust URLs for Ollama, TTS, ComfyUI etc.
# (localhost -> IP/Hostname of the host machine)

# change to docker subfolder
cd docker

# 3. Build and start
docker compose up -d --build

# 4. Check logs
docker compose logs -f
```

The app is accessible at `http://localhost:8100` (docker-compose maps 8100:8000).

## Update

```
# pull from github
git pull origin main

# update container
cd docker
docker compose up -d --build
```


## Volumes

| Path in Container | Description |
|---|---|
| `/storage` | User data, character profiles, templates, stories |
| `/workflows` | comfyUI workflows |
| `/models` | models for face service |
| `/.env` | Configuration file |
| `/voices` | TTS voice reference files (for F5-TTS voice cloning) |
| `/logs` | Application log files |

## External Services

These services do NOT run inside the container and must be provided separately.
The URLs in `.env` must point to the external hosts (not `localhost`!).

| Service | Default Port | .env Variable |
|---|---|---|
| Ollama | 11434 | `PROVIDER_1_API_BASE` |
| Face Service | 8005 | `FACE_SERVICE_URL` |
| F5-TTS | 7860 | `TTS_F5_URL` |
| XTTS | 8020 | `TTS_XTTS_URL` |
| ComfyUI | 8188 | `SKILL_IMAGEGEN_3_API_URL` |
| SearX | 8888 | `SKILL_SEARX_URL` |
| Stable Diffusion | 7860 | `SKILL_IMAGEGEN_1_API_URL` |

## Commands

```bash
# Stop
docker compose down

# Rebuild after code changes
docker compose up -d --build

# Shell inside the container
docker compose exec app bash
```
