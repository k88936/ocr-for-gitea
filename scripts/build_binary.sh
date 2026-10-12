#!/usr/bin/env bash
#
# Build a standalone `gitea-ocr` executable with PyInstaller.
#
# The result bundles Python, httpx and GitPython. It still needs `git` and the
# `ocr` CLI on PATH at runtime, since those are external tools the app drives.
#
# Usage: scripts/build_binary.sh   ->  dist/gitea-ocr
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

uv run --extra build pyinstaller \
  --onefile \
  --clean \
  --noconfirm \
  --name gitea-ocr \
  --paths "$ROOT_DIR" \
  --hidden-import socksio \
  --collect-submodules git \
  --distpath "$ROOT_DIR/dist" \
  --workpath "$ROOT_DIR/build/pyinstaller" \
  --specpath "$ROOT_DIR/build" \
  packaging/entrypoint.py

echo "built: $ROOT_DIR/dist/gitea-ocr"
