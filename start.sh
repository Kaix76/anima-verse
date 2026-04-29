#!/usr/bin/env bash
#
# Start/stop script for Agent System services
#
# Usage:
#   ./start.sh                       Start both services (default storage: ./storage)
#   ./start.sh --world demo          Start with storage: ./worlds/demo
#   ./start.sh --storage /path/to/x  Start with custom storage directory
#   ./start.sh --main                Start only main app
#   ./start.sh --face-service        Start only face service
#   ./start.sh --stop                Stop all services
#   ./start.sh --stop --main         Stop only main app
#   ./start.sh --stop --face-service Stop only face service
#   ./start.sh --restart             Restart all services
#   ./start.sh --status              Show running services

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="$SCRIPT_DIR/.pids"
LOG_DIR="$SCRIPT_DIR/logs"
ARCHIVE_DIR="$SCRIPT_DIR/logs/archive"
LOCK_DIR="$PID_DIR/start.lock"  # Atomares Lock via mkdir

MAIN_PID="$PID_DIR/main.pid"
FACE_PID="$PID_DIR/face.pid"

MAIN_LOG="$LOG_DIR/main.log"
FACE_LOG="$LOG_DIR/face.log"

mkdir -p "$PID_DIR" "$LOG_DIR" "$ARCHIVE_DIR"

