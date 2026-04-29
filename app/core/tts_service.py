"""TTS Service - XTTS v2, F5-TTS, and Magpie backends.

Provides text-to-speech generation with three configurable backends:
- XTTS v2: Voice cloning capable, higher quality, slower
- F5-TTS: High-quality voice cloning via Gradio API
- Magpie: Multilingual TTS via REST API (multipart form)
"""
import asyncio
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.core.log import get_logger
logger = get_logger("tts")

from app.core.paths import get_storage_dir as _get_storage_dir
_REF_AUDIO_CACHE: Dict[str, str] = {}


def _resolve_comfyui_candidates(skill_names: str) -> List[Tuple[str, str, int]]:
    """Resolve TTS_COMFYUI_SKILL to a list of (name, url, vram_required_mb).

    skill_names: Kommaseparierte SKILL_IMAGEGEN Backend-Namen (z.B. "ComfyUI-4070,ComfyUI-3090").
    Reihenfolge = Fallback-Reihenfolge. Nicht-ComfyUI-Backends werden uebersprungen.
    """
    targets = [s.strip() for s in skill_names.split(",") if s.strip()]
    if not targets:
        return []

    found: Dict[str, Tuple[str, str, int]] = {}
    i = 1
    while True:
        name = os.environ.get(f"SKILL_IMAGEGEN_{i}_NAME", "")
        if not name:
            break
        name = name.strip()
        if name in targets:
            api_type = os.environ.get(f"SKILL_IMAGEGEN_{i}_API_TYPE", "").strip().lower()
            if api_type != "comfyui":
                logger.warning("TTS_COMFYUI_SKILL '%s': Backend ist kein ComfyUI (api_type=%s)", name, api_type)
            else:
                url = os.environ.get(f"SKILL_IMAGEGEN_{i}_API_URL", "").strip().rstrip("/")
                vram_str = os.environ.get(f"SKILL_IMAGEGEN_{i}_VRAM_REQUIRED", "").strip()
                vram_mb = int(float(vram_str) * 1024) if vram_str else 0
                found[name] = (name, url, vram_mb)
        i += 1

    # Reihenfolge aus skill_names beibehalten
    candidates = [found[n] for n in targets if n in found]
    missing = [n for n in targets if n not in found]
    if missing:
        logger.warning("TTS_COMFYUI_SKILL: Kein SKILL_IMAGEGEN Backend gefunden fuer: %s", ",".join(missing))
    if candidates:
        logger.info("TTS ComfyUI Candidates: %s", ", ".join(f"{n}={u}" for n, u, _ in candidates))
    return candidates


# Tracker: which voice_description was last used to generate a voice per character
def _voice_desc_cache_file() -> Path:
    return _get_storage_dir() / "tmp" / "tts_voice_cache.json"
_voice_desc_cache: Dict[str, str] = {}  # {character_name: md5_of_voice_description}
_voice_desc_cache_loaded = False


def _load_voice_desc_cache() -> Dict[str, str]:
    """Loads the voice description cache from disk."""
    global _voice_desc_cache, _voice_desc_cache_loaded
    if _voice_desc_cache_loaded:
        return _voice_desc_cache
    if _voice_desc_cache_file().exists():
        try:
            _voice_desc_cache = json.loads(_voice_desc_cache_file().read_text(encoding="utf-8"))
        except Exception:
            _voice_desc_cache = {}
    _voice_desc_cache_loaded = True
    return _voice_desc_cache


def _save_voice_desc_cache(character_name: str, desc_hash: str):
    """Saves the voice description hash for a character."""
    global _voice_desc_cache
    _voice_desc_cache[character_name] = desc_hash
    _voice_desc_cache_file().parent.mkdir(parents=True, exist_ok=True)
    _voice_desc_cache_file().write_text(json.dumps(_voice_desc_cache, ensure_ascii=False), encoding="utf-8")


def _hash_voice_desc(desc: str) -> str:
    """Returns MD5 hash of a voice description string."""
    import hashlib
    return hashlib.md5(desc.strip().encode("utf-8")).hexdigest()


# Reference audio specs matching XTTS/F5-TTS expectations
_REF_TARGET_RATE = 24000
_REF_MAX_DURATION = 10  # seconds


