"""Face Processing Microservice

Standalone FastAPI server for face swap and face enhancement.
Runs independently from the main application on a separate port.

Endpoints:
  POST /swap     - Face swap (multipart: target + source images)
  POST /enhance  - Face enhancement (multipart: image)
  GET  /health   - Health check + model status
  POST /reset    - Reset/reload models

Configuration via .env:
  FACE_SERVICE_PORT=8005
"""
import os
import asyncio
from typing import Optional
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Load .env before anything else (models read env vars during init)
load_dotenv()

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import Response

from . import face_swap, face_enhance


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[FaceService] Starting up...")
    yield
    print("[FaceService] Shutting down...")


app = FastAPI(title="Face Processing Service", lifespan=lifespan)


@app.post("/swap")
async def swap_faces(
    target: UploadFile = File(..., description="Target image (face will be replaced)"),
    source: UploadFile = File(..., description="Source image (face to use)"),
):
    """Face swap: replace the face in target with the face from source."""
    target_bytes = await target.read()
    source_bytes = await source.read()

    if not target_bytes or not source_bytes:
        raise HTTPException(status_code=400, detail="Both target and source images required")

    result = await asyncio.to_thread(
        face_swap.apply_face_swap, target_bytes, source_bytes
    )

    if result is None:
        raise HTTPException(
            status_code=422,
            detail="Face swap failed (no face detected or model error)"
        )

    return Response(content=result, media_type="image/png")


@app.post("/enhance")
async def enhance_face(
    image: UploadFile = File(..., description="Image to enhance"),
):
    """Face enhancement: improve face quality in the image."""
    image_bytes = await image.read()

    if not image_bytes:
        raise HTTPException(status_code=400, detail="Image required")

    result = await asyncio.to_thread(
        face_enhance.apply_face_enhance, image_bytes
    )

    if result is None:
        raise HTTPException(
            status_code=422,
            detail="Face enhancement failed (no face detected or model error)"
        )

    return Response(content=result, media_type="image/png")


@app.get("/health")
async def health_check():
    """Health check with model status."""
    return {
        "status": "ok",
        "swap": {
            "initialized": face_swap._initialized,
            "error": face_swap._init_error,
        },
        "enhance": {
            "initialized": face_enhance._initialized,
            "error": face_enhance._init_error,
            "model_type": face_enhance._model_type,
        },
    }


@app.post("/reset")
async def reset_models():
    """Reset all models (will reload on next request)."""
    face_swap.reset()
    face_enhance.reset()
    return {"status": "ok", "message": "Models reset, will reload on next request"}


def main():
    """Entry point for running the service directly."""
    import uvicorn

    port = int(os.environ.get("FACE_SERVICE_PORT", "8005"))
    print(f"[FaceService] Starting on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
