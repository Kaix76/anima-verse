"""Standalone Face Swap

Backend-unabhaengiges Face Swapping als Post-Processing Schritt.
Models werden lazy beim ersten Aufruf geladen (kein Startup-Overhead).

Unterstuetzte Modelle:
  - inswapper_128.onnx  (Standard, 128x128, via insightface model_zoo)
  - reswapper_256.onnx  (256x256, hoehere Qualitaet, direktes INSwapper-Loading)

Konfiguration via .env:
  FACE_SERVICE_MODEL_PATH=./models/inswapper_128.onnx   # oder reswapper_256.onnx
"""
import os
import threading
import time
from typing import Optional

# Singleton State - lazy-loaded
_face_app = None
_swapper = None
_initialized = False
_init_error: Optional[str] = None
_alignment_patched = False
# Lock fuer Init-Phase: parallele Swap-Requests koennen sonst zwischen
# "_initialized=True" und der tatsaechlichen Zuweisung von _face_app
# eintreten und eine NoneType-AttributeError werfen.
_init_lock = threading.Lock()


def _patch_face_alignment_for_256():
    """Monkey-Patch fuer insightface face_align bei nicht-128er Modellen.

    ReSwapper 256 braucht einen Offset-Korrektur bei der Gesichts-Ausrichtung,
    da insightface's estimate_norm nur fuer 112 und 128 korrekt ist.
    Formel aus somanchiu/ReSwapper: offset = (128/32768) * image_size - 0.5
    """
    global _alignment_patched
    if _alignment_patched:
        return

    try:
        import insightface.utils.face_align as face_align_module

        _original_estimate_norm = face_align_module.estimate_norm

        def patched_estimate_norm(lmk, image_size=112, mode='arcface'):
            M = _original_estimate_norm(lmk, image_size, mode)
            if image_size not in (112, 128):
                offset = (128 / 32768) * image_size - 0.5
                M[0, 2] += offset
                M[1, 2] += offset
            return M

        face_align_module.estimate_norm = patched_estimate_norm
        _alignment_patched = True
        print("[FaceSwap] Face-Alignment Patch fuer 256px aktiviert")
    except Exception as e:
        print(f"[FaceSwap] WARNUNG: Alignment-Patch fehlgeschlagen: {e}")


def _load_swapper_model(model_path: str):
    """Laedt das Swapper-Model - automatisch passend zum Typ.

    inswapper_128.onnx  → insightface.model_zoo.get_model() (Standard-Weg)
    reswapper_256.onnx  → Direktes INSwapper-Loading (umgeht ModelRouter)

    Der insightface ModelRouter erkennt nur 128x128 als INSwapper.
    Bei 256x256 wuerde er faelschlicherweise ArcFaceONNX instanziieren.
    """
    import onnxruntime

    basename = os.path.basename(model_path).lower()
    is_reswapper = "reswapper" in basename or "256" in basename

    if is_reswapper:
        # Direktes Loading: ModelRouter umgehen
        from insightface.model_zoo.inswapper import INSwapper

        session = onnxruntime.InferenceSession(
            model_path,
            providers=['CUDAExecutionProvider', 'CPUExecutionProvider']
        )
        swapper = INSwapper(model_file=model_path, session=session)

        # Alignment-Patch fuer 256px aktivieren
        _patch_face_alignment_for_256()

        input_shape = session.get_inputs()[0].shape
        print(f"[FaceSwap] ReSwapper geladen: input_size={input_shape[2]}x{input_shape[3]}")
        return swapper
    else:
        # Standard-Weg fuer inswapper_128
        import insightface
        return insightface.model_zoo.get_model(model_path)


