#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
printf '
Installation completed. Run ./start_mac_linux.sh
'
