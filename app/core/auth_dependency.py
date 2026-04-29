"""FastAPI Dependencies fuer Auth (Multiuser Phase 1).

Nutzung in Routes:

    from app.core.auth_dependency import get_current_user, require_admin

    @router.get("/protected")
    def foo(user = Depends(get_current_user)): ...

    @router.get("/admin-only")
    def bar(user = Depends(require_admin)): ...

Zusaetzlich contextvar `current_user_ctx` — gesetzt durch Middleware,
lesbar aus beliebigem Code (get_current_user_from_ctx) ohne Request.
"""
from contextvars import ContextVar
from typing import Optional, Dict, Any
from fastapi import Request, HTTPException, status

from app.core import sessions, users
from app.core.log import get_logger

logger = get_logger("auth_dep")

current_user_ctx: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "current_user_ctx", default=None
)


def get_current_user_from_ctx() -> Optional[Dict[str, Any]]:
    """Liefert den aktuellen User aus dem Request-Context (via Middleware).
    None wenn kein Request-Context (z.B. Background-Task)."""
    return current_user_ctx.get()


def _get_session_user(request: Request) -> Optional[Dict[str, Any]]:
    token = request.cookies.get(sessions.SESSION_COOKIE_NAME)
    path = request.url.path
    if not token:
        if path.startswith("/world-dev/") or path.startswith("/admin/"):
            cookie_keys = list(request.cookies.keys())
            logger.warning("auth: kein Session-Cookie bei %s (verfuegbare cookies=%s)",
                           path, cookie_keys)
        return None
    sess = sessions.get_session(token)
    if not sess:
        if path.startswith("/world-dev/") or path.startswith("/admin/"):
            logger.warning("auth: Session-Token unbekannt/abgelaufen bei %s (token=%s...)",
                           path, token[:8])
        return None
    user = users.get_user_by_id(sess["user_id"])
    if not user and (path.startswith("/world-dev/") or path.startswith("/admin/")):
        logger.warning("auth: Session ok aber User_id %s nicht in DB bei %s",
                       sess["user_id"], path)
    return user


def get_current_user(request: Request) -> Dict[str, Any]:
    """Dependency: liefert den eingeloggten User oder 401."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Not authenticated")
    return user


def get_current_user_optional(request: Request) -> Optional[Dict[str, Any]]:
    """Dependency: liefert User oder None (kein 401)."""
    return _get_session_user(request)


def require_admin(request: Request) -> Dict[str, Any]:
    """Dependency: erzwingt Admin-Rolle."""
    user = get_current_user(request)
    if user.get("role") != users.ROLE_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Admin role required")
    return user


def filter_characters(request: Request, character_names):
    """Filtert eine Character-Liste nach Zugriffsrechten des aktuellen Users.

    User ohne Login: leere Liste. Sonst nur zugeordnete Characters — auch
    fuer Admins.
    """
    user = get_current_user_optional(request)
    if not user:
        return []
    allowed = set(user.get("allowed_characters") or [])
    return [c for c in character_names if c in allowed]


def user_can_access_character(request: Request, character_name: str) -> bool:
    """True wenn der aktuelle User den Character in allowed_characters hat."""
    user = get_current_user_optional(request)
    if not user:
        return False
    return character_name in (user.get("allowed_characters") or [])


async def user_context_middleware(request: Request, call_next):
    """Setzt current_user_ctx aus Session-Cookie fuer die Dauer des Requests.

    Character-Access-Policy:
    - Admin: sieht und aendert alles (kein Character-Filter)
    - User: darf Bilder/Expression/Basis-Zustaende aller Characters lesen
            (Profilbild + Expression sichtbar), aber keine sensiblen Daten
            (Profil, Schedule, Wissen, Memories, Secrets, Diary, Inventar)
    - User Write-Operations: nur auf zugeordnete (allowed_characters) Chars
    - allowed_characters gilt primaer fuer Avatar-Auswahl (siehe /account)
    """
    from fastapi.responses import JSONResponse

    user = _get_session_user(request)
    token = current_user_ctx.set(user)
    try:
        if user and user.get("role") != users.ROLE_ADMIN:
            path = request.url.path
            method = request.method.upper()
            chars = _extract_characters_from_path(path)
            if chars:
                allowed = set(user.get("allowed_characters") or [])
                is_write = method in ("POST", "PUT", "PATCH", "DELETE")
                is_sensitive = _is_sensitive_character_path(path)
                blocked_char = ""
                for c in chars:
                    if c in allowed:
                        continue
                    if is_write or is_sensitive:
                        blocked_char = c
                        break
                if blocked_char:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": f"Kein Zugriff auf Character '{blocked_char}'"},
                    )
        return await call_next(request)
    finally:
        current_user_ctx.reset(token)


# Sensible Pfade — nur mit allowed_characters (oder Admin) lesbar
_SENSITIVE_SEGMENTS = {
    "profile", "personality", "config", "appearance", "scheduler",
    "knowledge", "memories", "secrets", "diary", "assignments",
    "evolution", "generate-appearance", "generate-task",
    "thoughts", "notifications", "story-arcs", "soul",
}


def _is_sensitive_character_path(path: str) -> bool:
    """Prueft ob der Pfad sensible Character-Daten betrifft.

    - /characters/{name}/profile, /personality, /scheduler/*, /knowledge, ...
    - /inventory/characters/{name}/* — Inventar ist privat
    - /diary/*/{name}/* — Tagebuch privat
    - /relationships/... — Beziehungen privat
    """
    from urllib.parse import unquote
    parts = [unquote(p) for p in path.split("/") if p]

    if len(parts) >= 1 and parts[0] == "inventory":
        return True  # komplettes inventory-Subtree ist sensibel
    if len(parts) >= 1 and parts[0] == "diary":
        return True
    if len(parts) >= 1 and parts[0] == "relationships":
        return True
    if len(parts) >= 1 and parts[0] == "assignments":
        return True

    if len(parts) >= 3 and parts[0] == "characters":
        # /characters/{name}/{segment}
        seg = parts[2]
        if seg in _SENSITIVE_SEGMENTS:
            return True
    return False


def _extract_characters_from_path(path: str):
    """Extrahiert Character-Namen aus character-scoped URLs.

    Returns List[str] — alle Character-Namen die im Pfad referenziert werden
    (z.B. /relationships/A/B → [A, B]).

    Matched:
      /characters/{name}/*
      /inventory/characters/{name}/*
      /diary/{user_id}/{name}/*
      /assignments-for-character/{name}/* (falls existiert)
      /relationships/{a}/{b}
    """
    from urllib.parse import unquote
    parts = [unquote(p) for p in path.split("/") if p]
    result = []

    reserved = {
        "list", "chatbots", "at-location", "animate", "available-models",
        "outfit-rules", "outfit-lora-options",
        "graph", "migrate", "backfill", "",
    }

    if len(parts) >= 2 and parts[0] == "characters":
        cand = parts[1]
        if cand not in reserved:
            result.append(cand)
    elif len(parts) >= 3 and parts[0] == "inventory" and parts[1] == "characters":
        result.append(parts[2])
    elif len(parts) >= 3 and parts[0] == "diary":
        # /diary/{user_id}/{name}
        cand = parts[2]
        if cand not in reserved:
            result.append(cand)
    elif len(parts) >= 3 and parts[0] == "relationships":
        # /relationships/{a}/{b}
        for c in parts[1:3]:
            if c not in reserved:
                result.append(c)
    return result


