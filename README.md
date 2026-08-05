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

**After `git pull`:** if `bifrost-save-watch.service` is already running
(`systemctl --user is-active bifrost-save-watch.service`), it keeps executing
the code from before your pull — `pip install -e .` alone doesn't restart it.
Re-run `bifrost systemd install`: it restarts the watcher if idle, or warns
and leaves it alone if a save sync is in progress.


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

> **0.4.0 breaking change:** save-related commands moved under a `save` group —
> `save-sync` → `save sync`, `save-untrack` → `save untrack`, `watch-saves` → `save watch`,
> `debug saves` → `save debug`. Update any scripts, cron entries or aliases that call the old names.

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

# Review/remove orphan platform folders (e.g. leftover EmuDeck folders with no matching RomM platform)
bifrost sync --apply --prune-orphans

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

# Rewrite the config file on the current schema: drop obsolete keys, fill new
# defaults. Run after upgrading bifrost (install-deck.sh --update does this
# automatically). No prompts — safe for scripts.
bifrost config migrate

# Preview save sync operations
bifrost save sync

# Apply save sync operations (optionally filtered)
bifrost save sync --apply
bifrost save sync --apply --only-file "Game.srm"

# Stop syncing one save to this device
bifrost save untrack 123

# Watch local saves and trigger sync on change (used by bifrost-save-watch.service)
bifrost save watch

# Bypass disk cache for a fresh run
bifrost save sync --apply --no-cache

# Cache status and invalidation
bifrost cache status
bifrost cache invalidate

# Debug local save discovery
bifrost save debug
```

---

## Automation

Bifrost ships with five systemd **user** services (no root required) that make sync fully automatic on a console-style device.

### Save-sync timing at a glance

Every automatic trigger that can run `save-sync` on its own, in one place — useful when you're not calling `bifrost save-sync` manually at all (e.g. playing purely through ES-DE):

| Trigger | Direction | Fires | Blocking |
|---|---|---|---|
| ES-DE `game-start` hook | download only | the moment you launch a game | yes — blocks launch up to 8 s, fail-open (game starts anyway on timeout) |
| ES-DE `game-end` hook | upload only | the moment you quit a game | no — backgrounded |
| ES-DE `suspend` hook | upload only | device suspend | no — backgrounded |
| ES-DE `quit`/`poweroff`/`reboot` hooks | full push/pull | ES-DE quit, shutdown, reboot | yes — up to 30 s, fail-open |
| ES-DE `startup` hook | full push/pull | ES-DE launch | no — backgrounded (only the 15 s incremental *ROM* sync blocks startup) |
| Save file watcher (`bifrost-save-watch.service`) | full push/pull | 15 s after the last local save file change (60 s cooldown between runs) | no — always-running background daemon |
| `bifrost-save-sync.timer` | full push/pull | boot +3 min, then every 2 h | no — background oneshot |
| `bifrost save-sync --apply` | whatever `[sync].direction` says | on demand | yes |

So: play on mobile, close it, launch the same game from ES-DE on the console — the `game-start` hook pulls the latest save from RomM *before* the emulator reads it (up to an 8 s wait). No manual `bifrost save-sync` needed once `bifrost esde-hooks install` has been run and ES-DE's custom event scripts setting is enabled. The other triggers (watcher, 2 h timer, `game-end`/`suspend`/`quit` pushes) exist as a safety net in case a save changes outside of a tracked game session, or the 8 s pull times out.

**Savestates** are *not* covered by any of the above, including the 2 h timer — `bifrost-save-sync.timer` only runs `bifrost save sync`, never a savestate sync. The file watcher explicitly skips them, and no ES-DE hook syncs them either. Savestate sync isn't currently wired up to a CLI command at all (see [How it works](#how-it-works) below) — there's no automatic *or* manual trigger yet.

### Library sync timing at a glance

Same idea for ROMs/BIOS/assets/gamelist.xml (`bifrost sync` / `bifrost gamelist`) — this is metadata pulled from RomM, not files you generate by playing, so it only needs to run when your RomM library actually changes:

| Trigger | What runs | Fires | Blocking |
|---|---|---|---|
| ES-DE `startup` hook | Incremental ROM sync (`--incremental`) + incremental gamelist.xml patch | ES-DE launch | yes — 15 s timeout |
| ES-DE `startup` hook (background) | Stale-symlink check (`--check-stale`) | ES-DE launch, right after the above | no — backgrounded |
| `bifrost-sync.timer` | Full `bifrost sync --apply` + full `bifrost gamelist --apply` | boot +2 min, then every 6 h | no — background oneshot |
| `bifrost sync --apply` / `bifrost gamelist --apply` (manual) | Full symlink + gamelist regeneration, orphan-platform detection | on demand | yes |

The incremental path (ES-DE startup) only picks up ROMs `updated_after` the last run, so it's fast (~300–600 ms) but can drift from a full re-scan over time; the 6 h timer's full sync is the periodic correction for that.

| Unit | Trigger | What it does |
|------|---------|--------------|
| `bifrost-sync.timer` | Boot +2 min, then every 6 h | ROM symlinks + gamelist.xml |
| `bifrost-save-sync.timer` | Boot +3 min, then every 2 h | Save files |
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

### ES-DE event hooks

For save-sync that's tied to actually playing (not just a timer), install the ES-DE custom event scripts — this also needs *Main menu → Other settings → Enable custom event scripts* turned on in ES-DE itself:

```bash
bifrost esde-hooks install
```

This writes one script per ES-DE lifecycle event under `~/ES-DE/scripts/<event>/` (override with `--scripts-path`):

| Event | Script | What it does |
|---|---|---|
| `startup` | `10-bifrost-sync.sh` | Blocking 15 s incremental ROM sync, then backgrounds a stale-symlink check and a full save-sync |
| `game-start` | `10-bifrost-pull.sh` | Blocking, **download-only** save-sync scoped to the launched ROM, 8 s timeout |
| `game-end` | `10-bifrost-push.sh` | Backgrounded, **upload-only** save-sync scoped to the ROM just played |
| `suspend` | `10-bifrost-push.sh` | Backgrounded, upload-only save-sync (best-effort) |
| `quit` / `poweroff` / `reboot` | `10-bifrost-flush.sh` | Blocking, full push/pull save-sync, 30 s timeout |

Every hook is fail-open: on timeout or error it exits `0` so ES-DE / the game / the shutdown is never blocked or aborted by a sync problem. `--rom-path`-scoped hooks (`game-start`, `game-end`) also record play-session timestamps used for save-sync bookkeeping.

```bash
# Verify hooks are installed
bifrost esde-hooks status

