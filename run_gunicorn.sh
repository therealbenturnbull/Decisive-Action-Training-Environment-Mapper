#!/usr/bin/env sh
# Aim: Launch DATE Mapper through Gunicorn on macOS and Ubuntu.
# Author: Benjamin Turnbull

set -eu

PROJECT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"

HOST=${HOST:-127.0.0.1}
PORT=${PORT:-9099}
WORKERS=${GUNICORN_WORKERS:-${WEB_CONCURRENCY:-1}}
THREADS=${GUNICORN_THREADS:-4}
TIMEOUT=${GUNICORN_TIMEOUT:-120}
LOG_LEVEL=${GUNICORN_LOG_LEVEL:-info}

start_gunicorn() {
    exec "$@" \
        --chdir "$PROJECT_DIR" \
        --bind "$HOST:$PORT" \
        --worker-class gthread \
        --workers "$WORKERS" \
        --threads "$THREADS" \
        --timeout "$TIMEOUT" \
        --log-level "$LOG_LEVEL" \
        --access-logfile - \
        --error-logfile - \
        app:app
}

if [ -x "$VENV_PYTHON" ] && "$VENV_PYTHON" -c "import gunicorn" >/dev/null 2>&1; then
    start_gunicorn "$VENV_PYTHON" -m gunicorn
fi

if command -v gunicorn >/dev/null 2>&1; then
    start_gunicorn "$(command -v gunicorn)"
fi

printf '%s\n' \
    "Gunicorn is not installed." \
    "Create the virtual environment and install the project dependencies:" \
    "  python3 -m venv .venv" \
    "  .venv/bin/python -m pip install -r requirements.txt" >&2
exit 1
