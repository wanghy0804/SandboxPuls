#!/usr/bin/env bash
# Install SandboxPulse hook configs.
# By default writes to project-level $PROJECT_DIR/.claude/ and .codex/.
# With --global, writes to user-level $HOME/.claude/ and .codex/ so any
# claude/codex session triggers the hook regardless of launch directory.
# Idempotent: detects presence by the unique substring "sp_signal.py" and
# skips if already installed.
#
# Usage:
#   ./scripts/install-hooks.sh             # project-level (this repo only)
#   ./scripts/install-hooks.sh --global    # user-level (everywhere)
set -euo pipefail

SCOPE="project"
for arg in "$@"; do
  case "$arg" in
    --global) SCOPE="global" ;;
    --project) SCOPE="project" ;;
    -h|--help)
      sed -n '2,11p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOOK_SCRIPT="$PROJECT_DIR/scripts/hooks/sp_signal.py"
SIGNAL_DIR="$PROJECT_DIR/signals"
MARKER="sp_signal.py"

if [[ "$HOOK_SCRIPT" == "$HOME/"* ]]; then
  HOOK_CMD='$HOME/'"${HOOK_SCRIPT#$HOME/}"
  SIGNAL_CMD='$HOME/'"${SIGNAL_DIR#$HOME/}"
else
  HOOK_CMD="$HOOK_SCRIPT"
  SIGNAL_CMD="$SIGNAL_DIR"
fi

if [[ "$SCOPE" == "global" ]]; then
  CONFIG_ROOT="$HOME"
else
  CONFIG_ROOT="$PROJECT_DIR"
fi
CLAUDE_DIR="$CONFIG_ROOT/.claude"
CLAUDE_FILE="$CLAUDE_DIR/settings.json"
CODEX_DIR="$CONFIG_ROOT/.codex"
CODEX_FILE="$CODEX_DIR/config.toml"

echo "[scope] $SCOPE  ->  $CONFIG_ROOT"

if [[ ! -f "$HOOK_SCRIPT" ]]; then
  echo "error: hook script not found at $HOOK_SCRIPT" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required" >&2
  exit 1
fi

mkdir -p "$CLAUDE_DIR" "$CODEX_DIR" "$SIGNAL_DIR"

python3 - "$CLAUDE_FILE" "$HOOK_CMD" "$SIGNAL_CMD" "$MARKER" <<'PY'
import json
import sys
from pathlib import Path

target, hook, signal_dir, marker = sys.argv[1:5]
path = Path(target)
data = {}
if path.exists():
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError as exc:
        print(f"[claude] error: {target} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, dict):
        print(f"[claude] error: {target} root is not an object", file=sys.stderr)
        sys.exit(2)

hooks = data.setdefault("hooks", {})
if not isinstance(hooks, dict):
    print(f"[claude] error: existing 'hooks' is not an object", file=sys.stderr)
    sys.exit(2)

for events in hooks.values():
    if not isinstance(events, list):
        continue
    for entry in events:
        for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
            if marker in h.get("command", ""):
                print("[claude] already installed, skip")
                sys.exit(0)

cmd = f"SP_PROVIDER=claude SP_SIGNAL_DIR={signal_dir} python3 {hook}"

def add(event: str, matcher: str | None = None) -> None:
    entry: dict = {"hooks": [{"type": "command", "command": cmd, "timeout": 3}]}
    if matcher is not None:
        entry["matcher"] = matcher
    hooks.setdefault(event, []).append(entry)

for ev in ("SessionStart", "UserPromptSubmit", "Stop", "Notification"):
    add(ev)
for ev in ("PreToolUse", "PostToolUse", "PermissionRequest"):
    add(ev, ".*")

path.write_text(json.dumps(data, indent=2) + "\n")
print(f"[claude] installed -> {target}")
PY

if [[ -f "$CODEX_FILE" ]] && grep -q "$MARKER" "$CODEX_FILE"; then
  echo "[codex] already installed, skip"
else
  touch "$CODEX_FILE"
  cat >>"$CODEX_FILE" <<EOF

# >>> sandboxpulse:hook (managed block — remove between markers to uninstall)
[[hooks.SessionStart]]
[[hooks.SessionStart.hooks]]
type = "command"
command = 'SP_PROVIDER=codex SP_SIGNAL_DIR=$SIGNAL_CMD python3 $HOOK_CMD'
timeout = 3

[[hooks.UserPromptSubmit]]
[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = 'SP_PROVIDER=codex SP_SIGNAL_DIR=$SIGNAL_CMD python3 $HOOK_CMD'
timeout = 3

[[hooks.PreToolUse]]
matcher = ".*"
[[hooks.PreToolUse.hooks]]
type = "command"
command = 'SP_PROVIDER=codex SP_SIGNAL_DIR=$SIGNAL_CMD python3 $HOOK_CMD'

[[hooks.PostToolUse]]
matcher = ".*"
[[hooks.PostToolUse.hooks]]
type = "command"
command = 'SP_PROVIDER=codex SP_SIGNAL_DIR=$SIGNAL_CMD python3 $HOOK_CMD'

[[hooks.PermissionRequest]]
matcher = ".*"
[[hooks.PermissionRequest.hooks]]
type = "command"
command = 'SP_PROVIDER=codex SP_SIGNAL_DIR=$SIGNAL_CMD python3 $HOOK_CMD'

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = 'SP_PROVIDER=codex SP_SIGNAL_DIR=$SIGNAL_CMD python3 $HOOK_CMD'
# <<< sandboxpulse:hook
EOF
  echo "[codex] installed -> $CODEX_FILE"
fi

echo "done. signals will land in: $SIGNAL_DIR"