def _ensure_initialized() -> bool:
    """Lazy-Load der insightface Models beim ersten Aufruf.

    Setzt OMP_NUM_THREADS VOR dem Import von onnxruntime,
    um pthread_setaffinity_np Fehler zu vermeiden.
    """
    global _face_app, _swapper, _initialized, _init_error

    # Fast-path: Init bereits abgeschlossen
    if _initialized:
        return _init_error is None

    # Lock serialisiert die Init-Phase. Wer als zweiter reinkommt, wartet
    # bis Thread 1 fertig ist und sieht dann das Ergebnis — kein Halb-State.
    with _init_lock:
        if _initialized:
            return _init_error is None

        # OMP_NUM_THREADS MUSS vor onnxruntime-Import gesetzt sein
        omp_threads = os.environ.get("FACE_SERVICE_OMP_NUM_THREADS", os.environ.get("FACESWAP_OMP_NUM_THREADS", "4"))
        os.environ.setdefault("OMP_NUM_THREADS", omp_threads)
        # Thread-Affinity deaktivieren — sonst spammt onnxruntime
        # "pthread_setaffinity_np failed: Invalid argument" wenn das OS
        # CPU-Pinning verbietet (Container, restricted cpuset).
        # OMP_PLACES NICHT setzen — leerer String wird von libgomp abgelehnt.
        os.environ.setdefault("OMP_PROC_BIND", "false")

        local_face_app = None
        local_swapper = None
        try:
            import onnxruntime
            onnxruntime.set_default_logger_severity(3)

            from insightface.app import FaceAnalysis

            # Face Analysis initialisieren
            det_size = int(os.environ.get("FACE_SERVICE_DET_SIZE", os.environ.get("FACESWAP_DET_SIZE", "640")))
            # det_size darf NICHT >= Bildgroesse sein (insightface Bug: findet dann keine Gesichter)
            if det_size > 640:
                print(f"[FaceSwap] WARNUNG: det_size={det_size} zu gross, setze auf 640")
                det_size = 640
            local_face_app = FaceAnalysis(name="buffalo_l")
            local_face_app.prepare(ctx_id=0, det_size=(det_size, det_size))

            # Swapper Model laden
            model_path = os.environ.get("FACE_SERVICE_MODEL_PATH", os.environ.get("FACESWAP_MODEL_PATH"))
            if not model_path or not os.path.exists(model_path):
                _init_error = f"Swapper Model nicht gefunden: {model_path}"
                print(f"[FaceSwap] FEHLER: {_init_error}")
                _initialized = True  # erst NACH dem Fehler markieren
                return False

            local_swapper = _load_swapper_model(model_path)

            # Erst jetzt — wenn alles geklappt hat — die Globals befuellen
            # und _initialized setzen. Dadurch kann ein anderer Thread (z.B.
            # ein paralleler /swap) niemals einen halben State sehen.
            _face_app = local_face_app
            _swapper = local_swapper
            _initialized = True
            print(f"[FaceSwap] Models geladen (det_size={det_size}, model={model_path})")
            return True

        except ImportError as e:
            _init_error = f"Fehlende Abhaengigkeit: {e}"
            print(f"[FaceSwap] FEHLER: {_init_error}")
            _initialized = True
            return False
        except Exception as e:
            _init_error = f"Initialisierung fehlgeschlagen: {e}"
            print(f"[FaceSwap] FEHLER: {_init_error}")
            _initialized = True
            return False


def reset():
    """Setzt den Singleton-State zurueck, damit Models beim naechsten Aufruf neu geladen werden."""
    global _face_app, _swapper, _initialized, _init_error, _alignment_patched
    _face_app = None
    _swapper = None
    _initialized = False
    _init_error = None
    _alignment_patched = False
    print("[FaceSwap] Reset: Models werden beim naechsten Aufruf neu geladen")


