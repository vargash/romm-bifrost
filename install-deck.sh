#!/usr/bin/env bash
# install-deck.sh — Bifrost one-shot installer for Steam Deck / EmuDeck
#
# Usage:
#   ./install-deck.sh            # interactive (recommended for first install)
#   ./install-deck.sh --update   # reinstall / upgrade, keep existing config
#   ./install-deck.sh --uninstall
#
# The script is idempotent: safe to run multiple times.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIFROST_BIN="$HOME/.local/bin/bifrost"
CONFIG_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/bifrost/config.toml"
LOG_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/bifrost/logs"

# ── colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[bifrost]${RESET} $*"; }
success() { echo -e "${GREEN}[bifrost]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[bifrost]${RESET} $*"; }
error()   { echo -e "${RED}[bifrost]${RESET} $*" >&2; }
die()     { error "$*"; exit 1; }

# ── parse args ─────────────────────────────────────────────────────────────
MODE=install
for arg in "$@"; do
  case "$arg" in
    --update)    MODE=update ;;
    --uninstall) MODE=uninstall ;;
    --help|-h)
      echo "Usage: $0 [--update|--uninstall]"
      exit 0
      ;;
  esac
done

# ── uninstall ──────────────────────────────────────────────────────────────
if [[ "$MODE" == "uninstall" ]]; then
  info "Removing Bifrost systemd units..."
  if command -v bifrost &>/dev/null; then
    bifrost systemd uninstall --yes || true
  fi
  info "Removing bifrost via pipx..."
  pipx uninstall romm-bifrost 2>/dev/null || true
  success "Bifrost uninstalled. Config at $CONFIG_FILE was NOT removed."
  exit 0
fi

# ── banner ─────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║   Bifrost — RomM ↔ ES-DE installer  ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════╝${RESET}"
echo ""
[[ "$MODE" == "update" ]] && info "Mode: update (existing config preserved)"

# ── python check ───────────────────────────────────────────────────────────
info "Checking Python version..."
if ! command -v python3 &>/dev/null; then
  die "Python 3 not found. Install Python 3.11+ and retry."
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  die "Python 3.11+ required. Found: $PY_VER"
fi
success "Python $PY_VER — OK"

# ── pipx ───────────────────────────────────────────────────────────────────
info "Checking pipx..."
if ! command -v pipx &>/dev/null; then
  warn "pipx not found — installing via pip --user..."
  python3 -m pip install --user --quiet pipx
  python3 -m pipx ensurepath --force
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v pipx &>/dev/null; then
    die "pipx install failed. Try: python3 -m pip install --user pipx"
  fi
fi
success "pipx — OK"

# ── install / update bifrost ───────────────────────────────────────────────
info "Installing bifrost[watch] via pipx..."
if [[ "$MODE" == "update" ]]; then
  pipx install "$SCRIPT_DIR[watch]" --force --quiet
else
  if pipx list 2>/dev/null | grep -q "package romm-bifrost"; then
    warn "bifrost already installed — reinstalling..."
    pipx install "$SCRIPT_DIR[watch]" --force --quiet
  else
    pipx install "$SCRIPT_DIR[watch]" --quiet
  fi
fi

# Verify the binary is reachable
export PATH="$HOME/.local/bin:$PATH"
if ! command -v bifrost &>/dev/null; then
  die "bifrost binary not found after install. Add $HOME/.local/bin to PATH and retry."
fi
success "bifrost $(bifrost --version 2>/dev/null || echo 'installed') — OK"

# ── initial setup wizard ───────────────────────────────────────────────────
if [[ "$MODE" != "update" ]] || [[ ! -f "$CONFIG_FILE" ]]; then
  echo ""
  info "Starting setup wizard..."
  echo -e "  ${YELLOW}You will need:${RESET}"
  echo "    • RomM URL (e.g. http://192.168.1.x:8080)"
  echo "    • RomM Client API Token (starts with rmm_)"
  echo "    • NAS paths to your RomM library and resources"
  echo ""
  bifrost setup
else
  info "Config already exists at $CONFIG_FILE — skipping wizard (--update mode)"
  info "Run 'bifrost setup' any time to change settings."
fi

# ── systemd units ─────────────────────────────────────────────────────────
echo ""
info "Installing systemd user units..."
bifrost systemd install

# ── ensure lingering is enabled (survives game-mode logout) ───────────────
CURRENT_USER="${USER:-$(id -un)}"
if command -v loginctl &>/dev/null; then
  if ! loginctl show-user "$CURRENT_USER" 2>/dev/null | grep -q "Linger=yes"; then
    info "Enabling linger for $CURRENT_USER (keeps services alive in game mode)..."
    loginctl enable-linger "$CURRENT_USER" 2>/dev/null || \
      warn "Could not enable linger — services may stop when you log out of desktop mode"
  else
    success "Linger already enabled for $CURRENT_USER"
  fi
fi

# ── log directory ─────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"

# ── initial sync ──────────────────────────────────────────────────────────
echo ""
info "Running initial ROM sync (this may take a few minutes)..."
if bifrost sync --apply; then
  success "ROM symlinks created"
else
  warn "ROM sync returned a non-zero exit code — check 'bifrost status' and logs at $LOG_DIR"
fi

info "Running initial gamelist sync..."
if bifrost gamelist --apply; then
  success "Gamelists updated"
else
  warn "Gamelist sync returned a non-zero exit code"
fi

info "Running initial save sync..."
if bifrost save-sync --apply; then
  success "Saves synced"
else
  warn "Save sync returned a non-zero exit code"
fi

# ── summary ───────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}═══════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  Bifrost installation complete!       ${RESET}"
echo -e "${BOLD}${GREEN}═══════════════════════════════════════${RESET}"
echo ""
echo -e "  Config:  ${CYAN}$CONFIG_FILE${RESET}"
echo -e "  Logs:    ${CYAN}$LOG_DIR/bifrost.log${RESET}"
echo ""
echo -e "  Active automation:"
echo -e "    ${GREEN}✓${RESET} ROM sync + gamelist  — at boot + every 6 hours"
echo -e "    ${GREEN}✓${RESET} Save/state sync       — at boot + every 2 hours"
echo -e "    ${GREEN}✓${RESET} Save file watcher     — triggers sync on every local save"
echo ""
echo -e "  Useful commands:"
echo -e "    ${CYAN}bifrost systemd status${RESET}   — check service health"
echo -e "    ${CYAN}bifrost status${RESET}            — check RomM connection"
echo -e "    ${CYAN}bifrost cache status${RESET}      — inspect API cache"
echo -e "    ${CYAN}bifrost setup${RESET}             — re-run configuration wizard"
echo ""
