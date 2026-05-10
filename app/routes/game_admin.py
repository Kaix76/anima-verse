"""Game-Admin shell route.

Serves the React-built shell from ``static/game_admin/index.html``. All
behavior lives in the React app under ``frontend/`` (built via
``npm run build`` to ``static/game_admin/``); this Python route exists only
to gate the page behind admin auth and to return the bundled shell.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from app.core.auth_dependency import require_admin

router = APIRouter()

_SHELL_PATH = Path("static/game_admin/index.html")

_DEV_HINT = """<!doctype html>
<html><head><meta charset="utf-8"><title>Game Admin — build missing</title>
<style>body{font-family:system-ui,sans-serif;background:#0d1117;color:#c9d1d9;
padding:32px;max-width:720px;margin:auto;line-height:1.5}
code{background:#161b22;padding:2px 6px;border-radius:4px}
h1{color:#f85149}</style></head><body>
<h1>Game Admin build missing</h1>
<p>The React bundle at <code>static/game_admin/index.html</code> hasn&#39;t been
built yet. From the repo root, run:</p>
<pre><code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code></pre>
<p>For development with hot-reload, open
<a href="http://localhost:5173/" style="color:#58a6ff">http://localhost:5173/</a>
after starting <code>npm run dev</code> in the same directory — the Vite dev
server proxies API calls to this FastAPI server on :8000.</p>
</body></html>"""


@router.get("/game-admin", include_in_schema=False)
async def game_admin_page(user=Depends(require_admin)):
    if not _SHELL_PATH.is_file():
        return HTMLResponse(content=_DEV_HINT, status_code=503)
    return FileResponse(_SHELL_PATH)


@router.get("/game-admin/", include_in_schema=False)
async def game_admin_page_slash(user=Depends(require_admin)):
    if not _SHELL_PATH.is_file():
        return HTMLResponse(content=_DEV_HINT, status_code=503)
    return FileResponse(_SHELL_PATH)
