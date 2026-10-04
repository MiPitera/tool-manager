#!/usr/bin/env bash
# Bootstrap tm on a fresh Debian VM. Idempotent.
set -euo pipefail

CODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TM_ROOT="${TM_ROOT:-$HOME/tools}"
BIN_DIR="$TM_ROOT/bin"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

say "tool-manager bootstrap (code: $CODE_DIR, data: $TM_ROOT)"

# --- required: git, curl, uv ---
if ! have git || ! have curl; then
  say "installing git/curl via apt (needs sudo)"
  sudo apt-get update && sudo apt-get install -y git curl ca-certificates
fi

if ! have uv; then
  say "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# --- optional backends (asked, not forced) ---
ask_install() { # pkg check-cmd
  have "$2" && return 0
  read -r -p "Install $1 (for ${3:-$1} installs)? [y/N] " a
  [ "$a" = y ] || [ "$a" = Y ] || return 0
  sudo apt-get install -y "$1"
}
if have apt-get; then
  ask_install docker.io docker "Docker-based tools" || true
  ask_install golang-go go "go install tools" || true
  ask_install build-essential gcc "source builds" || true
fi

# --- claude ---
if ! have claude; then
  say "WARNING: 'claude' CLI not found. tm needs Claude Code logged in."
  say "  Install Claude Code, then run: claude  (and /login with your Pro account)"
  say "  On a headless VM: run 'claude setup-token' on a desktop and export"
  say "  CLAUDE_CODE_OAUTH_TOKEN here. No API key / credits required."
fi

# --- layout + tm shim ---
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/tm" <<EOF
#!/bin/sh
# managed by tm — tool: tm
exec uv run --script "$CODE_DIR/tm.py" "\$@"
EOF
chmod 0755 "$BIN_DIR/tm"
say "installed tm shim -> $BIN_DIR/tm"

say "running tm setup"
TM_ROOT="$TM_ROOT" uv run --script "$CODE_DIR/tm.py" setup || true

say "done. Open a new shell (or: export PATH=\"$BIN_DIR:\$PATH\"), then: tm --help"
