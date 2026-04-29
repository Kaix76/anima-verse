"""Authentication-Routes (Multiuser Phase 1).

Cookie-basierte Sessions. Login setzt HttpOnly-Cookie, Logout loescht es.
"""
from typing import Dict, Any
from fastapi import APIRouter, Request, Response, HTTPException, Depends, status

from app.core.log import get_logger
from app.core import sessions, users
from app.core.auth_dependency import (
    get_current_user, get_current_user_optional, require_admin)

logger = get_logger("auth")

router = APIRouter(prefix="/auth", tags=["authentication"])


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=sessions.SESSION_COOKIE_NAME,
        value=token,
        max_age=sessions.SESSION_TTL_HOURS * 3600,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(sessions.SESSION_COOKIE_NAME, path="/")


@router.post("/login")
async def login(request: Request, response: Response) -> Dict[str, Any]:
    """Loggt einen User ein und setzt Session-Cookie."""
    data = await request.json()
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="Benutzername und Passwort erforderlich")

    user = users.check_user_password(username, password)
    if not user:
        raise HTTPException(status_code=401, detail="Ungueltige Anmeldedaten")

    token = sessions.create_session(user["id"])
    users.touch_last_login(user["id"])
    _set_session_cookie(response, token)
    logger.info("Login: %s (role=%s)", user["username"], user["role"])

    return {
        "status": "success",
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "allowed_characters": user["allowed_characters"],
        },
    }


@router.post("/logout")
def logout(request: Request, response: Response) -> Dict[str, Any]:
    """Loggt aus — Session serverseitig loeschen + Cookie clearen."""
    token = request.cookies.get(sessions.SESSION_COOKIE_NAME)
    if token:
        sessions.delete_session(token)
    _clear_session_cookie(response)
    return {"status": "success"}


@router.get("/me")
def me(user = Depends(get_current_user)) -> Dict[str, Any]:
    """Liefert den aktuell eingeloggten User."""
    return {"user": user}


@router.get("/status")
def auth_status(user = Depends(get_current_user_optional)) -> Dict[str, Any]:
    """Status ohne 401 — Frontend prueft ob Login noetig."""
    return {"authenticated": user is not None, "user": user}


# ── User-Verwaltung (Admin-only) ──────────────────────────────────────

@router.get("/users")
def list_users_route(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return {"status": "success", "users": users.list_users()}


@router.post("/users")
async def create_user_route(
    request: Request,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    data = await request.json()
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    role = (data.get("role") or "user").strip()
    allowed = data.get("allowed_characters") or []
    try:
        user_id = users.create_user(username, password, role=role,
                                    allowed_characters=allowed)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "user_id": user_id}


@router.patch("/users/{user_id}")
async def update_user_route(
    user_id: str, request: Request,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    data = await request.json()
    password = data.pop("password", None)
    try:
        if password:
            users.set_user_password(user_id, password)
        updated = users.update_user(user_id, **data) if data else True
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not updated and not password:
        raise HTTPException(status_code=404, detail="User nicht gefunden")
    return {"status": "success"}


@router.delete("/users/{user_id}")
def delete_user_route(
    user_id: str,
    current: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    if current["id"] == user_id:
        raise HTTPException(status_code=400, detail="Eigener Account nicht loeschbar")
    target = users.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User nicht gefunden")
    # Letzten Admin nicht loeschen
    if target.get("role") == users.ROLE_ADMIN:
        admins = [u for u in users.list_users() if u.get("role") == users.ROLE_ADMIN]
        if len(admins) <= 1:
            raise HTTPException(status_code=400,
                                detail="Letzter Admin kann nicht geloescht werden")
    users.delete_user(user_id)
    sessions.delete_sessions_for_user(user_id)
    return {"status": "success"}
