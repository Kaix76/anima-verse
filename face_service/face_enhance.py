"""Standalone Face Enhancement

Backend-unabhaengiges Face Enhancement als Post-Processing Schritt.
Verbessert Gesichtsqualitaet nach FaceSwap (entfernt Artefakte, schaerft Details).
Models werden lazy beim ersten Aufruf geladen (kein Startup-Overhead).

Unterstuetzte Modelle (alle 512x512, ONNX):
  - GFPGANv1.4.onnx     (348 MB, schnell, gute Identitaetstreue)
  - codeformer.onnx      (377 MB, Fidelity-Weight steuerbar, beste Balance)
  - GPEN-BFR-512.onnx    (284 MB, beste Gesamtqualitaet, nahtlose Uebergaenge)

Pipeline pro Gesicht:
  1. Gesicht erkennen (insightface FaceAnalysis, wird von face_swap.py geteilt)
  2. Gesicht auf FFHQ 512x512 Template ausrichten (Affine Transform)
  3. Restoration (GFPGAN / CodeFormer / GPEN)
  4. Color Correction (Lab-Farbraum-Transfer, optional)
  5. Sharpening (Unsharp Mask, optional)
  6. Ergebnis zurueck ins Originalbild einsetzen (Inverse Affine + Soft Blending)

Konfiguration via .env:
  FACE_ENHANCE_ENABLED=true
  FACE_ENHANCE_MODEL_PATH=./models/GFPGANv1.4.onnx
  FACE_ENHANCE_BLEND=1.0             # 0.0=Original, 1.0=voll enhanced
  FACE_ENHANCE_CODEFORMER_WEIGHT=0.7 # Nur fuer CodeFormer: 0.0=max Qualitaet, 1.0=max Identitaet
  FACE_ENHANCE_COLOR_CORRECTION=true  # Lab-Farbraum-Angleichung an Umgebung
  FACE_ENHANCE_SHARPEN=true           # Unsharp-Mask Nachschaerfung
  FACE_ENHANCE_SHARPEN_STRENGTH=0.5   # Schaerfe-Staerke (0.1-2.0, empfohlen 0.3-0.8)
"""
import os
import time
from typing import Optional

import numpy as np

# Singleton State - lazy-loaded
_enhancer_session = None
_model_type: Optional[str] = None  # "gfpgan", "codeformer", "gpen"
_initialized = False
_init_error: Optional[str] = None

# FFHQ 512 Template: 5-Punkt Landmarks fuer Gesichtsausrichtung
# (left eye, right eye, nose, left mouth, right mouth)
FFHQ_512_TEMPLATE = np.array([
    [192.98138, 239.94708],
    [318.90277, 240.19360],
    [256.63416, 314.01935],
    [201.26117, 371.41043],
    [313.08905, 371.15118],
], dtype=np.float32)

MODEL_SIZE = (512, 512)


def _detect_model_type(model_path: str) -> str:
    """Erkennt den Model-Typ anhand des Dateinamens."""
    basename = os.path.basename(model_path).lower()
    if "codeformer" in basename:
        return "codeformer"
    elif "gpen" in basename:
        return "gpen"
    return "gfpgan"


def _ensure_initialized() -> bool:
    """Lazy-Load des Enhancement ONNX Models beim ersten Aufruf."""
    global _enhancer_session, _model_type, _initialized, _init_error

    if _initialized:
        return _init_error is None

    _initialized = True

    omp_threads = os.environ.get("FACE_SERVICE_OMP_NUM_THREADS", os.environ.get("FACESWAP_OMP_NUM_THREADS", "4"))
    os.environ.setdefault("OMP_NUM_THREADS", omp_threads)

    try:
        import onnxruntime

        model_path = os.environ.get("FACE_ENHANCE_MODEL_PATH")
        if not model_path or not os.path.exists(model_path):
            _init_error = f"Enhancement Model nicht gefunden: {model_path}"
            print(f"[FaceEnhance] FEHLER: {_init_error}")
            return False

        _enhancer_session = onnxruntime.InferenceSession(
            model_path,
            providers=['CUDAExecutionProvider', 'CPUExecutionProvider']
        )
        _model_type = _detect_model_type(model_path)

        inputs = _enhancer_session.get_inputs()
        input_info = ", ".join(f"{inp.name} {inp.shape}" for inp in inputs)
        print(f"[FaceEnhance] Model geladen: {model_path}")
        print(f"[FaceEnhance]   Typ: {_model_type}")
        print(f"[FaceEnhance]   Inputs: {input_info}")

        # Config loggen
        cc = os.environ.get("FACE_ENHANCE_COLOR_CORRECTION", "true").lower() in ("true", "1", "yes")
        sh = os.environ.get("FACE_ENHANCE_SHARPEN", "true").lower() in ("true", "1", "yes")
        sh_s = os.environ.get("FACE_ENHANCE_SHARPEN_STRENGTH", "0.5")
        blend = os.environ.get("FACE_ENHANCE_BLEND", "1.0")
        print(f"[FaceEnhance]   ColorCorrection={cc}, Sharpen={sh} (strength={sh_s}), Blend={blend}")
        if _model_type == "codeformer":
            w = os.environ.get("FACE_ENHANCE_CODEFORMER_WEIGHT", "0.7")
            print(f"[FaceEnhance]   CodeFormer Fidelity-Weight={w}")

        return True

    except ImportError as e:
        _init_error = f"Fehlende Abhaengigkeit: {e}"
        print(f"[FaceEnhance] FEHLER: {_init_error}")
        return False
    except Exception as e:
        _init_error = f"Initialisierung fehlgeschlagen: {e}"
        print(f"[FaceEnhance] FEHLER: {_init_error}")
        return False


