#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
cd "$SCRIPT_DIR"
if [ ! -x .venv/bin/python ]; then
    echo "Create .venv and install requirements.txt first. See README.md."
    exit 1
fi
exec .venv/bin/python -m geo_workbench --open "$@"