# Remove hooks
bifrost esde-hooks uninstall
```

### Save file watcher

`bifrost-save-watch.service` watches your saves directory using inotify (via `watchdog`) and triggers a save sync after a 15-second quiet window following the last file change. This means saves reach RomM within seconds of an emulator writing them, without polling.

If `watchdog` is not installed, the service falls back to polling every 30 seconds.

Run the watcher manually (useful for testing or debugging):

```bash
bifrost save watch
```

### Running unattended from cron (alternative to systemd)

If you prefer cron over systemd timers:

```bash
# Add with: crontab -e
0 */6 * * * bifrost sync --apply >> ~/.local/share/bifrost/logs/cron.log 2>&1
0 */2 * * * bifrost save sync --apply >> ~/.local/share/bifrost/logs/cron.log 2>&1
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

Every `--apply` command (sync, gamelist, save sync) runs pre-flight checks before making any changes:

- NAS paths exist and are readable (detects stale/empty mounts)
- Destination directories are writable
- At least 200 MB free disk space

If a check fails, Bifrost prints an explicit error message and aborts — no partial writes, no silent failures.

---

## Orphan platform folders

EmuDeck pre-creates a folder under `roms/` for every emulator it supports, before RomM/Bifrost ever runs. Since RomM is the single source of truth, `bifrost sync` detects top-level folders under `[esde].roms_path` that don't match any platform currently in your RomM library, and reports them in the sync summary.