def reset():
    """Setzt den Singleton-State zurueck."""
    global _enhancer_session, _model_type, _initialized, _init_error
    _enhancer_session = None
    _model_type = None
    _initialized = False
    _init_error = None
    print("[FaceEnhance] Reset: Model wird beim naechsten Aufruf neu geladen")


# ── Face Alignment ──────────────────────────────────────────────

def _warp_face(image: np.ndarray, kps: np.ndarray) -> tuple:
    """Richtet ein Gesicht auf das FFHQ 512x512 Template aus.

    Returns:
        (crop_face, affine_matrix) - Ausgerichtetes Gesicht und Transformationsmatrix
    """
    import cv2
    affine_matrix = cv2.estimateAffinePartial2D(
        kps.astype(np.float32), FFHQ_512_TEMPLATE, method=cv2.LMEDS
    )[0]
    crop_face = cv2.warpAffine(
        image, affine_matrix, MODEL_SIZE,
        borderMode=cv2.BORDER_REPLICATE
    )
    return crop_face, affine_matrix


# ── Preprocessing / Postprocessing ──────────────────────────────

def _preprocess(crop_face: np.ndarray) -> np.ndarray:
    """BGR uint8 (512,512,3) -> NCHW float32 [-1,1] RGB."""
    img = crop_face[:, :, ::-1].astype(np.float32) / 255.0  # BGR->RGB, [0,1]
    img = (img - 0.5) / 0.5                                  # [-1,1]
    return np.expand_dims(img.transpose(2, 0, 1), axis=0).astype(np.float32)


def _postprocess(output: np.ndarray) -> np.ndarray:
    """NCHW float32 -> BGR uint8 (512,512,3)."""
    img = np.clip(output[0], -1, 1)
    img = (img + 1) / 2                          # [0,1]
    img = img.transpose(1, 2, 0)                  # HWC
    img = (img * 255.0).round().astype(np.uint8)
    return img[:, :, ::-1]                        # RGB->BGR


def _run_restoration(crop_face: np.ndarray) -> np.ndarray:
    """Fuehrt die ONNX-Inference durch — unterstuetzt GFPGAN, CodeFormer, GPEN."""
    tensor = _preprocess(crop_face)
    inputs = _enhancer_session.get_inputs()
    input_name = inputs[0].name

    if _model_type == "codeformer" and len(inputs) >= 2:
        # CodeFormer hat zusaetzlichen Fidelity-Weight Input 'w'
        w_value = float(os.environ.get("FACE_ENHANCE_CODEFORMER_WEIGHT", "0.7"))
        w_name = inputs[1].name
        feed = {input_name: tensor, w_name: np.array([w_value], dtype=np.float64)}
    else:
        # GFPGAN und GPEN: nur ein Input
        feed = {input_name: tensor}

    output = _enhancer_session.run(None, feed)[0]
    return _postprocess(output)


# ── Color Correction ────────────────────────────────────────────

