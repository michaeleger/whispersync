#!/usr/bin/env bash
# One-time setup for whispersync on a Linux box: venv, faster-whisper, model download, self-test.
# Safe to rerun. Usage: ./setup.sh [model]   (default: large-v3-turbo)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL="${1:-${WS_MODEL:-large-v3-turbo}}"
cd "$DIR"

echo "== whispersync setup in $DIR"

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "Need Python 3.9 or newer." >&2; exit 1
fi

if ! command -v ffmpeg >/dev/null; then
  echo "Note: ffmpeg not found. whispersync will fall back to PyAV, but ffmpeg is sturdier: sudo apt install ffmpeg"
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "-- creating .venv"
  if ! python3 -m venv .venv 2>/dev/null; then
    echo "python3-venv is missing. Run: sudo apt install python3-venv   then rerun ./setup.sh" >&2
    exit 1
  fi
fi

echo "-- installing faster-whisper and google-genai"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q "faster-whisper>=1.1" "google-genai>=1.0"

echo "-- downloading model $MODEL (one time, roughly 1.5 GB for large-v3-turbo)"
mkdir -p models work
.venv/bin/python - "$MODEL" <<'PY'
import sys
from faster_whisper import WhisperModel
WhisperModel(sys.argv[1], device="cpu", compute_type="int8", cpu_threads=4, download_root="models")
print("   model ready")
PY

chmod +x whispersync whispersync.py
echo "-- self-test"
./whispersync selftest

if .venv/bin/python -c "import sys; sys.path.insert(0, '.'); import whispersync as w; sys.exit(0 if w.gemini_key() else 1)"; then
  echo "-- Gemini key found: the Google third opinion is on"
else
  echo "-- No Gemini key yet. For the Google third opinion, add a line to $DIR/.env:"
  echo "     GEMINI_API_KEY=your-key-from-aistudio.google.com"
fi

cat <<EOF

Ready. Try:
  $DIR/whispersync episodes
  $DIR/whispersync run doerr
EOF
