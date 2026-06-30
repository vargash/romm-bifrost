# romm-bifrost

> CLI tool to bridge [RomM](https://github.com/rommapp/romm) and [ES-DE](https://es-de.org) — symlinks, gamelist.xml and save sync via RomM REST API.

Bifrost acts as an intelligent bridge between RomM and ES-DE. It reads your library entirely through the RomM REST API and projects it onto ES-DE through symlinks and generated config files — with zero file duplication.

The name comes from Norse mythology: Bifrost is the bridge connecting Asgard to Midgard.

---

## How it works

```
RomM API (HTTP/LAN)
      │
      │  platforms, ROMs, BIOS, asset paths, metadata
      ▼
  Bifrost (CLI on your ES-DE machine)
      │
      ├── ROM symlinks ───► ~/ROMs/{platform}/{rom}
      │                         → /path/to/romm/library/roms/{platform}/{rom}
      │
      ├── BIOS symlinks ──► ~/BIOS/{bios_file}
      │                         → /path/to/romm/library/bios/{bios_file}
      │
      ├── asset symlinks ─► /Emulation/tools/downloaded_media/{platform}/{type}/
      │                         → /path/to/romm/resources/roms/{platform_id}/{type}/
      │
      ├── gamelist.xml ───► ~/.emulationstation/gamelists/{platform}/gamelist.xml
      │                     (Bifrost-owned, built from API, merge-safe)
      │
      └── save sync ──────► RomM ↔ local save files
```

No files are ever copied or duplicated. RomM is the single source of truth.

---

## Requirements

| Dependency | Minimum version |
|------------|----------------|
| [RomM](https://github.com/rommapp/romm) | 4.9.2 |
| [ES-DE](https://es-de.org) | 3.4.1 |
| Python | 3.11+ |

---

## Installation

### Steam Deck / EmuDeck (recommended)

Download and run the installer. Open a terminal once and everything else runs automatically from then on:

```bash
curl -L https://github.com/vargash/romm-bifrost/releases/latest/download/install-deck.sh -o install-deck.sh
chmod +x install-deck.sh
./install-deck.sh
```

`install-deck.sh` handles everything in sequence:
1. Verifies Python 3.11+ and installs `pipx` if missing
2. Downloads and installs `bifrost` (with save-watcher support) via `pipx`
3. Runs the setup wizard interactively
4. Installs and enables the systemd user services (ROM sync, save sync, save watcher)
5. Enables session linger so services survive game-mode logout
6. Runs the initial sync

To update an existing installation without re-running the wizard:

```bash
./install-deck.sh --update
```

To uninstall:

```bash
./install-deck.sh --uninstall
```

### Pipx install

```bash
pipx install "romm-bifrost[watch] @ https://github.com/vargash/romm-bifrost/releases/latest/download/romm_bifrost-VERSION-py3-none-any.whl"
bifrost setup
```

Replace `VERSION` with the version from the [latest release](https://github.com/vargash/romm-bifrost/releases/latest).

### Development

```bash
git clone https://github.com/vargash/romm-bifrost.git
cd romm-bifrost
pip install -e .[dev]
```

For a full, reproducible setup from fresh clone to passing checks, see [`docs/development_setup.md`](docs/development_setup.md).


---

## Setup

```bash
bifrost setup
```

The setup wizard stores your RomM URL and Client API Token in `~/.config/bifrost/config.toml` with secure permissions (`600`) and verifies connectivity via `/api/heartbeat`.

`bifrost setup` is safely re-runnable: existing values are pre-filled so you can change only what you need.

Non-interactive setup:

```bash
bifrost setup --url http://192.168.1.x:8080 --token rmm_your-token
```

Device Pairing flow:

```bash
bifrost setup --pair --url http://192.168.1.x:8080 --pair-code MCM9-FDSQ
```

---

## Usage

```bash
# Check connection and library stats
bifrost status

# Scan library for anomalies (read-only)
bifrost scan

# Preview symlink operations without touching the filesystem (default)
bifrost sync

# Apply ROM, BIOS and asset symlink changes
bifrost sync --apply

# Incremental sync — only ROMs updated since last run (fast, for startup hooks)
bifrost sync --apply --incremental

# Stale check — fetch identifier set from RomM, remove deleted ROM symlinks only
bifrost sync --check-stale

# Suppress progress output (useful in background scripts)
bifrost sync --apply --incremental --quiet

# Preview gamelist.xml changes (default)
bifrost gamelist

# Apply gamelist.xml changes
bifrost gamelist --apply

# Register current machine in RomM and persist device_id
bifrost device-enroll

# Show current config values
bifrost config show

# Update one config value
bifrost config set romm.url http://192.168.1.x:8080

# Preview save sync operations
bifrost save-sync

# Apply save sync operations (optionally filtered)
bifrost save-sync --apply
bifrost save-sync --apply --only-file "Game.srm"

# Preview/apply state sync (emulator savestates)
bifrost state-sync
bifrost state-sync --apply --only-file "Game.state1"

# Bypass disk cache for a fresh run
bifrost save-sync --apply --no-cache

# Cache status and invalidation
bifrost cache status
bifrost cache invalidate

# Debug local save discovery
bifrost debug saves
```

---

## Automation

Bifrost ships with five systemd **user** services (no root required) that make sync fully automatic on a console-style device.

| Unit | Trigger | What it does |
|------|---------|--------------|
| `bifrost-sync.timer` | Boot +2 min, then every 6 h | ROM symlinks + gamelist.xml |
| `bifrost-save-sync.timer` | Boot +3 min, then every 2 h | Save files + savestates |
| `bifrost-save-watch.service` | Always running | Detects save file changes, triggers sync within 15 s |

Install and enable all services in one command:

```bash
bifrost systemd install
```

`bifrost systemd install` also:
- Auto-detects the systemd mount unit for your NAS path and injects `After=` / `BindsTo=` dependencies into the service files, so sync never runs before the NAS is mounted.
- Enables `loginctl linger` so services survive game-mode logout on Steam Deck.

Provide the NAS mount unit manually if auto-detection fails:

```bash
bifrost systemd install --nas-mount mnt-nas.mount
# find it with: systemctl list-units --type=mount
```

Check service health at any time:

```bash
bifrost systemd status
```

Uninstall:

```bash
bifrost systemd uninstall
```

### ES-DE startup hooks

For zero-perceptible-delay sync directly from ES-DE (no systemd required), install the ES-DE event hooks:

```bash
bifrost esde-hooks install
```

This writes `~/.emulationstation/scripts/game-start/bifrost.sh` and `~/.emulationstation/scripts/startup/bifrost.sh`. The startup hook launches two background jobs the moment ES-DE starts — before the UI is drawn:

- `bifrost sync --apply --incremental --quiet` — fetches only ROMs updated since last run and applies any new/changed symlinks + gamelist entries
- `bifrost sync --check-stale --quiet` — diffs the current ROM identifier set against the cached one and removes stale symlinks for deleted ROMs

Both run via `setsid ... &` so they never block ES-DE startup. On a stable library (nothing changed), the incremental path completes in ~300 ms; with 5 ROM changes it takes ~600 ms — invisible to the user.

```bash
# Verify hooks are installed
bifrost esde-hooks status

# Remove hooks
bifrost esde-hooks uninstall
```

### Save file watcher

`bifrost-save-watch.service` watches your saves directory using inotify (via `watchdog`) and triggers a save + state sync after a 15-second quiet window following the last file change. This means saves reach RomM within seconds of an emulator writing them, without polling.

If `watchdog` is not installed, the service falls back to polling every 30 seconds.

Run the watcher manually (useful for testing or debugging):

```bash
bifrost watch-saves
```

### Running unattended from cron (alternative to systemd)

If you prefer cron over systemd timers:

```bash
# Add with: crontab -e
0 */6 * * * bifrost sync --apply >> ~/.local/share/bifrost/logs/cron.log 2>&1
0 */2 * * * bifrost save-sync --apply >> ~/.local/share/bifrost/logs/cron.log 2>&1
```

---

## Diagnostics

```bash
bifrost doctor
```

`bifrost doctor` runs a full health check and prints a single report covering:

- Config file validity
- NAS paths (accessible, non-empty mount points)
- Local paths (ES-DE ROMs, gamelists, BIOS, saves, media)
- Disk space on the home partition
- RomM connectivity (live heartbeat)
- Systemd service states
- Last 20 lines of the Bifrost log

Use `--log` to also write the report to the log file — useful for diagnosing issues on a headless device without an open terminal:

```bash
bifrost doctor --log
```

---

## Pre-flight checks

Every `--apply` command (sync, gamelist, save-sync, state-sync) runs pre-flight checks before making any changes:

- NAS paths exist and are readable (detects stale/empty mounts)
- Destination directories are writable
- At least 200 MB free disk space

If a check fails, Bifrost prints an explicit error message and aborts — no partial writes, no silent failures.

---

## Save/State Sync

### How it works

`bifrost save-sync` syncs local save files (`.srm`, `.sav`, etc.) with RomM using the negotiate/complete handshake:

1. Scans `[emudeck].saves_path` for local save files
2. Fuzzy-matches each save to a ROM in the RomM library by filename
3. Calls `POST /api/sync/negotiate` — sends the full inventory, receives upload/download/conflict operations
4. In `--apply` mode, executes each operation; calls `POST /api/sync/sessions/{id}/complete` at the end

`bifrost state-sync` handles emulator savestates (`.state`, `.state1`, …) via a simpler upload-only flow against `/api/states`.

### Conflict resolution

When RomM reports a conflict (both sides changed since last sync), the `[sync].conflict_strategy` config controls the outcome:

| Strategy | Headless behavior | Interactive (`--apply` from TTY) |
|---|---|---|
| `local_wins` | Upload local file | Upload local file |
| `server_wins` | Download server file | Download server file |
| `ask` | Auto-resolves as `local_wins` + logs a warning | Prompts `[u/d/s]` for each conflict |

The default is `ask`. In headless mode (systemd, cron) `ask` is safe: Bifrost never blocks for input and defaults to local_wins.

Before any download that would overwrite a local file, Bifrost creates a `<filename>.bak` backup in the same directory.

### Logs

Bifrost writes a structured log to `~/.local/share/bifrost/logs/bifrost.log` on every sync run. The log rotates at 10 MB and keeps 5 backups.

---

## Configuration

Config is stored at `~/.config/bifrost/config.toml` (generated by `bifrost setup`).
See [`config.example.toml`](config.example.toml) for the full annotated reference.

Key sections relevant to sync:

```toml
[sync]
# Conflict resolution strategy: ask | local_wins | server_wins
conflict_strategy = "ask"
# Sync direction: push_pull | push_only | pull_only
direction = "push_pull"
# Worker threads for parallel symlink evaluation/apply (reduce if NAS is overloaded)
parallel_workers = 16

[cache]
enabled = true
ttl_roms_hours = 6
ttl_platforms_hours = 24
ttl_firmware_hours = 24
```

---

## Implementation status

| Area | Status |
|------|--------|
| Config (TOML, wizard, validation) | ✅ |
| RomM API client | ✅ |
| `bifrost sync` — ROM/BIOS/asset symlinks | ✅ |
| `bifrost gamelist` — gamelist.xml merge-safe | ✅ |
| `bifrost save-sync` — conflict resolution, backup, legacy fallback | ✅ |
| `bifrost state-sync` | ✅ |
| `bifrost device-enroll` | ✅ |
| Three-level API cache (L1 mem + L2 disk + L3 HTTP) | ✅ |
| Structured logging + log rotation | ✅ |
| Pre-flight checks (NAS, paths, disk space) | ✅ |
| `bifrost doctor` — diagnostics command | ✅ |
| Systemd user services + timers | ✅ |
| Save file watcher (inotify/polling) | ✅ |
| `install-deck.sh` — one-shot Steam Deck installer (installs from GitHub release) | ✅ |
| GitHub Actions release workflow (wheel + sdist + installer asset on tag) | ✅ |
| `bifrost sync --incremental` — delta sync via `updated_after` | ✅ |
| `bifrost sync --check-stale` — identifier-set diff, stale symlink removal | ✅ |
| ES-DE startup hooks (`bifrost esde-hooks install`) | ✅ |
| Watch mode for assets / gamelist auto-rebuild | ❌ planned |
| Structured metrics / JSON export | ❌ planned |
| Parallel symlink evaluation/apply (ThreadPoolExecutor, configurable workers) | ✅ |
| API request batching | ❌ planned |
| Partial sync resume on failure | ❌ planned |

---

## License

[GNU General Public License v3.0](LICENSE)

## Acknowledgments
This project is developed with the assistance of AI coding tools, primarily **Claude Code**, which helped in scaffolding, refactoring, and optimizing parts of the codebase.
All AI-generated code is reviewed, tested, and maintained by a human (me!).
