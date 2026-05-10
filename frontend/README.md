# Game Admin (React)

Standalone admin SPA served by FastAPI at `/game-admin`. Tabs ported here
from the legacy `#game-admin-modal` in `templates/index.html` so UI changes
no longer require a Python server restart — the React build writes to
`../static/game_admin/`, which the FastAPI server serves directly via the
existing `/static` mount.

See `development_instructions/plan-game-admin-redesign.md` for the full
plan and migration order.

## Workflow

```bash
# one-time
cd frontend && npm install

# production build → ../static/game_admin/
npm run build

# dev server with HMR on http://localhost:5173/
# (proxies API calls to the Python server on :8000)
npm run dev
```

## i18n

Strings are written in **English** at the source and looked up via
`useI18n().t("English source")`. Missing translations log
`[i18n] missing [<lang>]: <source>` once per (lang, source) pair so the
i18n welle can sweep them into `shared/languages/<lang>.json`.

The `<I18nProvider>` mirrors the `t()` contract used by
`static/script.js` so a language switch in the main app surfaces here
after a reload.

## Stack

- Vite 5 (Node 18 compatible — Vite 6+ requires Node 20)
- React 18 + TypeScript
- No state-management framework — `useState` + `useReducer` only.
- No UI framework — theme variables come from
  `static/themes/{base,dark}.css`, loaded via `<link>` tags in
  `index.html`.
