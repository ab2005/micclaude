#!/usr/bin/env bash
# Get from a fresh clone to talking, in one command.
#
#   ./start.sh              English, local Whisper
#   ./start.sh ru           Russian
#   ./start.sh ru whisper.cpp   Russian on Metal (needs: brew install whisper-cpp)
#
# Everything it installs stays inside .venv in this directory.
set -euo pipefail

cd "$(dirname "$0")"
LANGUAGE="${1:-en}"
BACKEND="${2:-faster-whisper}"

command -v claude >/dev/null || {
  echo "error: the Claude Code CLI is not on PATH." >&2
  echo "       Install it from https://claude.com/claude-code, then run 'claude' once to sign in." >&2
  exit 1
}

if [ ! -d .venv ]; then
  echo "==> creating .venv"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if ! python3 -c "import faster_whisper" 2>/dev/null && [ "$BACKEND" = "faster-whisper" ]; then
  echo "==> installing the speech model runtime (a few hundred MB, once)"
  pip install -q -r requirements.txt
fi

if [ "$BACKEND" = "whisper.cpp" ]; then
  command -v whisper-server >/dev/null || {
    echo "error: whisper-server is not on PATH. On a Mac: brew install whisper-cpp" >&2
    exit 1
  }
  MODEL="${MICCLAUDE_MODEL:-small}"
  BIN="$HOME/.cache/whisper.cpp/ggml-${MODEL}.bin"
  if [ ! -f "$BIN" ]; then
    echo "==> downloading ggml-${MODEL}.bin (once)"
    mkdir -p "$(dirname "$BIN")"
    curl -L --progress-bar -o "$BIN" \
      "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${MODEL}.bin"
  fi
fi

echo "==> starting micclaude (${LANGUAGE}, ${BACKEND}); the first run downloads the model"
exec env PYTHONPATH=server python3 -m micclaude --lang "$LANGUAGE" --backend "$BACKEND" "${@:3}"
