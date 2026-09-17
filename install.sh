#!/usr/bin/env bash
set -eu
cd -- "$(dirname -- "$0")"
for task_python in python3.13 python3.12 python3.11 python3.14 python3; do
  if command -v "$task_python" >/dev/null 2>&1 && "$task_python" -c 'import sys, struct; raise SystemExit(0 if (3,11) <= sys.version_info[:2] <= (3,14) and struct.calcsize("P") == 8 else 1)' >/dev/null 2>&1; then
    exec "$task_python" scripts/portable.py install
  fi
done
echo 'Please install Python 3.11-3.14 64-bit (recommended: 3.13).'
exit 1