# ── Virtualenv aktivieren (falls vorhanden) ───────────────────────────────────
# POSIX-Layout:   .venv/bin/activate          (Linux, macOS, Git-Bash on Windows)
# Windows-Layout: .venv/Scripts/activate      (native Python unter Windows)
if [[ -f "$SCRIPT_DIR/.venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.venv/bin/activate"
elif [[ -f "$SCRIPT_DIR/.venv/Scripts/activate" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.venv/Scripts/activate"
fi

# ── Lock ──────────────────────────────────────────────────────────────────────
# Verhindert parallele start.sh Aufrufe (mkdir ist atomar)

acquire_lock() {
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "[start] Bereits aktiv – anderer start.sh Prozess laeuft noch."
        echo "[start] Falls nicht: rm -rf $LOCK_DIR"
        exit 1
    fi
    trap 'rm -rf "$LOCK_DIR"' EXIT
}

# ── Helpers ───────────────────────────────────────────────────────────────────

rotate_log() {
    local log_file="$1"
    if [[ -f "$log_file" && -s "$log_file" ]]; then
        local timestamp
        timestamp=$(date +%Y%m%d_%H%M%S)
        cp "$log_file" "$ARCHIVE_DIR/$(basename "${log_file%.log}_${timestamp}.log")"
        : > "$log_file"
        echo "[logs] Rotated $(basename "$log_file") -> archive/"
    fi
}

is_running() {
    local pid_file="$1"
    if [[ -f "$pid_file" ]]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        # Stale PID file
        rm -f "$pid_file"
    fi
    return 1
}

# Wartet bis ein Prozess wirklich beendet ist (nicht nur Port freigegeben hat).
# Gibt 0 zurueck wenn tot, 1 wenn Timeout.
wait_for_death() {
    local pid="$1"
    local max_secs="${2:-8}"
    local i=0
    while kill -0 "$pid" 2>/dev/null; do
        ((i++)) || true
        if (( i > max_secs * 2 )); then
            return 1
        fi
        sleep 0.5
    done
    return 0
}

# Beendet alle Prozesse auf einem Port und wartet bis sie WIRKLICH tot sind.
# Erst danach ist es sicher, rotate_log aufzurufen.
kill_port() {
    local port="$1"
    local name="$2"
    local pids
    pids=$(lsof -ti :"$port" 2>/dev/null || true)
    [[ -z "$pids" ]] && return

    for pid in $pids; do
        [[ "$pid" == "$$" ]] && continue
        echo "[$name] Stopping process on port $port (PID $pid)..."
        kill "$pid" 2>/dev/null || true
    done

    # Auf vollstaendigen Tod warten – verhindert, dass Shutdown-Logs
    # in die soeben geleerte neue Log-Datei geschrieben werden
    for pid in $pids; do
        [[ "$pid" == "$$" ]] && continue
        if ! wait_for_death "$pid" 8; then
            echo "[$name] Force killing PID $pid..."
            kill -9 "$pid" 2>/dev/null || true
            sleep 0.3
        fi
    done
}

start_main() {
    if is_running "$MAIN_PID"; then
        echo "[main] Already running (PID $(cat "$MAIN_PID"))"
        return
    fi
    # Erst alten Prozess vollstaendig beenden, DANN Log rotieren
    kill_port 8000 "main"
    rotate_log "$MAIN_LOG"
    echo "[main] Starting main app on port 8000..."
    cd "$SCRIPT_DIR"
    nohup "$SCRIPT_DIR/.venv/bin/python" -m uvicorn app.server:app --host 0.0.0.0 --port 8000 \
        >> "$MAIN_LOG" 2>&1 &
    local pid=$!
    echo "$pid" > "$MAIN_PID"
    echo "[main] Started (PID $pid, log: $MAIN_LOG)"
}

start_face() {
    if is_running "$FACE_PID"; then
        echo "[face-service] Already running (PID $(cat "$FACE_PID"))"
        return
    fi
    local port="${FACE_SERVICE_PORT:-8005}"
    kill_port "$port" "face-service"
    rotate_log "$FACE_LOG"
    echo "[face-service] Starting face service on port $port..."
    cd "$SCRIPT_DIR"
    nohup "$SCRIPT_DIR/.venv/bin/python" -m uvicorn face_service.server:app --host 0.0.0.0 --port "$port" \
        >> "$FACE_LOG" 2>&1 &
    local pid=$!
    echo "$pid" > "$FACE_PID"
    echo "[face-service] Started (PID $pid, log: $FACE_LOG)"
}

stop_service() {
    local name="$1"
    local pid_file="$2"
    if is_running "$pid_file"; then
        local pid
        pid=$(cat "$pid_file")
        echo "[$name] Stopping (PID $pid)..."
        kill "$pid" 2>/dev/null || true
        if ! wait_for_death "$pid" 8; then
            echo "[$name] Force killing..."
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$pid_file"
        echo "[$name] Stopped"
    else
        echo "[$name] Not running"
    fi
}

show_status() {
    echo "=== Service Status ==="
    if is_running "$MAIN_PID"; then
        echo "[main]         Running (PID $(cat "$MAIN_PID"))"
    else
        echo "[main]         Stopped"
    fi
    if is_running "$FACE_PID"; then
        echo "[face-service] Running (PID $(cat "$FACE_PID"))"
    else
        echo "[face-service] Stopped"
    fi
}

# ── Parse arguments ───────────────────────────────────────────────────────────

ACTION="start"
TARGET="all"
STORAGE_ARG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --stop)          ACTION="stop"; shift ;;
        --restart)       ACTION="restart"; shift ;;
        --status)        ACTION="status"; shift ;;
        --main)          TARGET="main"; shift ;;
        --face-service)  TARGET="face"; shift ;;
        --world)
            STORAGE_ARG="$SCRIPT_DIR/worlds/$2"
            shift 2 ;;
        --storage)
            STORAGE_ARG="$2"
            shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--stop|--restart|--status] [--main|--face-service] [--world NAME|--storage PATH]"
            echo ""
            echo "  (no flags)       Start both services (storage: ./storage)"
            echo "  --world NAME     Use ./worlds/NAME as storage directory"
            echo "  --storage PATH   Use custom storage directory"
            echo "  --main           Target only main app"
            echo "  --face-service   Target only face service"
            echo "  --stop           Stop services"
            echo "  --restart        Restart services"
            echo "  --status         Show service status"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Export storage directory for Python app
if [[ -n "$STORAGE_ARG" ]]; then
    export STORAGE_DIR="$STORAGE_ARG"
    echo "[config] Storage directory: $STORAGE_DIR"
fi

# ── Load specific config from .env ────────────────────────────────────────────
# Only extract specific keys we need (source would fail on unquoted values with spaces)

