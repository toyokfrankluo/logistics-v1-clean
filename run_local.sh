#!/bin/sh
# Install and launch with the same interpreter; never use the legacy run.sh.
set -eu
cd "$(dirname "$0")"
LOCAL_PYTHON="${LOGISTICS_PYTHON:-python3.11}"
if ! command -v "$LOCAL_PYTHON" >/dev/null 2>&1; then
    echo "Python 3.11 未安装，请安装或设置 LOGISTICS_PYTHON" >&2
    exit 1
fi
"$LOCAL_PYTHON" -c 'import sys; assert sys.version_info[:2] == (3, 11), "Python 3.11 is required"'
"$LOCAL_PYTHON" -m venv .venv-local
.venv-local/bin/python -m pip install -r requirements-test.txt
.venv-local/bin/python -m pip check
exec .venv-local/bin/python -B dev_local.py
