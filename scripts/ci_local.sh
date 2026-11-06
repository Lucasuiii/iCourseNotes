#!/bin/bash
# Run the same test suite as .github/workflows/ci.yml on this Mac, in a clean environment.
#   scripts/ci_local.sh                 # full suite
#   scripts/ci_local.sh tests.test_x    # selected modules
# Python: $CI_PYTHON, else the shared local environment next to the runtime, else python3.
# Needs ffmpeg, node (PATH or nvm) and Homebrew cairo/pango for the PDF tests.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${CI_PYTHON:-}"
if [ -z "$PY" ]; then
  for candidate in ../../qwen-local-asr-test-env/bin/python ../qwen-local-asr-test-env/bin/python; do
    [ -x "$candidate" ] && PY="$(cd "$(dirname "$candidate")" && pwd)/python" && break
  done
fi
PY="${PY:-$(command -v python3)}"

NODE_DIR="$(dirname "$(command -v node 2>/dev/null || true)")"
if [ "$NODE_DIR" = "." ] && [ -d "$HOME/.nvm/versions/node" ]; then
  NODE_DIR="$(ls -d "$HOME"/.nvm/versions/node/*/bin | sort -V | tail -1)"
fi
BREW="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
for tool in ffmpeg; do command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 2; }; done
[ -x "$NODE_DIR/node" ] || { echo "missing: node (install it or use nvm)" >&2; exit 2; }
[ -e node_modules ] || { echo "missing: node_modules (run: npm ci)" >&2; exit 2; }

HOME_DIR="$(mktemp -d)"; trap 'rm -rf "$HOME_DIR"' EXIT
echo "python: $PY"; echo "node:   $NODE_DIR/node"
# No inherited credentials or user configuration, like a fresh CI runner.
exec env -i PATH="$NODE_DIR:$BREW/bin:/usr/bin:/bin" HOME="$HOME_DIR" LANG=en_US.UTF-8 \
  DYLD_FALLBACK_LIBRARY_PATH="$BREW/lib" "$PY" -m unittest ${@:-discover -s tests}