if [[ -f "$SCRIPT_DIR/.env" ]]; then
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$key" ]] && continue
        key=$(echo "$key" | xargs)
        case "$key" in
            FACE_SERVICE_PORT) export FACE_SERVICE_PORT="$value" ;;
        esac
    done < "$SCRIPT_DIR/.env"
fi

# ── Face-Service Settings aus storage/<world>/config.json laden ───────────────
# Der Face-Service ist ein eigenstaendiger Prozess und sieht den os.environ der
# Main-App nicht. Damit er das konfigurierte Swap-Modell findet, muessen die
# FACE_SERVICE_*-Variablen vor dem Start exportiert werden.
_face_config_json=""
if [[ -n "$STORAGE_ARG" && -f "$STORAGE_ARG/config.json" ]]; then
    _face_config_json="$STORAGE_ARG/config.json"
elif [[ -f "$SCRIPT_DIR/storage/config.json" ]]; then
    _face_config_json="$SCRIPT_DIR/storage/config.json"
fi
if [[ -n "$_face_config_json" ]] && command -v python3 >/dev/null 2>&1; then
    while IFS='=' read -r k v; do
        [[ -z "$k" ]] && continue
        export "$k=$v"
    done < <(python3 - "$_face_config_json" <<'PY'
import json, os, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        cfg = json.load(f)
except Exception:
    sys.exit(0)
fs = cfg.get("faceswap") or {}
mapping = {
    "service_url": "FACE_SERVICE_URL",
    "service_port": "FACE_SERVICE_PORT",
    "service_model_path": "FACE_SERVICE_MODEL_PATH",
    "service_det_size": "FACE_SERVICE_DET_SIZE",
    "service_omp_num_threads": "FACE_SERVICE_OMP_NUM_THREADS",
    "service_enabled": "FACE_SERVICE_ENABLED",
    "service_debug": "FACE_SERVICE_DEBUG",
}
for src, env in mapping.items():
    if src in fs and fs[src] not in (None, ""):
        print(f"{env}={fs[src]}")
fe = cfg.get("face_enhance") or {}
if fe.get("model_path"):
    print(f"FACE_ENHANCE_MODEL_PATH={fe['model_path']}")
PY
)
    if [[ -n "${FACE_SERVICE_MODEL_PATH:-}" ]]; then
        echo "[config] Face-Service Modell: $FACE_SERVICE_MODEL_PATH"
    fi
fi

# ── Execute ───────────────────────────────────────────────────────────────────

case "$ACTION" in
    start)
        acquire_lock
        case "$TARGET" in
            all)
                start_face   # Face service first (so it's ready when main needs it)
                sleep 1
                start_main
                echo ""
                echo "==> Browser: http://localhost:8000"
                echo "==> Admin:   http://localhost:8000/admin"
                ;;
            main)
                start_main
                echo ""
                echo "==> Browser: http://localhost:8000"
                echo "==> Admin:   http://localhost:8000/admin"
                ;;
            face)  start_face ;;
        esac
        ;;
    stop)
        case "$TARGET" in
            all)
                stop_service "main" "$MAIN_PID"
                stop_service "face-service" "$FACE_PID"
                ;;
            main)  stop_service "main" "$MAIN_PID" ;;
            face)  stop_service "face-service" "$FACE_PID" ;;
        esac
        ;;
    restart)
        acquire_lock
        case "$TARGET" in
            all)
                echo "[restart] Restarting all services..."
                stop_service "main" "$MAIN_PID"
                stop_service "face-service" "$FACE_PID"
                sleep 1
                start_face
                sleep 1
                start_main
                echo ""
                echo "==> Browser: http://localhost:8000"
                echo "==> Admin:   http://localhost:8000/admin"
                ;;
            main)
                echo "[restart] Restarting main app..."
                stop_service "main" "$MAIN_PID"
                sleep 1
                start_main
                echo ""
                echo "==> Browser: http://localhost:8000"
                echo "==> Admin:   http://localhost:8000/admin"
                ;;
            face)
                echo "[restart] Restarting face service..."
                stop_service "face-service" "$FACE_PID"
                sleep 1
                start_face
                ;;
        esac
        ;;
    status)
        show_status
        ;;
esac