def apply_face_swap(
    target_image_bytes: bytes,
    source_image_bytes: bytes,
) -> Optional[bytes]:
    """Face Swap: Source-Gesicht auf Target-Bild uebertragen.

    Args:
        target_image_bytes: Generiertes Bild (PNG bytes), in das geswapt wird.
        source_image_bytes: Source-Gesicht (PNG bytes, z.B. Agent-Profilbild).

    Returns:
        PNG bytes des geswapted Bildes, oder None falls Swap nicht moeglich
        (kein Gesicht erkannt, Model nicht geladen, etc.).
    
    Env Vars:
        FACESWAP_DEBUG: Wenn gesetzt, speichert Debug-Bilder in /tmp/faceswap_debug/
    """
    if not _ensure_initialized():
        print(f"[FaceSwap] Uebersprungen: {_init_error}")
        return None

    t_start = time.time()
    debug_mode = os.environ.get("FACE_SERVICE_DEBUG", os.environ.get("FACESWAP_DEBUG", "")).lower() in ("1", "true", "yes")

    try:
        import cv2
        import numpy as np

        # Bilder aus Bytes dekodieren
        print(f"[FaceSwap] 📥 Dekodiere Bilder...")
        print(f"[FaceSwap]   Target: {len(target_image_bytes)} bytes")
        print(f"[FaceSwap]   Source: {len(source_image_bytes)} bytes")
        
        target_arr = np.frombuffer(target_image_bytes, dtype=np.uint8)
        target_img = cv2.imdecode(target_arr, cv2.IMREAD_COLOR)
        if target_img is None:
            print("[FaceSwap] ❌ FEHLER: Target-Bild konnte nicht dekodiert werden")
            return None
        print(f"[FaceSwap]   ✓ Target dekodiert: {target_img.shape} (H×W×C)")
        print(f"[FaceSwap]     Pixel-Range: [{target_img.min()}, {target_img.max()}], Mean: {target_img.mean():.1f}")

        source_arr = np.frombuffer(source_image_bytes, dtype=np.uint8)
        source_img = cv2.imdecode(source_arr, cv2.IMREAD_COLOR)
        if source_img is None:
            print("[FaceSwap] ❌ FEHLER: Source-Bild konnte nicht dekodiert werden")
            return None
        print(f"[FaceSwap]   ✓ Source dekodiert: {source_img.shape} (H×W×C)")
        print(f"[FaceSwap]     Pixel-Range: [{source_img.min()}, {source_img.max()}], Mean: {source_img.mean():.1f}")

        # Debug-Mode: Bilder speichern
        if debug_mode:
            debug_dir = "/tmp/faceswap_debug"
            os.makedirs(debug_dir, exist_ok=True)
            timestamp = int(time.time() * 1000)
            
            target_debug_path = f"{debug_dir}/target_{timestamp}.png"
            source_debug_path = f"{debug_dir}/source_{timestamp}.png"
            
            cv2.imwrite(target_debug_path, target_img)
            cv2.imwrite(source_debug_path, source_img)
            print(f"[FaceSwap] 🐛 DEBUG: Bilder gespeichert:")
            print(f"[FaceSwap]   Target: {target_debug_path}")
            print(f"[FaceSwap]   Source: {source_debug_path}")

        # Gesichtserkennung
        det_size = int(os.environ.get("FACE_SERVICE_DET_SIZE", os.environ.get("FACESWAP_DET_SIZE", "640")))
        print(f"\n[FaceSwap] 👤 Gesichtserkennung (det_size={det_size})...")
        
        print(f"[FaceSwap]   → Analysiere Source-Bild ({source_img.shape[1]}×{source_img.shape[0]})...")
        source_faces = _face_app.get(source_img)
        print(f"[FaceSwap]   {'✓' if source_faces else '❌'} Source Faces gefunden: {len(source_faces)}")
        
        if source_faces:
            for idx, face in enumerate(source_faces):
                bbox = face.bbox.astype(int)
                print(f"[FaceSwap]     Face {idx}: BBox=[{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}], Score={face.det_score:.3f}")
        else:
            print(f"[FaceSwap]   ⚠️ DIAGNOSE:")
            print(f"[FaceSwap]     - Bildgröße: {source_img.shape[1]}×{source_img.shape[0]}")
            print(f"[FaceSwap]     - Det-Size: {det_size}×{det_size}")
            print(f"[FaceSwap]     ❗ TIPPS:")
            print(f"[FaceSwap]       • Bild sollte mindestens ein gut erkennbares Gesicht zeigen")
            print(f"[FaceSwap]       • Gesicht sollte frontal und gut beleuchtet sein")
            print(f"[FaceSwap]       • Verwenden Sie ein ECHTES FOTO als Profilbild, kein KI-generiertes Bild")
            print(f"[FaceSwap]       • Falls Gesicht klein: Versuchen Sie FACE_SERVICE_DET_SIZE=320 oder 512")
            if debug_mode:
                print(f"[FaceSwap]       • Debug-Bild prüfen: {source_debug_path}")
            print(f"[FaceSwap]   ❌ ABBRUCH: Kein Gesicht im Source-Bild erkannt")
            return None

        print(f"[FaceSwap]   → Analysiere Target-Bild ({target_img.shape[1]}×{target_img.shape[0]})...")
        target_faces = _face_app.get(target_img)
        print(f"[FaceSwap]   {'✓' if target_faces else '❌'} Target Faces gefunden: {len(target_faces)}")
        
        if target_faces:
            for idx, face in enumerate(target_faces):
                bbox = face.bbox.astype(int)
                print(f"[FaceSwap]     Face {idx}: BBox=[{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}], Score={face.det_score:.3f}")
        else:
            print(f"[FaceSwap]   ❌ ABBRUCH: Kein Gesicht im Target-Bild erkannt")
            return None

        # Erstes erkanntes Gesicht swappen
        print(f"\n[FaceSwap] 🔄 Swapping Face 0...")
        print(f"[FaceSwap]   Source Face: BBox={source_faces[0].bbox.astype(int)}, Score={source_faces[0].det_score:.3f}")
        print(f"[FaceSwap]   Target Face: BBox={target_faces[0].bbox.astype(int)}, Score={target_faces[0].det_score:.3f}")
        result = _swapper.get(target_img, target_faces[0], source_faces[0], paste_back=True)
        print(f"[FaceSwap]   ✓ Swap abgeschlossen, Ergebnis: {result.shape}")

        # Debug-Mode: Ergebnis speichern
        if debug_mode:
            result_debug_path = f"{debug_dir}/result_{timestamp}.png"
            cv2.imwrite(result_debug_path, result)
            print(f"[FaceSwap] 🐛 DEBUG: Ergebnis gespeichert: {result_debug_path}")

        # Zurueck in PNG bytes kodieren
        success, encoded = cv2.imencode(".png", result)
        if not success:
            print("[FaceSwap] FEHLER: Ergebnis konnte nicht kodiert werden")
            return None

        result_bytes = encoded.tobytes()
        elapsed = time.time() - t_start
        print(f"[FaceSwap] ✓ Erfolg ({len(result_bytes)} bytes, {elapsed:.1f}s)")
        return result_bytes

    except Exception as e:
        elapsed = time.time() - t_start
        print(f"[FaceSwap] Fehler beim Face Swap ({elapsed:.1f}s): {e}")
        import traceback
        traceback.print_exc()
        return None