def _color_correct_lab(enhanced_face: np.ndarray, original_face: np.ndarray) -> np.ndarray:
    """Lab-Farbraum-Transfer: Gleicht Farben des enhanced Gesichts an das Original an.

    Uebertraegt die Farbverteilung (Mittelwert + Standardabweichung) vom Original
    auf das enhanced Gesicht im Lab-Farbraum. Nur a/b-Kanaele (Farbe) werden
    angeglichen, L-Kanal (Helligkeit) bleibt vom Enhancement.
    """
    import cv2

    enhanced_lab = cv2.cvtColor(enhanced_face, cv2.COLOR_BGR2LAB).astype(np.float32)
    original_lab = cv2.cvtColor(original_face, cv2.COLOR_BGR2LAB).astype(np.float32)

    # Nur a/b Kanaele (Index 1, 2) angleichen — L (Helligkeit) behalten
    for ch in (1, 2):
        src_mean = enhanced_lab[:, :, ch].mean()
        src_std = enhanced_lab[:, :, ch].std()
        tgt_mean = original_lab[:, :, ch].mean()
        tgt_std = original_lab[:, :, ch].std()

        if src_std > 0:
            enhanced_lab[:, :, ch] = (
                (enhanced_lab[:, :, ch] - src_mean) * (tgt_std / src_std) + tgt_mean
            )

    return cv2.cvtColor(np.clip(enhanced_lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)


# ── Sharpening ──────────────────────────────────────────────────

def _unsharp_mask(image: np.ndarray, sigma: float = 1.0, strength: float = 0.5) -> np.ndarray:
    """Unsharp Mask: Schaerft Feindetails die Restoration-Modelle glaetten.

    Args:
        image: BGR uint8 Bild
        sigma: Gaussscher Blur-Radius (groesser = groebere Details schaerfen)
        strength: Schaerfe-Staerke (0.1=kaum, 0.5=moderat, 1.0=stark, 2.0=sehr stark)
    """
    import cv2
    blurred = cv2.GaussianBlur(image, (0, 0), sigma)
    sharpened = cv2.addWeighted(image, 1.0 + strength, blurred, -strength, 0)
    return np.clip(sharpened, 0, 255).astype(np.uint8)


# ── Paste-Back ──────────────────────────────────────────────────

def _create_blend_mask(size: tuple, blur: float = 0.3) -> np.ndarray:
    """Erzeugt eine weiche Maske fuer nahtloses Zuruecksetzen."""
    import cv2
    h, w = size
    mask = np.ones((h, w), dtype=np.float32)
    blur_px = int(h * 0.5 * blur)
    edge = max(blur_px // 2, 1)
    mask[:edge, :] = 0
    mask[-edge:, :] = 0
    mask[:, :edge] = 0
    mask[:, -edge:] = 0
    if blur_px > 0:
        mask = cv2.GaussianBlur(mask, (0, 0), blur_px * 0.25)
    return mask


def _paste_back(frame: np.ndarray, crop_face: np.ndarray,
                affine_matrix: np.ndarray, blur: float = 0.3) -> np.ndarray:
    """Setzt das enhanced Gesicht zurueck ins Originalbild mit Soft Blending."""
    import cv2
    inv_matrix = cv2.invertAffineTransform(affine_matrix)
    frame_wh = frame.shape[:2][::-1]  # (width, height)

    inv_crop = cv2.warpAffine(
        crop_face, inv_matrix, frame_wh,
        borderMode=cv2.BORDER_REPLICATE
    )
    mask = _create_blend_mask(crop_face.shape[:2], blur)
    inv_mask = cv2.warpAffine(mask, inv_matrix, frame_wh).clip(0, 1)

    result = frame.copy().astype(np.float32)
    inv_mask_3d = inv_mask[:, :, np.newaxis]
    result = inv_mask_3d * inv_crop.astype(np.float32) + (1 - inv_mask_3d) * result
    return result.astype(np.uint8)


# ── Haupt-Pipeline ──────────────────────────────────────────────

def apply_face_enhance(
    image_bytes: bytes,
    face_app=None,
) -> Optional[bytes]:
    """Face Enhancement: Verbessert alle erkannten Gesichter im Bild.

    Pipeline pro Gesicht:
      1. Alignment (FFHQ 512x512)
      2. Restoration (GFPGAN / CodeFormer / GPEN)
      3. Color Correction (Lab, optional)
      4. Sharpening (Unsharp Mask, optional)
      5. Paste-Back (Soft Blending)

    Args:
        image_bytes: Eingabebild (PNG bytes).
        face_app: Optionale insightface FaceAnalysis-Instanz.

    Returns:
        PNG bytes des enhanced Bildes, oder None bei Fehler.
    """
    if not _ensure_initialized():
        print(f"[FaceEnhance] Uebersprungen: {_init_error}")
        return None

    t_start = time.time()

    # Config auslesen
    blend = float(os.environ.get("FACE_ENHANCE_BLEND", "1.0"))
    use_color_correction = os.environ.get(
        "FACE_ENHANCE_COLOR_CORRECTION", "true"
    ).lower() in ("true", "1", "yes")
    use_sharpen = os.environ.get(
        "FACE_ENHANCE_SHARPEN", "true"
    ).lower() in ("true", "1", "yes")
    sharpen_strength = float(os.environ.get("FACE_ENHANCE_SHARPEN_STRENGTH", "0.5"))

    try:
        import cv2

        # Bild dekodieren
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if image is None:
            print("[FaceEnhance] FEHLER: Bild konnte nicht dekodiert werden")
            return None

        print(f"[FaceEnhance] Bild: {image.shape[1]}x{image.shape[0]}, "
              f"model={_model_type}, blend={blend}")
        print(f"[FaceEnhance]   ColorCorrection={use_color_correction}, "
              f"Sharpen={use_sharpen} (strength={sharpen_strength})")

        # FaceAnalysis: Eigene oder geteilte Instanz
        if face_app is None:
            from . import face_swap
            if not face_swap._ensure_initialized():
                print("[FaceEnhance] FEHLER: FaceAnalysis konnte nicht geladen werden")
                return None
            face_app = face_swap._face_app

        faces = face_app.get(image)
        if not faces:
            print("[FaceEnhance] Kein Gesicht erkannt - Enhancement uebersprungen")
            return None

        print(f"[FaceEnhance] {len(faces)} Gesicht(er) erkannt")

        result = image.copy()

        for idx, face in enumerate(faces):
            kps = face.kps
            bbox = face.bbox.astype(int)
            print(f"[FaceEnhance]   Face {idx}: BBox=[{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}], "
                  f"Score={face.det_score:.3f}")

            # 1. Gesicht ausrichten
            crop_face, affine_matrix = _warp_face(result, kps)
            original_crop = crop_face.copy()  # Fuer Color Correction

            # 2. Restoration (GFPGAN / CodeFormer / GPEN)
            enhanced_face = _run_restoration(crop_face)
            print(f"[FaceEnhance]     Restoration ({_model_type}) done")

            # 3. Color Correction (Lab-Transfer)
            if use_color_correction:
                enhanced_face = _color_correct_lab(enhanced_face, original_crop)
                print(f"[FaceEnhance]     Color Correction done")

            # 4. Sharpening
            if use_sharpen and sharpen_strength > 0:
                enhanced_face = _unsharp_mask(enhanced_face, sigma=1.0, strength=sharpen_strength)
                print(f"[FaceEnhance]     Sharpening done (strength={sharpen_strength})")

            # 5. Paste-Back
            result = _paste_back(result, enhanced_face, affine_matrix)
            print(f"[FaceEnhance]   Face {idx}: Enhanced")

        # Blend mit Original
        if blend < 1.0:
            result = cv2.addWeighted(image, 1.0 - blend, result, blend, 0)
            print(f"[FaceEnhance]   Blend: {blend:.0%} enhanced + {1-blend:.0%} original")

        # PNG kodieren
        success, encoded = cv2.imencode(".png", result)
        if not success:
            print("[FaceEnhance] FEHLER: Ergebnis konnte nicht kodiert werden")
            return None

        result_bytes = encoded.tobytes()
        elapsed = time.time() - t_start
        print(f"[FaceEnhance] Erfolg: {len(faces)} Gesicht(er) enhanced "
              f"({len(result_bytes)} bytes, {elapsed:.1f}s)")
        return result_bytes

    except Exception as e:
        elapsed = time.time() - t_start
        print(f"[FaceEnhance] Fehler ({elapsed:.1f}s): {e}")
        import traceback
        traceback.print_exc()
        return None


def apply_face_enhance_files(
    image_path: str,
    output_path: Optional[str] = None,
) -> Optional[str]:
    """File-basiertes Face Enhancement.

    Args:
        image_path: Pfad zum Eingabebild.
        output_path: Optionaler Ausgabepfad. Wenn None, wird image_path ueberschrieben.

    Returns:
        Pfad zur Ergebnisdatei, oder None bei Fehler.
    """
    if not os.path.exists(image_path):
        print(f"[FaceEnhance] Datei nicht gefunden: {image_path}")
        return None

    print(f"[FaceEnhance] File-basiert: {image_path}")

    image_bytes = open(image_path, "rb").read()
    result_bytes = apply_face_enhance(image_bytes)
    if result_bytes is None:
        return None

    out = output_path or image_path
    with open(out, "wb") as f:
        f.write(result_bytes)
    print(f"[FaceEnhance] Ergebnis gespeichert: {out} ({len(result_bytes)} bytes)")
    return out
