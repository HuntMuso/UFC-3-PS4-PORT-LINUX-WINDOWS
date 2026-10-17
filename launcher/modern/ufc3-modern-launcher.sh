#!/usr/bin/env bash
# Linux: starts the modern launcher (Python 3.8+ and Chrome, Chromium or Edge for the app window).
here=$(cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$here/ufc3_modern_launcher.py" "$@"
