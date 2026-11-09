#!/bin/bash
# Install the vendored Silero VAD model (k2-fsa/sherpa-onnx release asset "asr-models/silero_vad.onnx",
# MIT licensed) without any network access, and verify its exact bytes.
#   scripts/install_vad_model.sh [destination]   (default: ./silero_vad.onnx)
set -euo pipefail
cd "$(dirname "$0")/.."
SHA256=9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6
dest="${1:-silero_vad.onnx}"
src=assets/models/silero_vad.onnx
actual=$(sha256sum "$src" 2>/dev/null || shasum -a 256 "$src")
[ "${actual%% *}" = "$SHA256" ] || { echo "vendored VAD model hash mismatch" >&2; exit 1; }
[ "$(cd "$(dirname "$dest")" && pwd)/$(basename "$dest")" = "$PWD/$src" ] || cp "$src" "$dest"
