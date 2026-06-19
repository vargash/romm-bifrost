#!/usr/bin/env bash
set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 non trovato. Installa Python 3.11+ e riprova."
  exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Python 3.11+ richiesto. Versione corrente: $(python3 --version 2>&1)"
  exit 1
fi

if ! command -v pipx >/dev/null 2>&1; then
  echo "pipx non trovato. Provo ad installarlo in user mode..."
  python3 -m pip install --user pipx
  python3 -m pipx ensurepath
  echo "Apri un nuovo terminale (o esegui: export PATH=\"$HOME/.local/bin:$PATH\") e rilancia ./install.sh"
  exit 0
fi

if pipx list 2>/dev/null | grep -q "package romm-bifrost"; then
  echo "Aggiorno installazione esistente..."
  pipx install . --force
else
  pipx install .
fi

echo "Installazione completata. Usa: bifrost --help"