def apply_face_swap_files(
    target_path: str,
    source_path: str,
    output_path: Optional[str] = None,
) -> Optional[str]:
    """File-basierter Face Swap: Liest Target und Source von der Platte,
    schreibt das Ergebnis zurueck.

    Args:
        target_path: Pfad zum generierten Bild (in das geswapt wird).
        source_path: Pfad zum Source-Gesicht (z.B. Profilbild).
        output_path: Optionaler Ausgabepfad. Wenn None, wird target_path ueberschrieben.

    Returns:
        Pfad zur Ergebnisdatei, oder None falls Swap fehlgeschlagen.
    """
    if not os.path.exists(target_path):
        print(f"[FaceSwap] ❌ Target-Datei nicht gefunden: {target_path}")
        return None
    if not os.path.exists(source_path):
        print(f"[FaceSwap] ❌ Source-Datei nicht gefunden: {source_path}")
        return None

    print(f"[FaceSwap] 📂 File-basierter Swap:")
    print(f"[FaceSwap]   Target: {target_path}")
    print(f"[FaceSwap]   Source: {source_path}")

    target_bytes = open(target_path, "rb").read()
    source_bytes = open(source_path, "rb").read()

    result_bytes = apply_face_swap(target_bytes, source_bytes)
    if result_bytes is None:
        return None

    out = output_path or target_path
    with open(out, "wb") as f:
        f.write(result_bytes)
    print(f"[FaceSwap] ✓ Ergebnis gespeichert: {out} ({len(result_bytes)} bytes)")
    return out
