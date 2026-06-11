#!/usr/bin/env bash
# Mirror this repo's scripts/ into the local-disk hook runtime copy.
# The global Claude/Codex hooks execute sp_signal.py from the runtime dir
# (local disk, still works when the /Users mount is unavailable), so after
# editing anything under scripts/ in the dev repo, run this to push it.
# Mirror semantics: files removed here are also removed from the runtime.
#
# Usage:
#   ./scripts/sync-hook-runtime.sh                  # sync to $HOME/Git/SandboxPuls
#   SP_RUNTIME_DIR=/path ./scripts/sync-hook-runtime.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_DIR="${SP_RUNTIME_DIR:-$HOME/Git/SandboxPuls}"

if [[ "$PROJECT_DIR" -ef "$RUNTIME_DIR" ]]; then
  echo "source and runtime are the same directory, nothing to do"
  exit 0
fi

mkdir -p "$RUNTIME_DIR/scripts"
rsync -a --delete "$PROJECT_DIR/scripts/" "$RUNTIME_DIR/scripts/"

if diff -rq "$PROJECT_DIR/scripts" "$RUNTIME_DIR/scripts" >/dev/null; then
  echo "synced: $PROJECT_DIR/scripts/ -> $RUNTIME_DIR/scripts/"
else
  echo "error: runtime differs from source after sync" >&2
  exit 1
fi
