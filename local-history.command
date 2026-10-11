#!/bin/bash
# macOS convenience entry. No credentials appear in shell arguments or output.
set -euo pipefail
LOCAL_HISTORY_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$LOCAL_HISTORY_ROOT"
LOCAL_HISTORY_PYTHON="${LOCAL_HISTORY_PYTHON:-$LOCAL_HISTORY_ROOT/.venv-local/bin/python}"
if [ ! -x "$LOCAL_HISTORY_PYTHON" ]; then
  LOCAL_HISTORY_PYTHON="$LOCAL_HISTORY_ROOT/../../qwen-local-asr-test-env/bin/python"
fi
if [ ! -x "$LOCAL_HISTORY_PYTHON" ]; then
  echo '找不到运行环境，请按 docs/local-history-refresh.md 配置 LOCAL_HISTORY_PYTHON。'
  exit 2
fi
if [ "$#" -eq 0 ]; then
  exec "$LOCAL_HISTORY_PYTHON" -m scripts.local_history_refresh doctor
fi
exec "$LOCAL_HISTORY_PYTHON" -m scripts.local_history_refresh "$@"
