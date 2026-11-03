#!/usr/bin/env bash
set -eu
cd -- "$(dirname -- "$0")"
if [ ! -x .venv/bin/python ]; then
  echo 'Please run: bash install.sh'
  exit 1
fi
exec .venv/bin/python scripts/portable.py check