Only *directories* are ever candidates for removal, and only when their entire contents are subdirectories or symlinks Bifrost itself created — any real file (or a symlink Bifrost didn't create) makes a folder "unsafe"; it's reported but never touched automatically, no matter the strategy below. Two exceptions: `systeminfo.txt` and `metadata.txt`, scaffolding files EmuDeck stamps into every platform folder it creates, are ignored by the safety check — their presence alone doesn't block automatic removal.

Detection and reporting always run as part of `bifrost sync`. Removal is opt-in and requires both:
- `[sync].prune_orphan_platforms = true` in config, or the `--prune-orphans` flag for a one-off run
- `--apply` (dry-run only ever previews what would be removed)

If orphan folders are found and `bifrost sync` is run without `--apply`, the dry-run summary
suggests the exact command to prune them (`bifrost sync --apply --prune-orphans`) — separately
for folders that are safe to auto-prune and folders that contain real files and need review.

The `[sync].orphan_platform_strategy` config then controls how *safe* folders are confirmed for removal:

| Strategy | Headless behavior | Interactive (`--apply` from TTY) |
|---|---|---|
| `remove` | Removes all safe orphan folders | Removes all safe orphan folders |
| `skip` | Leaves all orphan folders in place | Leaves all orphan folders in place |
| `ask` | Leaves folders in place + logs a warning | Prompts `[y/n]` per folder |

Folders that aren't safe (contain real files beyond `systeminfo.txt`/`metadata.txt`) are never covered by
`orphan_platform_strategy` — they require a human. With pruning enabled and `--apply` run from a
TTY, Bifrost lists each unsafe folder's actual contents and asks for an explicit per-folder
override before removing anything. Headlessly, unsafe folders are always left in place and logged
for manual review.

The default is `ask`, gated behind `prune_orphan_platforms = false` — nothing is ever removed until you explicitly opt in.

---

## Save Sync

### How it works

`bifrost save sync` syncs local save files (`.srm`, `.sav`, etc.) with RomM using the negotiate/complete handshake:

1. Scans `[emudeck].saves_path` for local save files
2. Fuzzy-matches each save to a ROM in the RomM library by filename
3. Calls `POST /api/sync/negotiate` — sends the full inventory, receives upload/download/conflict operations
4. In `--apply` mode, executes each operation; calls `POST /api/sync/sessions/{id}/complete` at the end

Emulator savestate sync (`.state`, `.state1`, …) is implemented at the module level (`bifrost/state_sync.py`) but not currently wired up to a CLI command — it's on hold pending RomM device-sync API compliance work.

### Conflict resolution

When RomM reports a conflict (both sides changed since last sync), the `[sync].conflict_strategy` config controls the outcome:

| Strategy | Headless behavior | Interactive (`--apply` from TTY) |
|---|---|---|
| `local_wins` | Upload local file | Upload local file |
| `server_wins` | Download server file | Download server file |
| `ask` | Auto-resolves as `local_wins` + logs a warning | Prompts `[u/d/s]` for each conflict |

The default is `ask`. In headless mode (systemd, cron) `ask` is safe: Bifrost never blocks for input and defaults to local_wins.

Before any download that would overwrite a local file, Bifrost creates a `<filename>.bak` backup in the same directory.

### Cross-core save compatibility

Bifrost matches saves on `(ROM, emulator)`. If the same game is synced from two different emulators/cores — e.g. this device runs DuckStation, but a phone syncs the same PS1 game through RetroArch's Beetle PSX HW core (reported to RomM as `mednafen_psx_hw`) — Bifrost treats them as unrelated save families by default and won't route the phone's save into DuckStation's folder.

Cross-core save mappings tell Bifrost that a foreign emulator/core tag is safe to route into one of your local emulator profiles, because the two produce byte-compatible save files. There are two kinds:

- **Built-in (verified)** — pairings Bifrost's maintainers have manually confirmed are byte-compatible (currently: `mednafen_psx_hw` → `duckstation`, both using the standard 128KB PS1 memory card image). Still requires an explicit `sync.core_mappings` entry to take effect on a given device — nothing routes automatically.
- **Custom (unverified)** — a pairing *you* declare, for a core/emulator pair Bifrost hasn't vetted. Same file extension or size is not proof of true compatibility; a wrong mapping can corrupt a save on download. Adding one requires confirmation (or `--yes`), and warnings/logs distinguish two cases: the remote core is entirely unknown to Bifrost, or it's curated — just for a *different* local emulator than the one you targeted (likely a typo/mistake worth double-checking).

Manage mappings with:

```sh
# No flags at all: walks you through platform -> source core -> target,
# suggesting the curated target automatically when one exists.
bifrost config add-core-mapping

# For a built-in pairing, --local-emulator/--platform auto-fill:
bifrost config add-core-mapping --remote-core mednafen_psx_hw

# For a custom pairing, specify everything explicitly (asks for confirmation):
bifrost config add-core-mapping --remote-core some_core --local-emulator duckstation --platform psx

# List built-in and configured mappings, with verified/unverified status
bifrost config list-core-mappings

# Remove a mapping
bifrost config remove-core-mapping --remote-core mednafen_psx_hw
```

See `[[sync.core_mappings]]` in [`config.example.toml`](config.example.toml) for the underlying config shape.

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
# Stable slot name for saves with no explicit numbered slot. Must match the slot
# naming used by other RomM clients to keep saves paired on (rom_id, slot)
# across devices.
slot = "autosave"
# Opt in to reviewing/removing orphan platform folders (see "Orphan platform folders" above)
prune_orphan_platforms = false
# Orphan removal strategy: ask | remove | skip
orphan_platform_strategy = "ask"

[cache]
enabled = true
ttl_roms_hours = 6
ttl_platforms_hours = 24
ttl_firmware_hours = 24
```

---

## License

[GNU General Public License v3.0](LICENSE)

## Acknowledgments
This project is developed with the assistance of AI coding tools, primarily **Claude Code**, which helped in scaffolding, refactoring, and optimizing parts of the codebase.
All AI-generated code is reviewed, tested, and maintained by a human (me!).
