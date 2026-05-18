#!/usr/bin/env bash
# Headed Turnstile API on Linux without a physical monitor: Xvfb provides DISPLAY.
#
# Install (Debian/Ubuntu — use this on Vultr, etc.):
#   sudo apt update && sudo apt install -y xvfb
#   (package name is "xvfb", NOT xorg-server-xvfb — that is Arch-only)
# Install (Arch Linux):   sudo pacman -S xorg-server-xvfb
#
# Manual:
#   cd /path/to/captcha_bot && chmod +x scripts/start-headed-xvfb.sh
#   ./scripts/start-headed-xvfb.sh
#
# PM2 (headed + virtual display; API on all interfaces — adjust TURNSTILE_HOST if needed):
#   pm2 start scripts/start-headed-xvfb.sh --name captcha-bot --cwd /path/to/captcha_bot

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v xvfb-run >/dev/null 2>&1; then
  echo "xvfb-run not found. Install xvfb (see comments at top of this script)." >&2
  exit 1
fi

PY="${PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
  echo "Python not found at $PY — set PYTHON= or create .venv" >&2
  exit 1
fi

export TURNSTILE_HEADED="${TURNSTILE_HEADED:-1}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

# Virtual screen size (width x height x depth). Larger is fine if you have RAM.
XVFB_SCREEN="${XVFB_SCREEN:-1280x720x24}"

exec xvfb-run -a -s "-screen 0 ${XVFB_SCREEN}" "$PY" "$ROOT/main.py"
