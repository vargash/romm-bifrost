# Development Environment Setup

This guide covers the full path from cloning the repository to running lint, type-check and tests locally.

## 1. Clone the repository

```bash
git clone https://github.com/vargash/romm-bifrost.git
cd romm-bifrost
```

## 2. Verify Python version

Bifrost requires Python 3.11+.

```bash
python3 --version
```

If your system has multiple Python versions, ensure `python3` points to 3.11 or newer.

## 3. Create a virtual environment (recommended)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

If virtual environment creation fails on Debian/Ubuntu with `ensurepip is not available`, install venv support:

```bash
sudo apt update
sudo apt install -y python3-venv
```

Then recreate the venv and activate it.

## 4. Install project dependencies

Install package + all development dependencies (includes `watchdog` for the save watcher):

```bash
python -m pip install -e .[dev]
```

The `[dev]` extra pulls in `watchdog>=4.0.0` alongside the test/lint toolchain. If you only want the watcher without dev tools:

```bash
python -m pip install -e .[watch]
```

## 5. Validate the development environment

Run the same checks expected during development:

```bash
python -m bifrost.cli --help
python -m ruff check .
python -m mypy bifrost tests
python -m pytest -q
```

Expected outcome:

- `ruff` reports no issues.
- `mypy` reports success (7 pre-existing `union-attr` errors in `cli.py` from the setup wizard are known and non-blocking).
- `pytest` passes all tests.

## 6. First local config for status command

Use setup to create config interactively:

```bash
python -m bifrost.cli setup
```

Or in non-interactive mode:

```bash
python -m bifrost.cli setup --url http://192.168.1.x:8080 --token rmm_your-token
```

Device Pairing mode:

```bash
python -m bifrost.cli setup --pair --url http://192.168.1.x:8080 --pair-code MCM9-FDSQ
```

Manual fallback (if needed):

```bash
mkdir -p ~/.config/bifrost
cp config.example.toml ~/.config/bifrost/config.toml
chmod 600 ~/.config/bifrost/config.toml
```

Edit `[romm].url` and `[romm].client_token` before running `status`.

```bash
python -m bifrost.cli status
```

## 7. Testing automation components locally

The watcher and systemd commands can be exercised without a real Steam Deck:

```bash
# Test the save watcher (Ctrl+C to stop)
python -m bifrost.cli watch-saves

# Dry-run systemd install (prints what would be written, no changes)
python -m bifrost.cli systemd install --dry-run

# Run diagnostics against your local config
python -m bifrost.cli doctor
```

The watcher uses `watchdog` (inotify on Linux) when available and falls back to polling. In a dev environment with no real saves directory, it will wait for the directory to appear.

## 8. Troubleshooting

### PEP 668 / externally-managed-environment

If `pip install` fails with `externally-managed-environment`, use one of these options:

1. Preferred: use a virtual environment (section 3).
2. Fallback (not recommended for long term):

```bash
python3 -m pip install --break-system-packages -e .[dev]
```

### User scripts not on PATH

If tools are installed under `~/.local/bin` and not available directly, either add it to PATH or invoke tools via Python module form:

```bash
python -m pytest
python -m ruff check .
python -m mypy bifrost tests
```

## 9. Update dependencies later

```bash
python -m pip install --upgrade pip
python -m pip install -e .[dev]
```

## 10. Exit virtual environment

```bash
deactivate
```
