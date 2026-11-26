#!/bin/sh
# Create the Python environment for clipper in ./.venv
# Run this once after cloning or moving the folder (virtual environments do not survive a move).
set -e
cd "$(dirname "$0")"

UV=""
for candidate in "$(command -v uv 2>/dev/null)" "$HOME/.local/bin/uv" "$HOME"/Library/Python/*/bin/uv; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then UV="$candidate"; break; fi
done

if [ -n "$UV" ]; then
  "$UV" venv --python 3.12 --clear .venv
  "$UV" pip install --python .venv/bin/python -e ".[voices,dev]"
else
  PY="$(command -v python3.12 || command -v python3.13 || command -v python3.11 || true)"
  if [ -z "$PY" ]; then
    echo "clipper needs Python 3.11 or newer. Install uv (python3 -m pip install --user uv) or Python 3.12, then run this again." >&2
    exit 1
  fi
  rm -rf .venv
  "$PY" -m venv .venv
  .venv/bin/python -m pip install -e ".[voices,dev]"
fi

# Chromium renders the overlay templates. If Google Chrome is installed it is used as a fallback.
.venv/bin/python -m playwright install chromium || echo "Could not install Chromium; overlays will use Google Chrome if it is installed."

.venv/bin/clipper doctor
echo
echo "Ready. Try: .venv/bin/clipper polish /path/to/video.mp4 --preview"
