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

```bash
git clone https://github.com/yourusername/romm-bifrost.git
cd romm-bifrost
./install.sh
```

This installs Bifrost with `pipx` and exposes the `bifrost` command globally.

If you prefer a local editable installation for development:

```bash
pip install -e .
```

For development tooling:

```bash
pip install -e .[dev]
```

For a full, reproducible setup from fresh clone to passing checks, see [`docs/development_setup.md`](docs/development_setup.md).

---

## Setup

```bash
bifrost setup
```

The setup command now stores your RomM URL and Client API Token in `~/.config/bifrost/config.toml` with secure permissions (`600`) and verifies connectivity via `/api/heartbeat`.

`bifrost setup` is safely re-runnable: existing values are loaded as defaults so you can press Enter to keep current settings and only change one or two values.

Non-interactive setup is also supported:

```bash
bifrost setup --url http://192.168.1.x:8080 --token rmm_your-token
```

Device Pairing flow is supported as well:

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

Current implementation status:

- Implemented (foundation): config loading/validation, RomM API client, setup (token + pairing), `bifrost status`, `bifrost scan`
- Implemented (production-ready): `bifrost sync` (dry-run/apply), `bifrost gamelist` (dry-run/apply), `bifrost save-sync` (preview/apply, conflict resolution, backup, legacy fallback), `bifrost state-sync` (preview/apply)
- Supporting commands: `bifrost device-enroll`, `bifrost debug saves`, `bifrost cache`
- Not yet implemented: watch mode, systemd scheduling, structured metrics export (planned for F6 automation phase)

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

The default is `ask`. In headless mode (cron, systemd) `ask` is safe: Bifrost never blocks for input and defaults to local_wins.

Before any download that would overwrite a local file, Bifrost creates a `<filename>.bak` backup in the same directory.

### Logs

Bifrost writes a structured log to `~/.local/share/bifrost/logs/bifrost.log` on every sync run. The log rotates at 10 MB and keeps 5 backups — useful for auditing unattended runs.

### Running unattended

Bifrost's sync commands are designed to be called from cron or a systemd timer without user interaction:

```bash
# Example: add to crontab with `crontab -e`
# Sync saves every 6 hours
0 */6 * * * bifrost save-sync --apply >> ~/.local/share/bifrost/logs/cron.log 2>&1
```

Automatic scheduling via a native systemd service (including network-aware triggers and watch mode) is planned for the F6 automation phase. For now, the recommended approach is a cron entry or a manual systemd timer.

---

## Configuration

Config is stored at `~/.config/bifrost/config.toml` (generated by `bifrost setup`).
See [`config.example.toml`](config.example.toml) for the full annotated reference.

Key sections relevant to sync:

```toml
[sync]
# Conflict resolution strategy: ask | local_wins | server_wins
conflict_strategy = "ask"
# Sync mode: push_pull | push
sync_mode = "push_pull"

[cache]
enabled = true
ttl_roms_hours = 6
ttl_platforms_hours = 24
ttl_firmware_hours = 24
```

---

## License

[GNU General Public License v3.0](LICENSE)
