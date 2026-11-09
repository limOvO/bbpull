#!/bin/sh
# bbpull launcher - run `./bbpull.sh <subcommand>`, or `./bbpull.sh` for the menu.
#
# Mirrors bbpull.cmd: prefers the project virtual environment (.venv) so a
# machine with several Pythons always gets the same dependencies and the same
# GUI engine. Even if this file falls through to a bare python3, bbpull re-execs
# itself into .venv once it exists, so the environment cannot drift.
#
# Credentials are never stored in this file; the program asks and can save them
# in the desktop keyring (or an owner-only file) - see `./bbpull.sh doctor`.
set -u

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

VENV_PY="$HERE/.venv/bin/python"
if [ -x "$VENV_PY" ]; then
    PY="$VENV_PY"
elif [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    PY=python
fi

exec "$PY" -m bbpull "$@"
