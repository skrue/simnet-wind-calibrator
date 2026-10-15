#!/bin/sh
# Run the wind calibrator using an isolated local Python environment.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"
PYTHON=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
  echo "Creating Python virtual environment in .venv ..."
  "$PYTHON" -m venv .venv
fi
if ! .venv/bin/python -c 'import prompt_toolkit' >/dev/null 2>&1; then
  echo "Installing dependencies ..."
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python wind_calibrator.py "$@"
