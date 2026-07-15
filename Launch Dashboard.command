#!/bin/bash
# Double-click in Finder to open the dashboard in your browser.
# If it's already running, this just opens the browser to it. If not, it starts
# the server (this Terminal window then hosts it — close the window to stop it).
cd "$(dirname "$0")" || exit 1
URL="http://localhost:8501"

# Already running? Just open the browser and quit.
if curl -fsS "$URL/_stcore/health" >/dev/null 2>&1; then
    open "$URL"
    exit 0
fi

# First-time setup if needed.
if [ ! -x .venv/bin/tshirt-dash ]; then
    echo "Setting up for the first time..."
    python3 -m venv .venv && .venv/bin/pip install -e .
fi

# Start the server (Streamlit opens the browser itself on first launch).
exec .venv/bin/tshirt-dash