def _normalize_reference_audio(audio_path: str) -> str:
    """Normalizes reference audio to 24kHz mono WAV, max 10s.

    Non-WAV files (MP3, FLAC, OGG) or WAV files with wrong sample rate
    are converted on first use. Result is cached in voices/ directory.
    Returns the path to use (original if already OK, normalized otherwise).
    """
    if audio_path in _REF_AUDIO_CACHE:
        return _REF_AUDIO_CACHE[audio_path]

    p = Path(audio_path)
    if not p.exists():
        return audio_path

    needs_convert = False
    reason = ""

    if p.suffix.lower() != ".wav":
        needs_convert = True
        reason = f"non-WAV format ({p.suffix})"
    else:
        # Check WAV properties
        try:
            with open(p, "rb") as wf:
                wf.read(4)  # RIFF
                wf.read(4)  # size
                wf.read(4)  # WAVE
                wf.read(4)  # fmt
                wf.read(4)  # chunk size
                fmt = struct.unpack("<HHIIHH", wf.read(16))
                channels, sample_rate = fmt[1], fmt[2]
                bps = fmt[5]
                file_size = p.stat().st_size
                duration = (file_size - 44) / (sample_rate * channels * bps // 8)
                if sample_rate != _REF_TARGET_RATE:
                    needs_convert = True
                    reason = f"sample rate {sample_rate}Hz"
                elif channels != 1:
                    needs_convert = True
                    reason = f"{channels} channels"
                elif duration > _REF_MAX_DURATION + 2:
                    needs_convert = True
                    reason = f"too long ({duration:.1f}s)"
        except Exception:
            pass

    if not needs_convert:
        _REF_AUDIO_CACHE[audio_path] = audio_path
        return audio_path

    # Build normalized path: same dir, same stem, _norm.wav
    norm_path = p.parent / f"{p.stem}_norm.wav"

    if norm_path.exists():
        logger.debug("Using cached normalized ref: %s", norm_path)
        _REF_AUDIO_CACHE[audio_path] = str(norm_path)
        return str(norm_path)

    # Convert with ffmpeg (preferred) or soundfile (fallback)
    converted = False
    try:
        cmd = [
            "ffmpeg", "-y", "-i", str(p),
            "-ar", str(_REF_TARGET_RATE),
            "-ac", "1",
            "-sample_fmt", "s16",
            "-t", str(_REF_MAX_DURATION),
            str(norm_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and norm_path.exists():
            converted = True
        else:
            logger.warning("ffmpeg normalization failed: %s", result.stderr[:200])
    except FileNotFoundError:
        # ffmpeg not installed — try soundfile as fallback
        try:
            import soundfile as sf
            import numpy as np
            data, sr = sf.read(str(p))
            # Stereo to mono
            if len(data.shape) > 1:
                data = np.mean(data, axis=1)
            # Resample to target rate
            if sr != _REF_TARGET_RATE:
                ratio = sr / _REF_TARGET_RATE
                if abs(ratio - round(ratio)) < 0.01 and round(ratio) >= 2:
                    data = data[::int(round(ratio))]
                else:
                    old_len = len(data)
                    new_len = int(old_len * _REF_TARGET_RATE / sr)
                    new_indices = np.linspace(0, old_len - 1, new_len)
                    data = np.interp(new_indices, np.arange(old_len), data)
            # Trim to max duration
            max_samples = _REF_MAX_DURATION * _REF_TARGET_RATE
            if len(data) > max_samples:
                data = data[:max_samples]
            data = data.astype(np.float32)
            peak = np.max(np.abs(data))
            if peak > 1.0:
                data = data / peak
            sf.write(str(norm_path), data, _REF_TARGET_RATE, subtype='PCM_16')
            converted = True
        except ImportError:
            logger.warning("Neither ffmpeg nor soundfile available, using original ref audio (%s): %s",
                          reason, audio_path)
        except Exception as sf_err:
            logger.warning("soundfile normalization failed: %s", sf_err)
    except Exception as e:
        logger.error("Ref audio normalization error: %s", e)

    if converted and norm_path.exists():
        logger.info("Normalized ref audio (%s): %s -> %s", reason, p.name, norm_path.name)
        _REF_AUDIO_CACHE[audio_path] = str(norm_path)
        return str(norm_path)

    _REF_AUDIO_CACHE[audio_path] = audio_path
    return audio_path


def clear_tts_tmp():
    """Loescht alle temporaeren TTS-Audio-Dateien. Wird beim Server-Start aufgerufen."""
    if (_get_storage_dir() / "tmp" / "tts_audio").exists():
        shutil.rmtree((_get_storage_dir() / "tmp" / "tts_audio"))
        logger.info("Temp-Verzeichnis geleert: %s", (_get_storage_dir() / "tmp" / "tts_audio"))
    (_get_storage_dir() / "tmp" / "tts_audio").mkdir(parents=True, exist_ok=True)


def clean_text_for_tts(text: str) -> str:
    """Bereinigt LLM-Antworttext fuer TTS-Ausgabe."""
    # Options-Block entfernen
    text = re.sub(r'\*\*Option\s+[A-Z]:\*\*\s*\[?[^\]\n]+\]?', '', text)
    # Mood-Marker entfernen
    text = re.sub(r'\*\*I\s+feel\s+.+?\*\*', '', text, flags=re.IGNORECASE)
    # Markdown bold/italic entfernen
    text = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', text)
    # Markdown headers entfernen
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Markdown links: [text](url) -> text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Inline-Bilder entfernen
    text = re.sub(r'!\[.*?\]\([^)]+\)', '', text)
    # URLs entfernen
    text = re.sub(r'https?://\S+', '', text)
    # Emojis entfernen (common unicode ranges)
    text = re.sub(
        r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF'
        r'\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0000FE0F]+',
        '', text
    )
    # Mehrfache Leerzeilen reduzieren
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _find_split_pos(buffer: str, i: int) -> int:
    """Findet die Split-Position nach einem Satzende bei Index i.

    Schliesst nachfolgende Anfuehrungszeichen, Klammern und Sternchen ein,
    damit diese nicht als Fragmente im naechsten Chunk landen.
    """
    pos = i + 1
    while pos < len(buffer) and buffer[pos] in '"\'""»›*)':
        pos += 1
    return pos


def split_tts_chunk(buffer: str, min_chars: int) -> tuple:
    """Splittet Buffer am ersten Satzende nach min_chars.

    Erkennt echte Satzenden: Punkt/Ausrufezeichen/Fragezeichen gefolgt von
    Leerzeichen oder Newline. Ignoriert Abkuerzungen (z.B., Dr., Nr.).
    Schliesst nachfolgende Anfuehrungszeichen/Sternchen ein.

    Args:
        buffer: Gesammelter Text fuer TTS.
        min_chars: Minimale Zeichenanzahl bevor ein Satzende gesucht wird.

    Returns:
        (chunk, rest) — chunk ist None wenn Buffer zu kurz oder kein Satzende.
    """
    if len(buffer) < min_chars:
        return (None, buffer)

    # Erstes echtes Satzende AB min_chars suchen
    for i in range(min_chars, len(buffer)):
        ch = buffer[i]

        if ch in '!?':
            pos = _find_split_pos(buffer, i)
            chunk = buffer[:pos].strip()
            remaining = buffer[pos:]
            if chunk:
                return (chunk, remaining)

        elif ch == '.':
            next_idx = i + 1
            # Nachfolgende Anfuehrungszeichen ueberspringen
            closing = next_idx
            while closing < len(buffer) and buffer[closing] in '"\'""»›*)':
                closing += 1

            if closing >= len(buffer):
                # Punkt (+ Quotes) am Textende
                chunk = buffer[:closing].strip()
                if chunk:
                    return (chunk, "")
            elif closing < len(buffer) and buffer[closing] in ' \n\t':
                # Punkt + Whitespace — Abkuerzungen ausschliessen
                word_start = i - 1
                while word_start >= 0 and buffer[word_start].isalpha():
                    word_start -= 1
                word_len = i - word_start - 1
                if word_len > 2 or buffer[closing] == '\n':
                    chunk = buffer[:closing].strip()
                    remaining = buffer[closing:]
                    if chunk:
                        return (chunk, remaining)

        elif ch == '\n' and i > 0 and buffer[i - 1] == '\n':
            # Doppelter Zeilenumbruch = Absatzende
            chunk = buffer[:i].strip()
            remaining = buffer[i:]
            if chunk:
                return (chunk, remaining)

    # Kein Satzende nach min_chars gefunden — suche LETZTES Satzende DAVOR
    for i in range(min(min_chars, len(buffer)) - 1, -1, -1):
        ch = buffer[i]
        if ch in '!?':
            pos = _find_split_pos(buffer, i)
            chunk = buffer[:pos].strip()
            if chunk:
                return (chunk, buffer[pos:])
        elif ch == '.':
            pos = _find_split_pos(buffer, i)
            if pos < len(buffer) and buffer[pos] in ' \n\t':
                word_start = i - 1
                while word_start >= 0 and buffer[word_start].isalpha():
                    word_start -= 1
                word_len = i - word_start - 1
                if word_len > 2 or buffer[pos] == '\n':
                    chunk = buffer[:pos].strip()
                    if chunk:
                        return (chunk, buffer[pos:])

    return (None, buffer)


class TTSService:
    """Manages TTS backends (XTTS v2, F5-TTS, Magpie) with global + per-character config."""

    def __init__(self):
        self.enabled = False
        self.auto = False
        self.backend = "xtts"
        self.xtts_url = ""
        self.xtts_speaker_wav = ""
        self.xtts_language = "de"
        self.magpie_url = ""
        self.magpie_voice = ""
        self.magpie_language = "de-DE"
        self.f5_url = ""
        self.f5_ref_audio = ""
        self.f5_ref_text = ""
        self.f5_custom_ckpt = ""
        self.f5_custom_vocab = ""
        self.f5_custom_cfg = ""
        self.f5_remove_silence = False
        self.f5_speed = 1.0
        self.f5_nfe_steps = 32
        self._f5_client = None
        self._f5_ref_text_cache: Dict[str, str] = {}
        # F5-TTS per-language model registry
        self._f5_language_models: Dict[str, Dict[str, str]] = {}
        self._f5_current_language: Optional[str] = None
        self._f5_model_lock = threading.Lock()
        self._available = None
        self.fallback_backend = ""
        self._load_config()

    def _load_config(self):
        """Laedt Config aus Umgebungsvariablen."""
        self.enabled = os.environ.get("TTS_ENABLED", "false").lower() in ("true", "1", "yes")
        self.auto = os.environ.get("TTS_AUTO", "false").lower() in ("true", "1", "yes")
        self.chunk_size = int(os.environ.get("TTS_CHUNK_SIZE", "0"))
        self.backend = os.environ.get("TTS_BACKEND", "xtts").lower()
        self.fallback_backend = os.environ.get("TTS_FALLBACK_BACKEND", "").lower()

        # XTTS
        self.xtts_url = os.environ.get("TTS_XTTS_URL", "http://localhost:8020").rstrip("/")
        self.xtts_speaker_wav = os.environ.get("TTS_XTTS_SPEAKER_WAV", "")
        self.xtts_language = os.environ.get("TTS_XTTS_LANGUAGE", "de")

        # Magpie
        self.magpie_url = os.environ.get("TTS_MAGPIE_URL", "http://localhost:9000").rstrip("/")
        self.magpie_voice = os.environ.get("TTS_MAGPIE_VOICE", "")
        self.magpie_language = os.environ.get("TTS_MAGPIE_LANGUAGE", "de-DE")

        # ComfyUI TTS — Liste von ComfyUI-Backends (Fallback-Reihenfolge).
        # URL wird pro Call aufgeloest (_pick_comfyui_url), nicht beim Init festgepinnt.
        self.comfyui_skill = os.environ.get("TTS_COMFYUI_SKILL", "").strip()
        self.comfyui_candidates: List[Tuple[str, str, int]] = []
        self.comfyui_vram_required_mb = 4096
        if self.comfyui_skill:
            self.comfyui_candidates = _resolve_comfyui_candidates(self.comfyui_skill)
            vram_values = [v for _n, _u, v in self.comfyui_candidates if v]
            if vram_values:
                self.comfyui_vram_required_mb = max(vram_values)
        _vram_override = os.environ.get("TTS_COMFYUI_VRAM_REQUIRED", "").strip()
        if _vram_override:
            self.comfyui_vram_required_mb = int(float(_vram_override) * 1024)
        self.comfyui_mode = os.environ.get("TTS_COMFYUI_MODE", "voiceclone").lower()  # voiceclone | voicedesc | voicename | auto
        self.comfyui_workflow_voiceclone = os.environ.get("TTS_COMFYUI_WORKFLOW_VOICECLONE", "./workflows/tts_voiceclone_workflow_api.json")
        self.comfyui_workflow_voicedesc = os.environ.get("TTS_COMFYUI_WORKFLOW_VOICEDESC", "./workflows/tts_voicedesc_workflow_api.json")
        self.comfyui_workflow_voicename = os.environ.get("TTS_COMFYUI_WORKFLOW_VOICENAME", "./workflows/tts_voicename_workflow_api.json")
        self.comfyui_max_wait = int(os.environ.get("TTS_COMFYUI_MAX_WAIT", "300"))
        self.comfyui_poll_interval = float(os.environ.get("TTS_COMFYUI_POLL_INTERVAL", "1.0"))

        # F5-TTS
        self.f5_url = os.environ.get("TTS_F5_URL", "http://localhost:7860").rstrip("/")
        self.f5_ref_audio = os.environ.get("TTS_F5_REF_AUDIO", "")
        self.f5_ref_text = os.environ.get("TTS_F5_REF_TEXT", "")
        self.f5_custom_ckpt = os.environ.get("TTS_F5_CUSTOM_CKPT", "")
        self.f5_custom_vocab = os.environ.get("TTS_F5_CUSTOM_VOCAB", "")
        self.f5_custom_cfg = os.environ.get("TTS_F5_CUSTOM_CFG", "")
        self.f5_remove_silence = os.environ.get("TTS_F5_REMOVE_SILENCE", "false").lower() in ("true", "1", "yes")
        self.f5_speed = float(os.environ.get("TTS_F5_SPEED", "1.0"))
        self.f5_nfe_steps = int(os.environ.get("TTS_F5_NFE_STEPS", "32"))
        self._f5_client = None

        # F5-TTS per-language model registry: scan TTS_F5_MODEL_{LANG} env vars
        self._f5_language_models = {}
        prefix = "TTS_F5_MODEL_"
        for key, value in os.environ.items():
            if key.startswith(prefix) and len(key) == len(prefix) + 2:
                lang = key[len(prefix):].lower()
                ckpt = value.strip()
                vocab = os.environ.get(f"TTS_F5_VOCAB_{lang.upper()}", "").strip()
                cfg = os.environ.get(f"TTS_F5_CFG_{lang.upper()}", "").strip()
                self._f5_language_models[lang] = {
                    "ckpt": ckpt,
                    "vocab": vocab,
                    "cfg": cfg or self.f5_custom_cfg,
                }
                logger.info("F5-TTS: Sprache '%s' registriert: %s",
                            lang, 'standard' if not ckpt else ckpt.split('/')[-1][:50])

        # Backward compatibility: old TTS_F5_CUSTOM_CKPT as legacy fallback
        if not self._f5_language_models and self.f5_custom_ckpt:
            self._f5_language_models["_legacy"] = {
                "ckpt": self.f5_custom_ckpt,
                "vocab": self.f5_custom_vocab,
                "cfg": self.f5_custom_cfg,
            }
            logger.info("F5-TTS: Legacy Single-Model Config (keine TTS_F5_MODEL_* Variablen)")

    def _pick_comfyui_url(self) -> str:
        """Waehlt den ersten erreichbaren ComfyUI-Backend aus den konfigurierten
        Candidates. Leerer String wenn keiner erreichbar ist.
        """
        for name, url, _vram in self.comfyui_candidates:
            if not url:
                continue
            try:
                resp = requests.get(f"{url}/system_stats", timeout=5)
                if resp.status_code == 200:
                    return url
                logger.debug("TTS ComfyUI: %s (%s) HTTP %d", name, url, resp.status_code)
            except Exception as e:
                logger.debug("TTS ComfyUI: %s (%s) nicht erreichbar: %s", name, url, type(e).__name__)
        return ""

    def _check_backend_reachable(self, backend: str) -> bool:
        """Prueft ob ein bestimmtes Backend erreichbar ist (ohne Modelle zu laden)."""
        try:
            if backend == "xtts":
                resp = requests.get(f"{self.xtts_url}/docs", timeout=5)
                return resp.status_code == 200
            elif backend == "magpie":
                resp = requests.get(self.magpie_url, timeout=5)
                return resp.status_code < 500
            elif backend == "f5":
                resp = requests.get(self.f5_url, timeout=5)
                return resp.status_code == 200
            elif backend == "comfyui":
                return bool(self._pick_comfyui_url())
            return False
        except Exception:
            return False

    def is_available(self) -> bool:
        """Prueft ob das konfigurierte TTS-Backend erreichbar ist."""
        if not self.enabled:
            self._available = False
            return False

        try:
            if self.backend == "xtts":
                resp = requests.get(f"{self.xtts_url}/docs", timeout=5)
                self._available = resp.status_code == 200
            elif self.backend == "magpie":
                resp = requests.get(self.magpie_url, timeout=5)
                self._available = resp.status_code < 500
            elif self.backend == "comfyui":
                self._available = bool(self._pick_comfyui_url())
            elif self.backend == "f5":
                resp = requests.get(self.f5_url, timeout=5)
                self._available = resp.status_code == 200
                if self._available and self._f5_language_models:
                    # Load the first registered language model at startup
                    first_lang = next(iter(self._f5_language_models))
                    if first_lang != "_legacy":
                        self._ensure_f5_model(first_lang)
                    elif self.f5_custom_ckpt:
                        self._ensure_f5_model("")
            else:
                self._available = False
        except requests.exceptions.ConnectionError:
            self._available = False
        except Exception:
            self._available = False

        if not self._available and self.fallback_backend and self.fallback_backend != self.backend:
            logger.warning("Primaeres Backend '%s' nicht erreichbar, pruefe Fallback '%s'...",
                          self.backend, self.fallback_backend)
            if self._check_backend_reachable(self.fallback_backend):
                logger.info("Fallback '%s' ist verfuegbar", self.fallback_backend)
                self._available = True
            else:
                logger.warning("Auch Fallback '%s' nicht erreichbar", self.fallback_backend)

        return self._available

    def get_character_config(self, agent_config: Dict[str, Any]) -> Dict[str, Any]:
        """Merged globale + per-character TTS-Einstellungen.

        Per-character values override global defaults.
        """
        # speaker_wav default haengt vom Backend ab
        if self.backend == "f5":
            default_speaker = self.f5_ref_audio
        elif self.backend == "comfyui":
            default_speaker = ""  # per Character konfiguriert (tts_speaker_wav)
        else:
            default_speaker = self.xtts_speaker_wav
        # voice default: Magpie hat eigene benannte Voices
        if self.backend == "magpie":
            default_voice = self.magpie_voice
            default_language = self.magpie_language
        else:
            default_voice = ""
            default_language = self.xtts_language
        return {
            "enabled": agent_config.get("tts_enabled", self.enabled),
            "auto": agent_config.get("tts_auto", self.auto),
            "voice": agent_config.get("tts_voice", "") or default_voice,
            "speaker_wav": agent_config.get("tts_speaker_wav", "") or default_speaker,
            "language": agent_config.get("tts_language", "") or default_language,
            "voice_description": agent_config.get("tts_voice_description", ""),
            "character_name": agent_config.get("name", ""),
            "comfyui_mode": agent_config.get("tts_comfyui_mode", "") or self.comfyui_mode,
        }

    def _generate_with_backend(
        self,
        backend: str,
        text: str,
        voice: str = "",
        speaker_wav: str = "",
        language: str = "",
        voice_description: str = "",
        character_name: str = "",
        comfyui_mode: str = "") -> Optional[Path]:
        """Generiert Audio mit einem bestimmten Backend."""
        if backend == "xtts":
            return self._generate_xtts(
                text,
                speaker_wav or self.xtts_speaker_wav,
                language or self.xtts_language)
        elif backend == "magpie":
            return self._generate_magpie(
                text,
                voice or self.magpie_voice,
                language or self.magpie_language)
        elif backend == "f5":
            effective_language = language or self.xtts_language
            # Thread-Lock: Model-Switch + Generierung atomar
            with self._f5_model_lock:
                if not self._ensure_f5_model(effective_language):
                    logger.warning("F5-TTS: Model-Switch fuer '%s' fehlgeschlagen, "
                                  "versuche Generierung mit aktuellem Modell", effective_language)
                return self._generate_f5(
                    text,
                    speaker_wav or self.f5_ref_audio,
                    self.f5_ref_text,
                    effective_language)
        elif backend == "comfyui":
            return self._generate_comfyui_tts(
                text,
                speaker_wav,
                voice_description,
                character_name,
                comfyui_mode=comfyui_mode)
        else:
            logger.error("Unknown backend: %s", backend)
            return None

    def generate(
        self,
        text: str,
        voice: str = "",
        speaker_wav: str = "",
        language: str = "",
        voice_description: str = "",
        character_name: str = "",
        comfyui_mode: str = "") -> Optional[Path]:
        """Generiert eine Audio-Datei via konfiguriertem Backend.

        Versucht zunaechst das primaere Backend. Bei Fehler wird, falls konfiguriert,
        das Fallback-Backend (TTS_FALLBACK_BACKEND) verwendet.

        Args:
            text: Bereinigter Text fuer TTS.
            voice: Stimmenname fuer Magpie (override).
            speaker_wav: XTTS/F5-TTS Referenz-Audio (override).
            language: Sprache (fuer XTTS und F5-TTS Modellwahl).

        Returns:
            Pfad zur generierten Audio-Datei oder None bei Fehler.
        """
        if not text.strip():
            return None

        (_get_storage_dir() / "tmp" / "tts_audio").mkdir(parents=True, exist_ok=True)

        logger.debug("Text an TTS (%s): %s%s", self.backend, text[:500], '...' if len(text) > 500 else '')

        result = self._generate_with_backend(self.backend, text, voice, speaker_wav, language, voice_description, character_name, comfyui_mode=comfyui_mode)

        if result is None and self.fallback_backend and self.fallback_backend != self.backend:
            logger.warning("Primaeres Backend '%s' fehlgeschlagen, versuche Fallback '%s'...",
                          self.backend, self.fallback_backend)
            result = self._generate_with_backend(self.fallback_backend, text, voice, speaker_wav, language, voice_description, character_name, comfyui_mode=comfyui_mode)
            if result:
                logger.info("Fallback '%s' erfolgreich", self.fallback_backend)

        return result

    def _comfyui_find_node_by_title(self, workflow: Dict, title: str) -> Optional[str]:
        """Findet Node-ID anhand des _meta.title."""
        for node_id, node in workflow.items():
            if node.get("_meta", {}).get("title") == title:
                return node_id
        return None

    def _generate_comfyui_tts(
        self, text: str, ref_audio: str = "", voice_description: str = "",
        character_name: str = "", comfyui_mode: str = "") -> Optional[Path]:
        """Generiert Audio via ComfyUI TTS Workflow.

        Modus (TTS_COMFYUI_MODE):
          voiceclone: ref_audio (WAV) als Referenzstimme → FB_Qwen3TTSVoiceClone
          voicedesc:  voice_description → Qwen3TTSVoiceDesignerNode (speichert Stimme als {character_name}.wav)
          voicename:  {character_name}.wav als gespeicherte Stimme → UnifiedTTSTextNode
          auto:       voicedesc beim ersten Mal oder bei Description-Aenderung, danach voicename
        """
        import copy
        import time as _time

        mode = comfyui_mode or self.comfyui_mode

        # Auto-Modus: Entscheidet zwischen voicedesc und voicename
        _is_auto = mode == "auto"
        if _is_auto:
            if not character_name or not voice_description:
                # Ohne Character-Name oder Description kann auto nicht arbeiten
                if ref_audio and Path(ref_audio).exists():
                    mode = "voiceclone"
                    logger.info("ComfyUI TTS auto: Kein character_name/voice_description, Fallback auf voiceclone")
                else:
                    logger.error("ComfyUI TTS auto: Weder voice_description+character_name noch ref_audio vorhanden")
                    return None
            else:
                desc_hash = _hash_voice_desc(voice_description)
                cache = _load_voice_desc_cache()
                cached_hash = cache.get(character_name, "")
                if cached_hash == desc_hash:
                    # Stimme wurde schon mit dieser Description generiert → voicename
                    mode = "voicename"
                    logger.info("ComfyUI TTS auto: Voice fuer '%s' existiert (Hash match) → voicename", character_name)
                else:
                    # Neue/geaenderte Description → voicedesc (generiert + speichert Stimme)
                    mode = "voicedesc"
                    if cached_hash:
                        logger.info("ComfyUI TTS auto: Voice-Description geaendert fuer '%s' → voicedesc", character_name)
                    else:
                        logger.info("ComfyUI TTS auto: Keine Voice fuer '%s' cached → voicedesc", character_name)

        if mode == "voiceclone":
            workflow_file = self.comfyui_workflow_voiceclone
        elif mode == "voicename":
            workflow_file = self.comfyui_workflow_voicename
        else:
            workflow_file = self.comfyui_workflow_voicedesc

        if not workflow_file or not Path(workflow_file).exists():
            logger.error("ComfyUI TTS: Workflow-Datei nicht gefunden: %s", workflow_file)
            return None

        # Einen ComfyUI-Backend fuer die gesamte Generierung pinnen (Upload + Submit +
        # Poll + Download muessen auf derselben Instanz laufen).
        comfyui_url = self._pick_comfyui_url()
        if not comfyui_url:
            logger.error("ComfyUI TTS: Kein erreichbares Backend in %s",
                         [n for n, _u, _v in self.comfyui_candidates])
            return None

        with open(workflow_file) as f:
            workflow = copy.deepcopy(json.load(f))

        # Eingabe-Text setzen
        text_node = self._comfyui_find_node_by_title(workflow, "input_text")
        if not text_node:
            logger.error("ComfyUI TTS: Node 'input_text' nicht gefunden")
            return None
        workflow[text_node]["inputs"]["value"] = text

        if mode == "voiceclone":
            if not ref_audio or not Path(ref_audio).exists():
                logger.error("ComfyUI TTS: ref_audio nicht gefunden: %s", ref_audio)
                return None
            # Referenz-Audio zu ComfyUI hochladen
            audio_node = self._comfyui_find_node_by_title(workflow, "input_reference_audio")
            if not audio_node:
                logger.error("ComfyUI TTS: Node 'input_reference_audio' nicht gefunden")
                return None
            try:
                with open(ref_audio, "rb") as af:
                    upload_resp = requests.post(
                        f"{comfyui_url}/upload/image",
                        files={"image": (Path(ref_audio).name, af, "audio/wav")},
                        timeout=30)
                if upload_resp.status_code != 200:
                    logger.error("ComfyUI TTS: Audio-Upload fehlgeschlagen: %s", upload_resp.text[:200])
                    return None
                uploaded_name = upload_resp.json().get("name", "")
                if not uploaded_name:
                    logger.error("ComfyUI TTS: Kein Name in Upload-Response")
                    return None
                workflow[audio_node]["inputs"]["audio"] = uploaded_name
                logger.debug("ComfyUI TTS: Ref-Audio hochgeladen: %s", uploaded_name)
            except Exception as e:
                logger.error("ComfyUI TTS: Audio-Upload Fehler: %s", e)
                return None
        elif mode == "voicename":
            # VoiceName: gespeicherte Stimme per Character-Name referenzieren
            if not character_name:
                logger.error("ComfyUI TTS (voicename): character_name nicht gesetzt")
                return None
            narrator_wav = f"{character_name}.wav"
            # UnifiedTTSTextNode hat narrator_voice als direkten Widget-Input
            for node_id, node in workflow.items():
                if node.get("class_type") == "UnifiedTTSTextNode":
                    node["inputs"]["narrator_voice"] = narrator_wav
                    break
            else:
                logger.error("ComfyUI TTS (voicename): UnifiedTTSTextNode nicht gefunden")
                return None
            logger.debug("ComfyUI TTS (voicename): narrator_voice=%s", narrator_wav)
        else:
            # VoiceDesign: Stimmbeschreibung + Character-Name setzen
            desc_node = self._comfyui_find_node_by_title(workflow, "input_voice_description")
            if desc_node and voice_description:
                workflow[desc_node]["inputs"]["value"] = voice_description
            name_node = self._comfyui_find_node_by_title(workflow, "input_voice_name")
            if name_node and character_name:
                workflow[name_node]["inputs"]["value"] = character_name

        # Core execution: submit + poll + download (runs inside GPU task or directly)
        def _comfyui_tts_execute() -> Optional[Path]:
            try:
                resp = requests.post(f"{comfyui_url}/prompt", json={"prompt": workflow}, timeout=30)
            except Exception as e:
                logger.error("ComfyUI TTS: Submit-Fehler: %s", e)
                return None
            if resp.status_code != 200:
                logger.error("ComfyUI TTS: HTTP %d: %s", resp.status_code, resp.text[:500])
                # Auto-Fallback: voicename fehlgeschlagen → voicedesc retry
                if _is_auto and mode == "voicename" and voice_description and character_name:
                    logger.info("ComfyUI TTS auto: voicename fehlgeschlagen, Fallback auf voicedesc")
                    _save_voice_desc_cache(character_name, "")
                    return self._generate_comfyui_tts(
                        text, ref_audio, voice_description, character_name, comfyui_mode="voicedesc")
                return None
            prompt_id = resp.json().get("prompt_id", "")
            if not prompt_id:
                logger.error("ComfyUI TTS: Keine prompt_id in Response")
                return None
            logger.info("ComfyUI TTS: Queued (mode=%s, prompt_id=%s)", mode, prompt_id)

            # Polling
            start = _time.time()
            outputs = {}
            while _time.time() - start < self.comfyui_max_wait:
                _time.sleep(self.comfyui_poll_interval)
                try:
                    hist = requests.get(f"{comfyui_url}/history/{prompt_id}", timeout=10).json()
                    if prompt_id not in hist:
                        continue
                    status = hist[prompt_id].get("status", {})
                    if status.get("status_str") == "error":
                        messages = status.get("messages", [])
                        err_detail = ""
                        for msg in messages:
                            if isinstance(msg, (list, tuple)) and len(msg) >= 2 and msg[0] == "execution_error":
                                detail = msg[1] if isinstance(msg[1], dict) else {}
                                err_detail = detail.get("exception_message", "")
                                node_type = detail.get("node_type", "")
                                if node_type:
                                    err_detail = f"[{node_type}] {err_detail}"
                                break
                        err = err_detail or str(messages)[:500]
                        logger.error("ComfyUI TTS: Fehler: %s", err)
                        return None
                    outputs = hist[prompt_id].get("outputs", {})
                    if outputs:
                        logger.info("ComfyUI TTS: Fertig nach %.1fs", _time.time() - start)
                        break
                except Exception as e:
                    logger.warning("ComfyUI TTS: Poll-Fehler: %s", e)
            else:
                logger.error("ComfyUI TTS: Timeout nach %ds", self.comfyui_max_wait)
                return None

            # Audio aus output_audio Node herunterladen
            output_node = self._comfyui_find_node_by_title(workflow, "output_audio")
            if output_node and output_node in outputs:
                target_outputs = {output_node: outputs[output_node]}
            else:
                target_outputs = outputs

            for node_id, node_output in target_outputs.items():
                for audio_info in node_output.get("audio", []):
                    filename = audio_info.get("filename", "")
                    if not filename:
                        continue
                    dl_params = {"filename": filename, "type": audio_info.get("type", "output")}
                    subfolder = audio_info.get("subfolder", "")
                    if subfolder:
                        dl_params["subfolder"] = subfolder
                    try:
                        audio_resp = requests.get(f"{comfyui_url}/view", params=dl_params, timeout=60)
                        if audio_resp.status_code == 200:
                            (_get_storage_dir() / "tmp" / "tts_audio").mkdir(parents=True, exist_ok=True)
                            out_path = (_get_storage_dir() / "tmp" / "tts_audio") / f"{uuid.uuid4().hex}.wav"
                            out_path.write_bytes(audio_resp.content)
                            logger.info("ComfyUI TTS: %d bytes -> %s", len(audio_resp.content), out_path.name)
                            if _is_auto and mode == "voicedesc" and character_name and voice_description:
                                _save_voice_desc_cache(character_name, _hash_voice_desc(voice_description))
                                logger.info("ComfyUI TTS auto: Voice-Cache gespeichert fuer '%s'", character_name)
                            return out_path
                    except Exception as e:
                        logger.error("ComfyUI TTS: Download-Fehler: %s", e)

            logger.warning("ComfyUI TTS: Kein Audio-Ergebnis gefunden")
            return None

        # Route through GPU task queue (dynamic channel routing)
        try:
            from app.core.llm_queue import get_llm_queue
            mode_label = {"voiceclone": "Voice Clone", "voicedesc": "Voice Design", "voicename": "Voice Name"}.get(mode, mode)
            return get_llm_queue().submit_gpu_task(
                task_type="tts_comfyui",
                priority=5,
                callable_fn=_comfyui_tts_execute,
                agent_name=character_name,
                label=f"TTS {mode_label}",
                vram_required_mb=self.comfyui_vram_required_mb,
                gpu_type="comfyui")
        except Exception as e:
            logger.error("ComfyUI TTS: GPU-Task-Fehler: %s", e)
            return None

    def _generate_xtts(
        self, text: str, speaker_wav: str, language: str
    ) -> Optional[Path]:
        """XTTS v2: Sendet Text + Speaker-Referenz an xtts-api-server."""
        if not speaker_wav:
            logger.error("XTTS error: speaker_wav is required but not configured. Set TTS_XTTS_SPEAKER_WAV.")
            return None
        speaker_wav = _normalize_reference_audio(speaker_wav)
        try:
            payload = {
                "text": text,
                "language": language,
                "speaker_wav": speaker_wav,
            }

            resp = requests.post(
                f"{self.xtts_url}/tts_to_audio/",
                json=payload,
                timeout=300)
            if resp.status_code == 200:
                filename = f"{uuid.uuid4().hex}.wav"
                out_path = (_get_storage_dir() / "tmp" / "tts_audio") / filename
                out_path.write_bytes(resp.content)
                logger.debug("XTTS: %d bytes -> %s", len(resp.content), out_path)
                return out_path
            else:
                logger.error("XTTS error: HTTP %d - %s", resp.status_code, resp.text[:200])
                return None
        except Exception as e:
            logger.error("XTTS error: %s", e)
            return None

    # Explizite Allowlist fuer Magpie TTS - nur diese Zeichen werden durchgelassen.
    # Magpie's character_mapping kennt keine Ziffern, Sonderzeichen wie % @ # etc.
    _MAGPIE_ALLOWED = frozenset(
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "äöüÄÖÜß"
        " \t\n"
        ".,!?;:-'\"/()'"
    )

    @staticmethod
    def _clean_text_for_magpie(text: str) -> str:
        """Extra text cleanup for Magpie TTS to avoid pronunciation mapping errors.

        Magpie's phonemizer fails on characters outside its supported set
        (digits, %, @, # etc.). Only characters in _MAGPIE_ALLOWED pass through.
        """
        # Replace common typographic/Unicode characters with allowed equivalents
        replacements = {
            "\u2014": " - ",   # em dash
            "\u2013": " - ",   # en dash
            "\u2026": "...",   # ellipsis
            "\u201e": '"',     # „
            "\u201c": '"',     # "
            "\u201d": '"',     # "
            "\u2018": "'",     # '
            "\u2019": "'",     # '
            "\u00ab": '"',     # «
            "\u00bb": '"',     # »
            "\u2022": ",",     # bullet
            "\u00a0": " ",     # non-breaking space
            "\u200b": "",      # zero-width space
            "\u200d": "",      # zero-width joiner
            "\ufeff": "",      # BOM
            # French/Latin accented characters → ASCII base
            "à": "a", "â": "a", "á": "a", "ã": "a", "å": "a",
            "è": "e", "é": "e", "ê": "e", "ë": "e",
            "ì": "i", "í": "i", "î": "i", "ï": "i",
            "ò": "o", "ó": "o", "ô": "o", "õ": "o",
            "ù": "u", "ú": "u", "û": "u",
            "ý": "y", "ÿ": "y",
            "ç": "c", "ñ": "n",
            "À": "A", "Â": "A", "Á": "A",
            "È": "E", "É": "E", "Ê": "E", "Ë": "E",
            "Î": "I", "Ï": "I",
            "Ô": "O", "Ó": "O",
            "Û": "U", "Ú": "U",
            "Ç": "C", "Ñ": "N",
            # Ampersand → "und"
            "&": " und ",
        }
        for old, new in replacements.items():
            text = text.replace(old, new)

        # Allowlist-Filter: alle nicht erlaubten Zeichen durch Leerzeichen ersetzen
        text = ''.join(c if c in TTSService._MAGPIE_ALLOWED else ' ' for c in text)

        # Collapse multiple spaces/newlines
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n{2,}', '\n', text)
        return text.strip()

    @staticmethod
    def _transliterate_for_magpie(text: str) -> str:
        """Fallback: replace German umlauts/special chars with ASCII equivalents.

        Magpie's phonemizer sometimes fails on certain umlaut combinations.
        This gives it a second chance with pure ASCII text.
        """
        replacements = {
            "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
            "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text

    def _magpie_sentence_fallback(
        self, text: str, language: str, voice: str) -> Optional[Path]:
        """Synthesize sentence by sentence, concatenating WAV results.

        Splits on sentence-ending punctuation AND newlines (for titles/paragraphs).
        Skips sentences that fail and concatenates the rest.
        Returns None if all sentences fail.
        """
        import wave
        import io

        # Split on sentence-ending punctuation OR newlines
        sentences = re.split(r'(?<=[.!?])\s+|\n+', text)
        sentences = [s.strip() for s in sentences if s.strip()]
        if len(sentences) <= 1:
            return None

        logger.debug("Magpie sentence fallback: %d chunks to process", len(sentences))

        wav_chunks = []
        params_set = False
        wav_params = None

        for i, sentence in enumerate(sentences):
            if not sentence:
                continue
            # Build fresh form_data for each sentence
            sentence_form = {
                "text": (None, sentence),
                "language": (None, language),
                "voice": (None, voice),
            }
            try:
                resp = requests.post(
                    f"{self.magpie_url}/v1/audio/synthesize",
                    files=sentence_form, timeout=60)
                if resp.status_code == 200 and len(resp.content) > 44:
                    bio = io.BytesIO(resp.content)
                    with wave.open(bio, 'rb') as wf:
                        if not params_set:
                            wav_params = wf.getparams()
                            params_set = True
                        wav_chunks.append(wf.readframes(wf.getnframes()))
                else:
                    err_detail = resp.text[:120] if resp.text else "(empty)"
                    logger.warning("Magpie skip [%d/%d] HTTP %d: %s | text: %s...",
                                  i+1, len(sentences), resp.status_code, err_detail, sentence[:50])
            except Exception as e:
                logger.error("Magpie sentence error [%d/%d]: %s", i+1, len(sentences), e)

        if not wav_chunks or not wav_params:
            logger.error("Magpie: all sentences failed")
            return None

        # Concatenate WAV chunks
        filename = f"{uuid.uuid4().hex}.wav"
        out_path = (_get_storage_dir() / "tmp" / "tts_audio") / filename
        with wave.open(str(out_path), 'wb') as out_wf:
            out_wf.setparams(wav_params)
            for chunk in wav_chunks:
                out_wf.writeframes(chunk)

        ok_count = len(wav_chunks)
        total = len(sentences)
        logger.debug("Magpie sentence fallback: %d/%d sentences OK -> %s", ok_count, total, out_path)
        return out_path

    def _generate_magpie(
        self, text: str, voice: str, language: str
    ) -> Optional[Path]:
        """Magpie TTS: Sendet Text via multipart form an Magpie REST API."""
        if not voice:
            logger.error("Magpie error: voice is required. Set TTS_MAGPIE_VOICE.")
            return None
        try:
            # Sanitize text for Magpie's phonemizer
            text = self._clean_text_for_magpie(text)
            if not text:
                logger.warning("Magpie: empty text after cleanup, skipping")
                return None

            # Magpie braucht Locale-Format "de-DE" (lowercase-UPPERCASE).
            # Sprache aus Voice-Name extrahieren (Magpie-Multilingual.DE-DE.Name)
            # oder Kurzform zu Locale mappen.
            effective_lang = language
            parts = voice.split(".")
            if len(parts) >= 2:
                raw_lang = parts[1]  # z.B. "DE-DE" aus "Magpie-Multilingual.DE-DE.Sofia"
                lang_parts = raw_lang.split("-")
                if len(lang_parts) == 2:
                    effective_lang = f"{lang_parts[0].lower()}-{lang_parts[1].upper()}"
                else:
                    effective_lang = raw_lang.lower()
            elif len(language) == 2:
                effective_lang = f"{language}-{language.upper()}"  # "de" -> "de-DE"
            form_data = {
                "text": (None, text),
                "language": (None, effective_lang),
                "voice": (None, voice),
            }
            resp = requests.post(
                f"{self.magpie_url}/v1/audio/synthesize",
                files=form_data,
                timeout=300)
            if resp.status_code == 200:
                filename = f"{uuid.uuid4().hex}.wav"
                out_path = (_get_storage_dir() / "tmp" / "tts_audio") / filename
                out_path.write_bytes(resp.content)
                logger.debug("Magpie: %d bytes -> %s", len(resp.content), out_path)
                return out_path

            # Mapping error: retry strategies
            if resp.status_code == 400 and "Mapping failed" in resp.text:
                logger.warning("Magpie mapping failed for: %s...", text[:80])

                # Strategy 1: transliterate umlauts
                fallback_text = self._transliterate_for_magpie(text)
                retry_form = {
                    "text": (None, fallback_text),
                    "language": (None, effective_lang),
                    "voice": (None, voice),
                }
                resp2 = requests.post(
                    f"{self.magpie_url}/v1/audio/synthesize",
                    files=retry_form, timeout=300)
                if resp2.status_code == 200:
                    filename = f"{uuid.uuid4().hex}.wav"
                    out_path = (_get_storage_dir() / "tmp" / "tts_audio") / filename
                    out_path.write_bytes(resp2.content)
                    logger.info("Magpie OK after transliteration")
                    return out_path

                err2 = resp2.text[:150] if resp2.text else "(empty)"
                logger.warning("Magpie transliteration also failed: HTTP %d - %s", resp2.status_code, err2)

                # Strategy 2: sentence-by-sentence, skip failures
                logger.warning("Trying sentence-by-sentence fallback")
                return self._magpie_sentence_fallback(
                    fallback_text, effective_lang, voice)

            logger.error("Magpie error: HTTP %d - %s", resp.status_code, resp.text[:200])
            return None
        except Exception as e:
            logger.error("Magpie error: %s", e)
            return None

    def _ensure_f5_model(self, language: str) -> bool:
        """Stellt sicher, dass das richtige F5-TTS Modell fuer die Sprache geladen ist.

        Ueberspringt den Switch wenn die Sprache bereits aktiv ist.
        Returns True wenn Modell bereit, False bei Fehler.
        """
        lang_key = language.lower() if language else ""

        if lang_key not in self._f5_language_models:
            if "_legacy" in self._f5_language_models:
                lang_key = "_legacy"
            else:
                # Keine Sprach-Config und kein Legacy -> Standard F5-TTS_v1
                if self._f5_current_language == "_standard":
                    return True
                return self._switch_to_standard_f5()

        # Bereits geladen?
        if self._f5_current_language == lang_key:
            return True

        model_info = self._f5_language_models[lang_key]
        ckpt = model_info["ckpt"]

        # Leerer ckpt = Standard F5-TTS_v1 (kein Custom-Modell)
        if not ckpt:
            return self._switch_to_standard_f5()

        # Custom-Modell laden
        try:
            client = self._get_f5_client()
            logger.info("F5-TTS: Lade Modell fuer Sprache '%s': %s", lang_key, ckpt.split('/')[-1][:60])
            # Choices: F5-TTS_v1, E2-TTS, Custom
            client.predict(new_choice="Custom", api_name="/switch_tts_model")
            # Base-Architektur Config als Standard
            cfg = model_info.get("cfg") or self.f5_custom_cfg or (
                '{"dim": 1024, "depth": 22, "heads": 16, "ff_mult": 2, '
                '"text_dim": 512, "text_mask_padding": false, "conv_layers": 4, '
                '"pe_attn_head": 1}'
            )
            client.predict(
                custom_ckpt_path=ckpt,
                custom_vocab_path=model_info.get("vocab", ""),
                custom_model_cfg=cfg,
                api_name="/set_custom_model")
            self._f5_current_language = lang_key
            logger.info("F5-TTS: Modell fuer '%s' geladen.", lang_key)
            return True
        except Exception as e:
            logger.error("F5-TTS: Modell-Switch fuer '%s' fehlgeschlagen: %s", lang_key, e)
            self._f5_client = None
            self._f5_current_language = None
            return False

    def _switch_to_standard_f5(self) -> bool:
        """Wechselt zum Standard F5-TTS_v1 Modell (kein Custom-Modell)."""
        try:
            client = self._get_f5_client()
            logger.info("F5-TTS: Wechsle zu Standard F5-TTS_v1")
            client.predict(new_choice="F5-TTS_v1", api_name="/switch_tts_model")
            self._f5_current_language = "_standard"
            logger.info("F5-TTS: Standard F5-TTS_v1 aktiv.")
            return True
        except Exception as e:
            logger.error("F5-TTS: Wechsel zu Standard-Modell fehlgeschlagen: %s", e)
            self._f5_client = None
            self._f5_current_language = None
            return False

    def _get_f5_client(self):
        """Returns a cached Gradio client for F5-TTS."""
        if self._f5_client is None:
            from gradio_client import Client
            self._f5_client = Client(self.f5_url)
        return self._f5_client

    def _transcribe_ref_audio(self, audio_path: str, language: str = "de") -> str:
        """Transkribiert Referenz-Audio mit faster-whisper. Ergebnis wird gecacht."""
        cache_key = f"{audio_path}:{language}"
        if cache_key in self._f5_ref_text_cache:
            return self._f5_ref_text_cache[cache_key]
        try:
            from faster_whisper import WhisperModel
            logger.debug("F5-TTS: Transkribiere Referenz-Audio '%s' (lang=%s) ...", audio_path, language)
            model = WhisperModel("small", device="cpu", compute_type="int8")
            segments, info = model.transcribe(audio_path, language=language or "de")
            text = " ".join(s.text.strip() for s in segments)
            logger.debug("F5-TTS: Transkription: '%s...'", text[:100])
            self._f5_ref_text_cache[cache_key] = text
            return text
        except Exception as e:
            logger.error("F5-TTS: Auto-Transkription fehlgeschlagen: %s", e)
            return ""

    def _generate_f5(self, text: str, ref_audio: str, ref_text: str, language: str = "de") -> Optional[Path]:
        """F5-TTS: Sendet Text an F5-TTS Gradio Server via gradio_client."""
        if not ref_audio:
            logger.error("F5-TTS error: ref_audio is required. Set TTS_F5_REF_AUDIO.")
            return None
        ref_audio = _normalize_reference_audio(ref_audio)

        # Auto-Transkription wenn ref_text leer ist
        if not ref_text.strip() and Path(ref_audio).exists():
            ref_text = self._transcribe_ref_audio(ref_audio, language)
            if not ref_text:
                logger.warning("F5-TTS: ref_text leer, F5-TTS versucht server-seitige Transkription.")

        try:
            from gradio_client import handle_file
            client = self._get_f5_client()
            result = client.predict(
                ref_audio_input=handle_file(ref_audio),
                ref_text_input=ref_text,
                gen_text_input=text,
                remove_silence=self.f5_remove_silence,
                nfe_slider=self.f5_nfe_steps,
                speed_slider=self.f5_speed,
                api_name="/basic_tts")
            # Result is tuple: (audio_filepath, spectrogram_filepath, ref_text, seed)
            audio_file = result[0] if isinstance(result, (list, tuple)) else result
            if audio_file and Path(audio_file).exists():
                filename = f"{uuid.uuid4().hex}.wav"
                out_path = (_get_storage_dir() / "tmp" / "tts_audio") / filename
                shutil.copy2(audio_file, out_path)
                file_size = out_path.stat().st_size
                logger.debug("F5-TTS: %d bytes -> %s", file_size, out_path)
                return out_path
            else:
                logger.error("F5-TTS error: No audio file in result")
                return None
        except Exception as e:
            # Reset client on error so it reconnects next time
            self._f5_client = None
            logger.error("F5-TTS error: %s", e)
            return None

    def reload(self):
        """Re-reads config from environment."""
        self._load_config()
        self._available = None
        self._f5_client = None
        self._f5_ref_text_cache.clear()
        self._f5_current_language = None

    def status_info(self) -> Dict[str, Any]:
        """Returns status info for availability summary."""
        if self.backend == "comfyui":
            url = ",".join(n for n, _u, _v in self.comfyui_candidates) or self.comfyui_skill
            voice = f"comfyui-tts ({self.comfyui_mode})"
        elif self.backend == "f5":
            url = self.f5_url
            if self._f5_language_models:
                langs = [k for k in self._f5_language_models if k != "_legacy"]
                if langs:
                    model_name = f"F5-TTS ({','.join(langs)})"
                else:
                    ckpt = self._f5_language_models.get("_legacy", {}).get("ckpt", "")
                    model_name = ckpt.split("/")[-2] if ckpt else "F5-TTS"
            else:
                model_name = "F5-TTS"
            voice = f"ref_audio ({model_name})"
        elif self.backend == "magpie":
            url = self.magpie_url
            voice = self.magpie_voice
        elif self.backend == "xtts":
            url = self.xtts_url
            voice = "speaker_wav"
        else:
            url = ""
            voice = ""
        return {
            "enabled": self.enabled,
            "available": self._available if self._available is not None else False,
            "backend": self.backend,
            "fallback_backend": self.fallback_backend or None,
            "url": url,
            "voice": voice,
            "auto": self.auto,
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_tts_service: Optional[TTSService] = None


def get_tts_service() -> TTSService:
    """Returns the global TTSService singleton."""
    global _tts_service
    if _tts_service is None:
        _tts_service = TTSService()
    return _tts_service


def initialize_tts_service() -> TTSService:
    """Initializes the TTS service and checks availability. Called at startup."""
    global _tts_service
    _tts_service = TTSService()
    _tts_service.is_available()
    return _tts_service


def reload_tts_service() -> Dict[str, Any]:
    """Reloads the TTS service from .env."""
    global _tts_service
    if _tts_service is None:
        _tts_service = TTSService()
    _tts_service.reload()
    _tts_service.is_available()
    return _tts_service.status_info()


# ---------------------------------------------------------------------------
# Chunked TTS Handler — deduplizierte Streaming-TTS-Logik
# ---------------------------------------------------------------------------

class ChunkedTTSHandler:
    """Handles chunked TTS generation during SSE streaming.

    Usage in a streaming endpoint::

        handler = ChunkedTTSHandler(agent_config, require_auto=True)
        async for event in agent.stream(...):
            if isinstance(event, ContentEvent):
                for sse in handler.feed(event.content):
                    yield sse
        for sse in await handler.flush():
            yield sse
    """

    def __init__(self, agent_config: Dict[str, Any], *, require_auto: bool = False):
        self.enabled = False
        self._svc = None
        self._cfg: Dict[str, Any] = {}
        self._buffer = ""
        self._tasks: List[Tuple[asyncio.Task, int]] = []
        self._idx = 0
        try:
            svc = get_tts_service()
            if svc.enabled and svc.chunk_size > 0:
                cfg = svc.get_character_config(agent_config)
                if cfg.get("enabled", True) and (not require_auto or cfg.get("auto", False)):
                    self.enabled = True
                    self._svc = svc
                    self._cfg = cfg
        except Exception as e:
            logger.error("TTS Chunk init error: %s", e)

    def _gen_task(self, text: str) -> asyncio.Task:
        return asyncio.create_task(asyncio.to_thread(
            self._svc.generate,
            text=text,
            voice=self._cfg.get("voice", ""),
            speaker_wav=self._cfg.get("speaker_wav", ""),
            language=self._cfg.get("language", "de"),
            voice_description=self._cfg.get("voice_description", ""),
            character_name=self._cfg.get("character_name", ""),
            comfyui_mode=self._cfg.get("comfyui_mode", "")))

    @staticmethod
    def _audio_sse(url: str, index: int, final: bool) -> str:
        return f"data: {json.dumps({'audio_chunk': {'url': url, 'index': index, 'final': final}})}\n\n"

    def feed(self, text: str) -> List[str]:
        """Feed new text, return list of ready SSE audio chunks."""
        if not self.enabled:
            return []
        self._buffer += text
        cleaned = clean_text_for_tts(self._buffer)
        chunk, rest = split_tts_chunk(cleaned, self._svc.chunk_size)
        if chunk and chunk.strip():
            self._buffer = rest
            idx = self._idx
            self._idx += 1
            self._tasks.append((self._gen_task(chunk), idx))

        results = []
        while self._tasks and self._tasks[0][0].done():
            t, i = self._tasks.pop(0)
            try:
                apath = t.result()
                if apath:
                    results.append(self._audio_sse(f"/tts/tmp/{apath.name}", i, False))
            except Exception as e:
                logger.error("TTS Chunk error chunk %d: %s", i, e)
        return results

    async def flush(self) -> List[str]:
        """Flush remaining buffer and await pending tasks. Returns SSE strings."""
        if not self.enabled:
            return []
        remaining = clean_text_for_tts(self._buffer.strip())
        if remaining.strip():
            idx = self._idx
            self._idx += 1
            self._tasks.append((self._gen_task(remaining), idx))
        self._buffer = ""

        results = []
        for t, i in self._tasks:
            try:
                apath = await t
                is_last = (i == self._tasks[-1][1])
                if apath:
                    results.append(self._audio_sse(f"/tts/tmp/{apath.name}", i, is_last))
            except Exception as e:
                logger.error("TTS Chunk error chunk %d: %s", i, e)
        self._tasks = []
        return results
