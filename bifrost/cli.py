"""Bifrost CLI entrypoint."""

from __future__ import annotations

import logging
import os
import platform as sys_platform
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table

from bifrost.api.client import RommApiClient, exchange_pairing_code
from bifrost.api.models import DeviceCreatePayload
from bifrost.cache import BifrostCache
from bifrost.config import (
    AppConfig,
    CacheConfig,
    EmudeckConfig,
    EsdeConfig,
    NasConfig,
    RommConfig,
    default_config_path,
    load_config,
    save_config,
)
from bifrost.errors import ApiError, AuthenticationError, ConfigError, NetworkError
from bifrost.gamelist import apply_gamelist_plan, build_gamelist_plan
from bifrost.logging_setup import setup_file_logging
from bifrost.multidisc import (
    M3uOperation,
    apply_m3u_operation,
    evaluate_m3u_operation,
    plan_m3u_operations,
)
from bifrost.preflight import PreflightResult, run_nas_check, run_save_preflight, run_sync_preflight
from bifrost.save_sync import build_save_sync_preview, execute_save_sync_preview
from bifrost.state_sync import build_state_sync_preview, execute_state_sync_preview
from bifrost.symlink_manager import (
    RemoveSymlinkOperation,
    apply_operation,
    apply_remove_operation,
    evaluate_operation,
    evaluate_remove_operation,
    plan_stale_removals,
    plan_symlink_operations,
)

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_AUTH_ERROR = 3
EXIT_API_ERROR = 4

PAIRING_CODE_PATTERN = re.compile(r"^[A-Z0-9]{4}-?[A-Z0-9]{4}$", re.IGNORECASE)


def _abort_on_preflight(result: PreflightResult, console: Console) -> None:
    """Print pre-flight warnings/errors and abort with EXIT_CONFIG_ERROR if any errors."""
    for warn in result.warnings:
        console.print(f"[yellow]Pre-flight warning:[/yellow] {warn}")
    if not result.ok:
        console.print("[red bold]Pre-flight checks failed — aborting --apply:[/red bold]")
        for err in result.errors:
            console.print(f"  [red]✗[/red] {err}")
        console.print(
            "\nFix the issues above and retry. "
            "Run [cyan]bifrost doctor[/cyan] for a full diagnostics report."
        )
        raise SystemExit(EXIT_CONFIG_ERROR)


@click.group(help="Bifrost: RomM <-> ES-DE bridge CLI")
def main() -> None:
    """Main CLI group."""


@main.group(help="Debug helpers for inspecting local paths and RomM discovery.")
def debug() -> None:
    """Debug command group."""


@main.group(help="View and update Bifrost configuration values.")
def config() -> None:
    """Configuration command group."""


@main.group(name="cache", help="Manage the Bifrost API response cache.")
def cache_group() -> None:
    """Cache management command group."""


def _format_age(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "never"
    hours = int(age_seconds // 3600)
    minutes = int((age_seconds % 3600) // 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


@cache_group.command(name="status", help="Show age and item count for each cached collection.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override config file path (default: ~/.config/bifrost/config.toml).",
)
def cache_status(config_path: Path | None) -> None:
    console = Console()
    resolved_path = config_path or default_config_path()

    try:
        cfg = load_config(resolved_path)
        cache_cfg = cfg.cache
    except ConfigError:
        cache_cfg = CacheConfig()

    bifrost_cache = BifrostCache(cache_cfg)
    statuses = bifrost_cache.status()

    if not cache_cfg.enabled:
        console.print("[yellow]Cache is disabled in config (cache.enabled = false).[/yellow]")

    table = Table(title="Bifrost Cache Status")
    table.add_column("Key")
    table.add_column("Fetched at")
    table.add_column("Age")
    table.add_column("Items")
    table.add_column("TTL (h)")
    table.add_column("Status")

    ttl_map = {
        "platforms": cache_cfg.ttl_platforms_hours,
        "roms": cache_cfg.ttl_roms_hours,
        "firmware": cache_cfg.ttl_firmware_hours,
    }
    for key, st in sorted(statuses.items()):
        fetched_str = st.fetched_at.isoformat(timespec="seconds") if st.fetched_at else "—"
        age_str = _format_age(st.age_seconds)
        status_str = "[red]expired[/red]" if st.is_expired else "[green]fresh[/green]"
        table.add_row(
            key,
            fetched_str,
            age_str,
            str(st.item_count) if st.item_count else "—",
            str(ttl_map.get(key, 24)),
            status_str,
        )
    console.print(table)
    raise SystemExit(EXIT_OK)


@cache_group.command(name="invalidate", help="Invalidate one or all cached collections.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override config file path (default: ~/.config/bifrost/config.toml).",
)
@click.option(
    "--key",
    type=click.Choice(["platforms", "roms", "firmware"]),
    default=None,
    help="Invalidate only this key. Omit to invalidate all.",
)
def cache_invalidate(config_path: Path | None, key: str | None) -> None:
    console = Console()
    resolved_path = config_path or default_config_path()

    try:
        cfg = load_config(resolved_path)
        cache_cfg = cfg.cache
    except ConfigError:
        cache_cfg = CacheConfig()

    bifrost_cache = BifrostCache(cache_cfg)
    bifrost_cache.invalidate(key)

    if key:
        console.print(f"[green]Cache invalidated:[/green] {key}")
    else:
        console.print("[green]Cache invalidated:[/green] all keys")
    raise SystemExit(EXIT_OK)


def _flatten_config(prefix: str, value: Any, out: dict[str, str]) -> None:
    """Flatten nested config values into dot-path keys."""

    if isinstance(value, dict):
        for key in sorted(value):
            path = f"{prefix}.{key}" if prefix else key
            _flatten_config(path, value[key], out)
        return
    out[prefix] = str(value)


def _resolve_interactive_base_config(existing_config: AppConfig | None) -> AppConfig:
    """Return defaults used by setup wizard when no config is available."""

    if existing_config is not None:
        return existing_config
    return AppConfig(
        romm=RommConfig(url="http://localhost:8080", client_token="rmm_placeholder", device_id="")
    )


def _collect_save_debug_rows(root: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return folder and file rows for a local save-tree inspection (follows symlinks)."""

    folder_rows: list[dict[str, str]] = []
    file_rows: list[dict[str, str]] = []

    if not root.exists():
        return folder_rows, file_rows

    immediate_children = sorted(root.iterdir(), key=lambda path: path.name.lower())
    for child in immediate_children:
        if child.name.startswith("."):
            continue
        if not (child.is_dir() or (child.is_symlink() and child.resolve().is_dir())):
            continue
        
        file_count = 0
        try:
            for _dirpath, _dirnames, filenames in os.walk(child, followlinks=True):
                for filename in filenames:
                    if not filename.startswith("."):
                        file_count += 1
        except (OSError, PermissionError):
            pass
        
        folder_rows.append(
            {
                "name": child.name,
                "path": str(child),
                "files": str(file_count),
            }
        )

    for dirpath, _dirnames, filenames in os.walk(root, followlinks=True):
        for filename in filenames:
            if filename.startswith("."):
                continue
            path = Path(dirpath) / filename
            try:
                relative_path = path.relative_to(root)
            except ValueError:
                relative_path = path
            try:
                size = path.stat().st_size
                file_rows.append(
                    {
                        "path": str(relative_path),
                        "size": str(size),
                    }
                )
            except (OSError, PermissionError):
                pass

    return folder_rows, file_rows


@debug.command(name="saves", help="Inspect the local save tree that Bifrost scans.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override config file path (default: ~/.config/bifrost/config.toml).",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=50,
    show_default=True,
    help="Maximum number of file rows to print.",
)
def debug_saves(config_path: Path | None, limit: int) -> None:
    """Show the local save path, folder counts and discovered files."""

    console = Console()
    resolved_path = config_path or default_config_path()

    try:
        config = load_config(resolved_path)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc

    save_root = Path(config.emudeck.saves_path).expanduser()
    folder_rows, file_rows = _collect_save_debug_rows(save_root)

    summary = Table(title="Bifrost Save Debug")
    summary.add_column("Metric")
    summary.add_column("Value")
    summary.add_row("Config file", str(resolved_path))
    summary.add_row("Configured saves_path", config.emudeck.saves_path)
    summary.add_row("Expanded saves_path", str(save_root))
    summary.add_row("Exists", "yes" if save_root.exists() else "no")
    summary.add_row("Top-level folders", str(len(folder_rows)))
    summary.add_row("Files discovered", str(len(file_rows)))
    console.print(summary)

    if folder_rows:
        folders = Table(title="Top-Level Save Folders")
        folders.add_column("Folder")
        folders.add_column("Path")
        folders.add_column("Files")
        for row in folder_rows:
            folders.add_row(row["name"], row["path"], row["files"])
        console.print(folders)

    if file_rows:
        files = Table(title="Discovered Save Files")
        files.add_column("Path")
        files.add_column("Size")
        for row in file_rows[:limit]:
            files.add_row(row["path"], row["size"])
        console.print(files)
        if len(file_rows) > limit:
            console.print(f"[yellow]Showing first {limit} files out of {len(file_rows)}.[/yellow]")
    else:
        console.print("[yellow]No save files discovered under the configured root.[/yellow]")

    raise SystemExit(EXIT_OK)


@config.command(name="show", help="Print current configuration values.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override config file path (default: ~/.config/bifrost/config.toml).",
)
def config_show(config_path: Path | None) -> None:
    """Print loaded config values in a key/value table."""

    console = Console()
    resolved_path = config_path or default_config_path()

    try:
        loaded = load_config(resolved_path)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc

    flattened: dict[str, str] = {}
    _flatten_config("", loaded.model_dump(mode="python"), flattened)

    table = Table(title="Bifrost Config")
    table.add_column("Key")
    table.add_column("Value")
    table.add_row("config.path", str(resolved_path))
    for key in sorted(flattened):
        table.add_row(key, flattened[key])
    console.print(table)
    raise SystemExit(EXIT_OK)


@config.command(name="set", help="Update one configuration value using dot notation.")
@click.argument("key", type=str)
@click.argument("value", type=str)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override config file path (default: ~/.config/bifrost/config.toml).",
)
def config_set(key: str, value: str, config_path: Path | None) -> None:
    """Set a config value and persist it to disk."""

    console = Console()
    resolved_path = config_path or default_config_path()

    try:
        loaded = load_config(resolved_path)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc

    key_path = [part for part in key.strip().split(".") if part]
    if len(key_path) < 2:
        console.print(
            "[red]Configuration error:[/red] key must use dot notation (for example romm.url)."
        )
        raise SystemExit(EXIT_CONFIG_ERROR)

    dumped = loaded.model_dump(mode="python")
    cursor: Any = dumped
    for part in key_path[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            console.print(f"[red]Configuration error:[/red] Unknown key: {key}")
            raise SystemExit(EXIT_CONFIG_ERROR)
        cursor = cursor[part]

    leaf = key_path[-1]
    if not isinstance(cursor, dict) or leaf not in cursor:
        console.print(f"[red]Configuration error:[/red] Unknown key: {key}")
        raise SystemExit(EXIT_CONFIG_ERROR)

    normalized_value = value
    if key == "romm.url":
        normalized_value = value.strip().rstrip("/")
    if key == "romm.client_token" and not value.startswith("rmm_"):
        console.print("[red]Configuration error:[/red] RomM token must start with 'rmm_'.")
        raise SystemExit(EXIT_CONFIG_ERROR)

    cursor[leaf] = normalized_value

    try:
        updated = AppConfig.model_validate(dumped)
    except Exception as exc:
        console.print(f"[red]Configuration error:[/red] Invalid value for {key}: {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc

    save_path = save_config(updated, resolved_path)
    console.print(f"[green]Updated[/green] {key} = {normalized_value}")
    console.print(f"[green]Configuration saved:[/green] {save_path}")
    raise SystemExit(EXIT_OK)


@main.command(help="Check RomM API connectivity and basic library endpoints.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override config file path (default: ~/.config/bifrost/config.toml).",
)
def status(config_path: Path | None) -> None:
    """Validate current configuration and API reachability."""

    console = Console()
    resolved_path = config_path or default_config_path()

    try:
        config = load_config(resolved_path)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc

    try:
        with RommApiClient(config) as client:
            heartbeat = client.heartbeat()
            stats = client.stats(include_platform_stats=False)
    except AuthenticationError as exc:
        console.print(f"[red]Authentication error:[/red] {exc}")
        raise SystemExit(EXIT_AUTH_ERROR) from exc
    except (NetworkError, ApiError) as exc:
        console.print(f"[red]API error:[/red] {exc}")
        raise SystemExit(EXIT_API_ERROR) from exc

    table = Table(title="Bifrost Status")
    table.add_column("Check")
    table.add_column("Value")
    table.add_row("Config file", str(resolved_path))
    table.add_row("RomM URL", config.romm.url)
    table.add_row("Heartbeat", heartbeat.status or heartbeat.message or "ok")
    table.add_row("Platforms", str(stats.PLATFORMS))
    table.add_row("ROMs", str(stats.ROMS))
    if stats.SAVES is not None:
        table.add_row("Saves", str(stats.SAVES))
    if stats.STATES is not None:
        table.add_row("States", str(stats.STATES))
    if stats.SCREENSHOTS is not None:
        table.add_row("Screenshots", str(stats.SCREENSHOTS))

    console.print(table)
    raise SystemExit(EXIT_OK)


@main.command(help="Scan RomM for missing, duplicate and unmatched ROM anomalies.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override config file path (default: ~/.config/bifrost/config.toml).",
)
def scan(config_path: Path | None) -> None:
    """Display a lightweight RomM anomaly report."""

    console = Console()
    resolved_path = config_path or default_config_path()

    try:
        config = load_config(resolved_path)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc

    try:
        with RommApiClient(config) as client:
            stats = client.stats(include_platform_stats=False)
            unmatched_roms = client.roms_count(matched=False)
            missing_roms = client.roms_count(missing=True)
            duplicate_roms = client.roms_count(duplicate=True)
    except AuthenticationError as exc:
        console.print(f"[red]Authentication error:[/red] {exc}")
        raise SystemExit(EXIT_AUTH_ERROR) from exc
    except (NetworkError, ApiError) as exc:
        console.print(f"[red]API error:[/red] {exc}")
        raise SystemExit(EXIT_API_ERROR) from exc

    table = Table(title="Bifrost Scan")
    table.add_column("Check")
    table.add_column("Value")
    table.add_row("RomM URL", config.romm.url)
    table.add_row("Total platforms", str(stats.PLATFORMS))
    table.add_row("Total ROMs", str(stats.ROMS))
    table.add_row("Unmatched ROMs", str(unmatched_roms))
    table.add_row("Missing ROMs", str(missing_roms))
    table.add_row("Duplicate ROMs", str(duplicate_roms))

    console.print(table)
    raise SystemExit(EXIT_OK)


@main.command(help="Generate or preview ES-DE gamelist.xml files from RomM metadata.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override config file path (default: ~/.config/bifrost/config.toml).",
)
@click.option(
    "--apply",
    is_flag=True,
    help="Write gamelist.xml files to disk. Without this flag, command runs in dry-run mode.",
)
@click.option("--no-cache", "no_cache", is_flag=True, help="Bypass disk cache for this run.")
def gamelist(config_path: Path | None, apply: bool, no_cache: bool) -> None:
    """Generate merge-safe gamelist.xml plans or files."""

    console = Console()
    resolved_path = config_path or default_config_path()
    rows: list[dict[str, Any]] = []

    try:
        config = load_config(resolved_path)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc

    if apply:
        _abort_on_preflight(run_sync_preflight(config), console)

    try:
        with RommApiClient(config, no_cache=no_cache) as client:
            if apply:
                apply_results = apply_gamelist_plan(config, client)
                rows = [
                    {
                        "platform": result.plan.platform_slug,
                        "path": result.plan.output_path,
                        "roms": result.plan.total_roms,
                        "new": result.plan.new_entries,
                        "updated": result.plan.updated_entries,
                        "unchanged": result.plan.unchanged_entries,
                        "removed": result.plan.removed_entries,
                        "written": result.written,
                    }
                    for result in apply_results
                ]
            else:
                plans = build_gamelist_plan(config, client)
                rows = [
                    {
                        "platform": plan.platform_slug,
                        "path": plan.output_path,
                        "roms": plan.total_roms,
                        "new": plan.new_entries,
                        "updated": plan.updated_entries,
                        "unchanged": plan.unchanged_entries,
                        "removed": plan.removed_entries,
                        "written": False,
                    }
                    for plan in plans
                ]
    except AuthenticationError as exc:
        console.print(f"[red]Authentication error:[/red] {exc}")
        raise SystemExit(EXIT_AUTH_ERROR) from exc
    except (NetworkError, ApiError) as exc:
        console.print(f"[red]API error:[/red] {exc}")
        raise SystemExit(EXIT_API_ERROR) from exc

    total_platforms = len(rows)
    total_roms = sum(int(row["roms"]) for row in rows)
    total_new = sum(int(row["new"]) for row in rows)
    total_updated = sum(int(row["updated"]) for row in rows)
    total_unchanged = sum(int(row["unchanged"]) for row in rows)
    total_removed = sum(int(row["removed"]) for row in rows)
    total_written = sum(1 for row in rows if bool(row["written"]))

    mode_label = "apply" if apply else "dry-run"
    summary = Table(title=f"Bifrost Gamelist ({mode_label})")
    summary.add_column("Metric")
    summary.add_column("Value")
    summary.add_row("Platforms", str(total_platforms))
    summary.add_row("ROM entries", str(total_roms))
    summary.add_row("New entries", str(total_new))
    summary.add_row("Updated entries", str(total_updated))
    summary.add_row("Unchanged entries", str(total_unchanged))
    summary.add_row("Removed entries", str(total_removed))
    if apply:
        summary.add_row("Files written", str(total_written))
    console.print(summary)

    details = Table(title="Gamelist Platform Summary")
    details.add_column("Platform")
    details.add_column("ROMs")
    details.add_column("New")
    details.add_column("Updated")
    details.add_column("Unchanged")
    details.add_column("Removed")
    details.add_column("Output")
    if apply:
        details.add_column("Written")

    max_rows = 25
    for row in rows[:max_rows]:
        detail_values = [
            str(row["platform"]),
            str(row["roms"]),
            str(row["new"]),
            str(row["updated"]),
            str(row["unchanged"]),
            str(row["removed"]),
            str(row["path"]),
        ]
        if apply:
            detail_values.append("yes" if bool(row["written"]) else "no")
        details.add_row(*detail_values)

    if rows:
        console.print(details)
        if len(rows) > max_rows:
            console.print(
                f"[yellow]Showing first {max_rows} platforms out of {len(rows)}.[/yellow]"
            )

    if not apply:
        console.print(
            "[cyan]Dry-run mode: no files were written. "
            "Re-run with --apply to write gamelist.xml files.[/cyan]"
        )

    raise SystemExit(EXIT_OK)


@main.command(help="Plan/apply ROM, BIOS and asset symlinks for ES-DE.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override config file path (default: ~/.config/bifrost/config.toml).",
)
@click.option(
    "--apply",
    is_flag=True,
    help="Apply filesystem changes. Without this flag, sync runs in dry-run mode.",
)
@click.option("--no-cache", "no_cache", is_flag=True, help="Bypass disk cache for this run.")
def sync(config_path: Path | None, apply: bool, no_cache: bool) -> None:
    """Create a dry-run plan or apply symlink operations."""

    console = Console()
    resolved_path = config_path or default_config_path()

    try:
        config = load_config(resolved_path)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc

    # NAS check runs in both dry-run and --apply: a down NAS makes the plan meaningless.
    _abort_on_preflight(run_nas_check(config), console)

    if apply:
        _abort_on_preflight(run_sync_preflight(config), console)

    try:
        with RommApiClient(config, no_cache=no_cache) as client:
            ops = plan_symlink_operations(config, client)
            m3u_ops = plan_m3u_operations(config, client)
    except AuthenticationError as exc:
        console.print(f"[red]Authentication error:[/red] {exc}")
        raise SystemExit(EXIT_AUTH_ERROR) from exc
    except (NetworkError, ApiError) as exc:
        console.print(f"[red]API error:[/red] {exc}")
        raise SystemExit(EXIT_API_ERROR) from exc

    remove_ops = plan_stale_removals(config, ops)
    all_ops: list[Any] = list(ops) + list(m3u_ops) + list(remove_ops)

    results: list[Any]
    if apply:
        if not all_ops:
            results = []
        else:
            results = []
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                TimeRemainingColumn(),
                console=console,
            ) as progress:
                task_id = progress.add_task("Applying sync", total=len(all_ops))
                for op in all_ops:
                    if isinstance(op, M3uOperation):
                        results.append(apply_m3u_operation(op))
                    elif isinstance(op, RemoveSymlinkOperation):
                        results.append(apply_remove_operation(op))
                    else:
                        results.append(apply_operation(op))
                    progress.advance(task_id)
        mode_label = "apply"
    else:
        results = []
        for op in all_ops:
            if isinstance(op, M3uOperation):
                results.append(evaluate_m3u_operation(op))
            elif isinstance(op, RemoveSymlinkOperation):
                results.append(evaluate_remove_operation(op))
            else:
                results.append(evaluate_operation(op))
        mode_label = "dry-run"

    counts: dict[str, int] = {}
    by_category: dict[str, int] = {}
    error_by_category: dict[str, int] = {}
    for result in results:
        counts[result.action] = counts.get(result.action, 0) + 1
        category = result.operation.category
        by_category[category] = by_category.get(category, 0) + 1
        if result.action == "error":
            error_by_category[category] = error_by_category.get(category, 0) + 1

    summary = Table(title=f"Bifrost Sync ({mode_label})")
    summary.add_column("Metric")
    summary.add_column("Value")
    summary.add_row("Total operations", str(len(results)))
    summary.add_row("ROM symlinks", str(by_category.get("rom", 0)))
    summary.add_row("BIOS symlinks", str(by_category.get("bios", 0)))
    summary.add_row("Asset symlinks", str(by_category.get("asset", 0)))
    summary.add_row("Legacy asset dirs removed", str(by_category.get("asset-dir", 0)))
    summary.add_row("M3U playlists", str(by_category.get("m3u", 0)))
    summary.add_row("Create", str(counts.get("create", 0)))
    summary.add_row("Replace", str(counts.get("replace", 0)))
    summary.add_row("Already OK", str(counts.get("ok", 0)))
    summary.add_row("Stale removed", str(counts.get("remove", 0)))
    summary.add_row("Broken (NAS file missing)", str(counts.get("broken", 0)))
    summary.add_row("Missing target (skipped)", str(counts.get("missing-target", 0)))
    summary.add_row("Conflicts", str(counts.get("conflict", 0)))
    summary.add_row("Errors", str(counts.get("error", 0)))
    console.print(summary)

    details = Table(title="Sync Operation Preview")
    details.add_column("Category")
    details.add_column("Action")
    details.add_column("Destination")
    details.add_column("Target")
    details.add_column("Detail")

    _SYNC_ACTION_PRIORITY = {
        "error": 0, "conflict": 1, "create": 2, "replace": 3, "remove": 4,
        "broken": 5, "missing-target": 6, "ok": 7, "skip": 8,
    }
    preview_results = sorted(results, key=lambda r: _SYNC_ACTION_PRIORITY.get(r.action, 9))
    max_rows = 25
    for result in preview_results[:max_rows]:
        details.add_row(
            result.operation.category,
            result.action,
            str(result.operation.destination),
            str(result.operation.target),
            result.detail,
        )

    if results:
        console.print(details)
        if len(results) > max_rows:
            console.print(
                f"[yellow]Showing first {max_rows} operations out of {len(results)}.[/yellow]"
            )

    if error_by_category:
        error_table = Table(title="Sync Errors by Category")
        error_table.add_column("Category")
        error_table.add_column("Errors")
        for category in sorted(error_by_category):
            error_table.add_row(category, str(error_by_category[category]))
        console.print(error_table)

    if not apply:
        console.print(
            "[cyan]Dry-run mode: no filesystem changes were made. "
            "Re-run with --apply to execute.[/cyan]"
        )

    if apply and counts.get("error", 0):
        console.print(
            "[yellow]Apply completed with filesystem errors. "
            "Check paths/permissions and re-run sync --apply.[/yellow]"
        )
        raise SystemExit(EXIT_CONFIG_ERROR)

    raise SystemExit(EXIT_OK)


@main.command(name="save-sync", help="Preview RomM save sync negotiation from local save files.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override config file path (default: ~/.config/bifrost/config.toml).",
)
@click.option(
    "--device-id",
    type=str,
    default=None,
    help="Override RomM device_id for sync negotiation.",
)
@click.option(
    "--apply",
    is_flag=True,
    help="Execute selected sync operations. Without this flag, command runs in preview mode.",
)
@click.option(
    "--only-file",
    "only_files",
    multiple=True,
    type=str,
    help="Filter sync to one or more file names/path fragments.",
)
@click.option("--no-cache", "no_cache", is_flag=True, help="Bypass disk cache for this run.")
def save_sync(
    config_path: Path | None,
    device_id: str | None,
    apply: bool,
    only_files: tuple[str, ...],
    no_cache: bool,
) -> None:
    """Scan local saves and preview the RomM sync negotiation."""

    console = Console()
    resolved_path = config_path or default_config_path()
    setup_file_logging()
    _cli_log = logging.getLogger("bifrost.cli.save_sync")

    try:
        config = load_config(resolved_path)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc

    if apply:
        _abort_on_preflight(run_save_preflight(config), console)

    is_interactive = sys.stdin.isatty() and sys.stdout.isatty()
    _cli_log.info(
        "save-sync started: apply=%s interactive=%s filters=%s",
        apply,
        is_interactive,
        list(only_files),
    )

    try:
        with RommApiClient(config, no_cache=no_cache) as client:
            preview = build_save_sync_preview(
                config,
                client,
                device_id=device_id,
                file_filters=list(only_files),
            )

            # Interactive conflict resolution for "ask" strategy
            conflict_overrides: dict[str, str] = {}
            if apply and is_interactive and config.sync.conflict_strategy == "ask":
                conflict_ops = [op for op in preview.operations if op.action == "conflict"]
                if conflict_ops:
                    console.print(
                        f"[yellow]{len(conflict_ops)} conflict(s) require resolution:[/yellow]"
                    )
                for op in conflict_ops:
                    console.print(
                        f"  [bold]{op.file_name}[/bold] (rom_id={op.rom_id}): {op.reason}"
                    )
                    choice = Prompt.ask(
                        "  Resolve as [u]pload (local wins), [d]ownload (server wins), or [s]kip?",
                        choices=["u", "d", "s"],
                        default="u",
                    )
                    conflict_overrides[op.file_name] = {
                        "u": "upload",
                        "d": "download",
                        "s": "skip",
                    }[choice]

            execution = None
            if apply:
                execution = execute_save_sync_preview(
                    config,
                    client,
                    preview,
                    file_filters=list(only_files),
                    is_interactive=is_interactive,
                    conflict_overrides=conflict_overrides if conflict_overrides else None,
                )
    except AuthenticationError as exc:
        _cli_log.error("authentication error: %s", exc)
        console.print(f"[red]Authentication error:[/red] {exc}")
        raise SystemExit(EXIT_AUTH_ERROR) from exc
    except ConfigError as exc:
        _cli_log.error("config error: %s", exc)
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc
    except (NetworkError, ApiError) as exc:
        _cli_log.error("api/network error: %s", exc)
        console.print(f"[red]API error:[/red] {exc}")
        raise SystemExit(EXIT_API_ERROR) from exc

    selected_operations = preview.operations
    if only_files:
        lowered_filters = [item.lower() for item in only_files]
        selected_operations = [
            op
            for op in preview.operations
            if any(fragment in op.file_name.lower() for fragment in lowered_filters)
        ]

    conflict_count = sum(1 for op in selected_operations if op.action == "conflict")
    summary = Table(title="Bifrost Save Sync (preview)")
    summary.add_column("Metric")
    summary.add_column("Value")
    summary.add_row("Config file", str(resolved_path))
    summary.add_row("Device ID", preview.device_id)
    summary.add_row("Files scanned", str(preview.scanned_files))
    summary.add_row("Files mapped", str(preview.mapped_files))
    summary.add_row("Files skipped", str(preview.skipped_files))
    summary.add_row("Negotiated session", str(preview.session_id or "-"))
    summary.add_row("Operations", str(len(selected_operations)))
    if conflict_count:
        summary.add_row("Conflicts", str(conflict_count))
    if only_files:
        summary.add_row("File filter", ", ".join(only_files))
    console.print(summary)

    operations_table = Table(title="Sync Operations")
    operations_table.add_column("Action")
    operations_table.add_column("ROM")
    operations_table.add_column("Save")
    operations_table.add_column("Reason")
    _SAVE_ACTION_PRIORITY = {"conflict": 0, "download": 1, "upload": 2, "skip": 3}
    for operation in sorted(selected_operations, key=lambda o: _SAVE_ACTION_PRIORITY.get(o.action, 9))[:25]:
        operations_table.add_row(
            operation.action,
            str(operation.rom_id),
            operation.file_name,
            operation.reason,
        )

    if selected_operations:
        console.print(operations_table)
        if len(selected_operations) > 25:
            console.print("[yellow]Showing first 25 operations only.[/yellow]")

    if preview.skipped_paths:
        skipped = Table(title="Unmapped Local Saves")
        skipped.add_column("Path")
        for path in preview.skipped_paths[:25]:
            skipped.add_row(str(path))
        console.print(skipped)
        if len(preview.skipped_paths) > 25:
            console.print("[yellow]Showing first 25 unmapped saves only.[/yellow]")

    if apply and execution is not None:
        result_table = Table(title="Save Sync Execution")
        result_table.add_column("Metric")
        result_table.add_column("Value")
        result_table.add_row("Executed", str(execution.executed))
        result_table.add_row("Failed", str(execution.failed))
        result_table.add_row("Skipped", str(execution.skipped))
        console.print(result_table)

        details = Table(title="Execution Details")
        details.add_column("Action")
        details.add_column("Save")
        details.add_column("Result")
        for action, file_name, result in execution.details[:25]:
            details.add_row(action, file_name, result)
        if execution.details:
            console.print(details)
            if len(execution.details) > 25:
                console.print("[yellow]Showing first 25 execution rows only.[/yellow]")

        _cli_log.info(
            "save-sync completed: executed=%d failed=%d skipped=%d",
            execution.executed,
            execution.failed,
            execution.skipped,
        )
    else:
        console.print(
            "[cyan]Preview only: no save files were uploaded or downloaded in this tranche.[/cyan]"
        )

    if apply and execution is not None and execution.failed > 0:
        raise SystemExit(EXIT_API_ERROR)

    raise SystemExit(EXIT_OK)


@main.command(name="state-sync", help="Preview/apply RomM state sync from local state files.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override config file path (default: ~/.config/bifrost/config.toml).",
)
@click.option(
    "--apply",
    is_flag=True,
    help="Execute state upload operations. Without this flag, command runs in preview mode.",
)
@click.option(
    "--only-file",
    "only_files",
    multiple=True,
    type=str,
    help="Filter sync to one or more file names/path fragments.",
)
@click.option("--no-cache", "no_cache", is_flag=True, help="Bypass disk cache for this run.")
def state_sync(
    config_path: Path | None,
    apply: bool,
    only_files: tuple[str, ...],
    no_cache: bool,
) -> None:
    """Scan local state files and preview/apply state sync actions."""

    console = Console()
    resolved_path = config_path or default_config_path()
    setup_file_logging()
    _cli_log = logging.getLogger("bifrost.cli.state_sync")
    _cli_log.info("state-sync started: apply=%s filters=%s", apply, list(only_files))

    try:
        config = load_config(resolved_path)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc

    if apply:
        _abort_on_preflight(run_save_preflight(config), console)

    try:
        with RommApiClient(config, no_cache=no_cache) as client:
            preview = build_state_sync_preview(
                config,
                client,
                file_filters=list(only_files),
            )
            execution = None
            if apply:
                execution = execute_state_sync_preview(
                    config,
                    client,
                    preview,
                    file_filters=list(only_files),
                )
    except AuthenticationError as exc:
        _cli_log.error("authentication error: %s", exc)
        console.print(f"[red]Authentication error:[/red] {exc}")
        raise SystemExit(EXIT_AUTH_ERROR) from exc
    except ConfigError as exc:
        _cli_log.error("config error: %s", exc)
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc
    except (NetworkError, ApiError) as exc:
        _cli_log.error("api/network error: %s", exc)
        console.print(f"[red]API error:[/red] {exc}")
        raise SystemExit(EXIT_API_ERROR) from exc

    selected_operations = preview.operations
    if only_files:
        lowered_filters = [item.lower() for item in only_files]
        selected_operations = [
            op
            for op in preview.operations
            if any(fragment in op.file_name.lower() for fragment in lowered_filters)
        ]

    summary = Table(title="Bifrost State Sync (preview)")
    summary.add_column("Metric")
    summary.add_column("Value")
    summary.add_row("Config file", str(resolved_path))
    summary.add_row("Files scanned", str(preview.scanned_files))
    summary.add_row("Files mapped", str(preview.mapped_files))
    summary.add_row("Files skipped", str(preview.skipped_files))
    summary.add_row("Operations", str(len(selected_operations)))
    if only_files:
        summary.add_row("File filter", ", ".join(only_files))
    console.print(summary)

    operations = Table(title="State Sync Operations")
    operations.add_column("Action")
    operations.add_column("ROM")
    operations.add_column("State")
    operations.add_column("Reason")
    _STATE_ACTION_PRIORITY = {"conflict": 0, "download": 1, "upload": 2, "skip": 3}
    for operation in sorted(selected_operations, key=lambda o: _STATE_ACTION_PRIORITY.get(o.action, 9))[:25]:
        operations.add_row(
            operation.action,
            str(operation.rom_id),
            operation.file_name,
            operation.reason,
        )

    if selected_operations:
        console.print(operations)
        if len(selected_operations) > 25:
            console.print("[yellow]Showing first 25 operations only.[/yellow]")

    if preview.skipped_paths:
        skipped = Table(title="Unmapped Local States")
        skipped.add_column("Path")
        for path in preview.skipped_paths[:25]:
            skipped.add_row(str(path))
        console.print(skipped)
        if len(preview.skipped_paths) > 25:
            console.print("[yellow]Showing first 25 unmapped states only.[/yellow]")

    if apply and execution is not None:
        result_table = Table(title="State Sync Execution")
        result_table.add_column("Metric")
        result_table.add_column("Value")
        result_table.add_row("Executed", str(execution.executed))
        result_table.add_row("Failed", str(execution.failed))
        result_table.add_row("Skipped", str(execution.skipped))
        console.print(result_table)

        details = Table(title="Execution Details")
        details.add_column("Action")
        details.add_column("State")
        details.add_column("Result")
        for action, file_name, result in execution.details[:25]:
            details.add_row(action, file_name, result)
        if execution.details:
            console.print(details)
            if len(execution.details) > 25:
                console.print("[yellow]Showing first 25 execution rows only.[/yellow]")

        _cli_log.info(
            "state-sync completed: executed=%d failed=%d skipped=%d",
            execution.executed,
            execution.failed,
            execution.skipped,
        )
    else:
        console.print(
            "[cyan]Preview only: no state files were uploaded in this tranche.[/cyan]"
        )

    if apply and execution is not None and execution.failed > 0:
        raise SystemExit(EXIT_API_ERROR)

    raise SystemExit(EXIT_OK)


@main.command(name="device-enroll", help="Register this machine in RomM and store its device_id.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override config file path (default: ~/.config/bifrost/config.toml).",
)
@click.option(
    "--name",
    type=str,
    default=None,
    help="Device display name shown in RomM.",
)
@click.option(
    "--platform",
    type=str,
    default=None,
    help="Device platform label (for example linux).",
)
@click.option(
    "--client",
    "client_name",
    type=str,
    default=None,
    help="Client name reported to RomM.",
)
@click.option(
    "--client-version",
    type=str,
    default=None,
    help="Client version reported to RomM.",
)
@click.option(
    "--hostname",
    type=str,
    default=None,
    help="Hostname reported to RomM.",
)
@click.option(
    "--sync-mode",
    type=str,
    default=None,
    help="RomM sync mode for this device (api, file_transfer, push_pull).",
)
@click.option(
    "--allow-duplicate/--no-allow-duplicate",
    default=False,
    help="Allow registering a duplicate device entry.",
)
@click.option(
    "--allow-existing/--no-allow-existing",
    default=True,
    help="Allow enrolling even if a device with similar identity already exists.",
)
@click.option(
    "--reset-syncs",
    is_flag=True,
    help="Reset sync history for the enrolled device.",
)
@click.option(
    "--replace",
    is_flag=True,
    help="Replace an existing romm.device_id in the config.",
)
def device_enroll(
    config_path: Path | None,
    name: str | None,
    platform: str | None,
    client_name: str | None,
    client_version: str | None,
    hostname: str | None,
    sync_mode: str | None,
    allow_duplicate: bool,
    allow_existing: bool,
    reset_syncs: bool,
    replace: bool,
) -> None:
    """Register this machine in RomM and persist the returned device ID."""

    console = Console()
    resolved_path = config_path or default_config_path()

    try:
        config = load_config(resolved_path)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc

    if config.romm.device_id and not replace:
        console.print(
            "[yellow]Existing device_id found in config.[/yellow] Use --replace to re-enroll."
        )
        raise SystemExit(EXIT_CONFIG_ERROR)

    hostname_value = (hostname or sys_platform.node() or "bifrost").strip()
    device_name_value = (
        name or Prompt.ask("Device name", default=f"Bifrost on {hostname_value}")
    ).strip()
    platform_value = (
        platform or Prompt.ask("Device platform", default=sys_platform.system().lower())
    ).strip()
    client_value = (client_name or Prompt.ask("Client name", default="bifrost")).strip()
    client_version_value = (client_version or Prompt.ask("Client version", default="0.1.0")).strip()
    hostname_reported = (hostname or Prompt.ask("Hostname", default=hostname_value)).strip()
    sync_mode_value = (sync_mode or Prompt.ask("Sync mode", default=config.sync.sync_mode)).strip()

    try:
        with RommApiClient(config) as client:
            response = client.register_device(
                DeviceCreatePayload(
                    name=device_name_value,
                    platform=platform_value,
                    client=client_value,
                    client_version=client_version_value,
                    hostname=hostname_reported,
                    sync_mode=sync_mode_value,
                    allow_existing=allow_existing,
                    allow_duplicate=allow_duplicate,
                    reset_syncs=reset_syncs,
                )
            )
    except AuthenticationError as exc:
        console.print(f"[red]Authentication error:[/red] {exc}")
        raise SystemExit(EXIT_AUTH_ERROR) from exc
    except (NetworkError, ApiError) as exc:
        console.print(f"[red]API error:[/red] {exc}")
        raise SystemExit(EXIT_API_ERROR) from exc

    updated_config = config.model_copy(
        update={
            "romm": config.romm.model_copy(update={"device_id": response.device_id}),
        }
    )
    save_config(updated_config, resolved_path)

    table = Table(title="Bifrost Device Enrollment")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Config file", str(resolved_path))
    table.add_row("Device ID", response.device_id)
    table.add_row("Device name", device_name_value)
    table.add_row("Platform", platform_value)
    table.add_row("Client", client_value)
    table.add_row("Version", client_version_value)
    table.add_row("Hostname", hostname_reported)
    table.add_row("Sync mode", sync_mode_value)
    console.print(table)
    console.print("[green]Device enrollment saved to config.[/green]")
    raise SystemExit(EXIT_OK)


@main.command(help="Configure RomM URL and Client Token for Bifrost.")
@click.option(
    "--url",
    "romm_url",
    type=str,
    default=None,
    help="RomM base URL, for example http://192.168.1.10:8080",
)
@click.option(
    "--token",
    "client_token",
    type=str,
    default=None,
    help="RomM Client API Token (rmm_...).",
)
@click.option(
    "--pair",
    is_flag=True,
    help="Use Device Pairing flow (exchange 8-digit code for a token).",
)
@click.option(
    "--pair-code",
    type=str,
    default=None,
    help="8-digit Device Pairing code from RomM UI.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override config file path (default: ~/.config/bifrost/config.toml).",
)
@click.option(
    "--skip-verify",
    is_flag=True,
    help="Skip API heartbeat verification before saving config.",
)
@click.option(
    "--configure-paths",
    is_flag=True,
    help="Prompt for NAS/ES-DE/EmuDeck path settings during setup.",
)
@click.option("--nas-library-path", type=str, default=None, help="NAS RomM library root path.")
@click.option("--nas-resources-path", type=str, default=None, help="NAS RomM resources root path.")
@click.option("--esde-roms-path", type=str, default=None, help="ES-DE ROMs destination path.")
@click.option("--emudeck-bios-path", type=str, default=None, help="EmuDeck BIOS destination path.")
@click.option(
    "--emudeck-media-path",
    type=str,
    default=None,
    help="EmuDeck media destination path for asset symlinks.",
)
def setup(
    romm_url: str | None,
    client_token: str | None,
    pair: bool,
    pair_code: str | None,
    config_path: Path | None,
    skip_verify: bool,
    configure_paths: bool,
    nas_library_path: str | None,
    nas_resources_path: str | None,
    esde_roms_path: str | None,
    emudeck_bios_path: str | None,
    emudeck_media_path: str | None,
) -> None:
    """Run setup using wizard defaults or CLI options."""

    console = Console()
    resolved_path = config_path or default_config_path()

    existing_config: AppConfig | None = None
    if resolved_path.exists():
        try:
            existing_config = load_config(resolved_path)
        except ConfigError:
            console.print(
                "[yellow]Warning:[/yellow] Existing config could not be loaded. "
                "Path values will use defaults unless provided now."
            )

    use_interactive_wizard = not any(
        [
            romm_url,
            client_token,
            pair,
            pair_code,
            configure_paths,
            nas_library_path,
            nas_resources_path,
            esde_roms_path,
            emudeck_bios_path,
            emudeck_media_path,
            skip_verify,
        ]
    )

    base_config = _resolve_interactive_base_config(existing_config)
    default_url = base_config.romm.url or "http://localhost:8080"

    if use_interactive_wizard:
        console.print("[bold cyan]Bifrost Setup[/bold cyan]")
        url_value = Prompt.ask("RomM URL", default=default_url).strip().rstrip("/")

        use_pairing = Confirm.ask("Use Device Pairing code", default=False)
        if use_pairing:
            code_value = Prompt.ask("RomM Pairing Code (8 digits)").strip()
            if not PAIRING_CODE_PATTERN.fullmatch(code_value):
                console.print(
                    "[red]Configuration error:[/red] Pairing code must be 8 alphanumeric"
                    " characters, optionally formatted with a hyphen (AAAA-BBBB)."
                )
                raise SystemExit(EXIT_CONFIG_ERROR)
            normalized_code = code_value.replace("-", "").upper()
            try:
                token_value = exchange_pairing_code(url_value, normalized_code)
            except (NetworkError, ApiError) as exc:
                console.print(f"[red]API error:[/red] {exc}")
                raise SystemExit(EXIT_API_ERROR) from exc
            console.print("[green]Pairing exchange completed.[/green]")
        else:
            existing_token = (
                existing_config.romm.client_token if existing_config is not None else ""
            )
            keep_existing_token = bool(existing_token) and Confirm.ask(
                "Keep existing RomM Client Token",
                default=True,
            )
            if keep_existing_token:
                token_value = existing_token
            else:
                token_value = Prompt.ask("RomM Client Token", password=True).strip()

        should_prompt_paths = Confirm.ask(
            "Update NAS/ES-DE/EmuDeck paths",
            default=existing_config is None,
        )
        if should_prompt_paths:
            nas_library_value = Prompt.ask(
                "NAS library path",
                default=base_config.nas.library_path,
            )
            nas_resources_value = Prompt.ask(
                "NAS resources path",
                default=base_config.nas.resources_path,
            )
            esde_roms_value = Prompt.ask(
                "ES-DE ROMs path",
                default=base_config.esde.roms_path,
            )
            emudeck_bios_value = Prompt.ask(
                "EmuDeck BIOS path",
                default=base_config.emudeck.bios_path,
            )
            emudeck_media_value = Prompt.ask(
                "EmuDeck media path",
                default=base_config.emudeck.media_path,
            )
        else:
            nas_library_value = base_config.nas.library_path
            nas_resources_value = base_config.nas.resources_path
            esde_roms_value = base_config.esde.roms_path
            emudeck_bios_value = base_config.emudeck.bios_path
            emudeck_media_value = base_config.emudeck.media_path
    else:
        url_value = (romm_url or Prompt.ask("RomM URL", default=default_url)).strip().rstrip("/")

        if pair and client_token:
            console.print("[red]Configuration error:[/red] Use either --pair or --token, not both.")
            raise SystemExit(EXIT_CONFIG_ERROR)

        if not pair and pair_code:
            console.print("[red]Configuration error:[/red] --pair-code requires --pair.")
            raise SystemExit(EXIT_CONFIG_ERROR)

        if pair:
            code_value = (pair_code or Prompt.ask("RomM Pairing Code (8 digits)")).strip()
            if not PAIRING_CODE_PATTERN.fullmatch(code_value):
                console.print(
                    "[red]Configuration error:[/red] Pairing code must be 8 alphanumeric"
                    " characters, optionally formatted with a hyphen (AAAA-BBBB)."
                )
                raise SystemExit(EXIT_CONFIG_ERROR)
            normalized_code = code_value.replace("-", "").upper()
            try:
                token_value = exchange_pairing_code(url_value, normalized_code)
            except (NetworkError, ApiError) as exc:
                console.print(f"[red]API error:[/red] {exc}")
                raise SystemExit(EXIT_API_ERROR) from exc
            console.print("[green]Pairing exchange completed.[/green]")
        else:
            token_value = (
                client_token
                or Prompt.ask(
                    "RomM Client Token",
                    password=True,
                )
            ).strip()

        if configure_paths:
            nas_library_value = (
                nas_library_path
                if nas_library_path is not None
                else Prompt.ask("NAS library path", default=base_config.nas.library_path)
            )
            nas_resources_value = (
                nas_resources_path
                if nas_resources_path is not None
                else Prompt.ask("NAS resources path", default=base_config.nas.resources_path)
            )
            esde_roms_value = (
                esde_roms_path
                if esde_roms_path is not None
                else Prompt.ask("ES-DE ROMs path", default=base_config.esde.roms_path)
            )
            emudeck_bios_value = (
                emudeck_bios_path
                if emudeck_bios_path is not None
                else Prompt.ask("EmuDeck BIOS path", default=base_config.emudeck.bios_path)
            )
            emudeck_media_value = (
                emudeck_media_path
                if emudeck_media_path is not None
                else Prompt.ask("EmuDeck media path", default=base_config.emudeck.media_path)
            )
        else:
            nas_library_value = nas_library_path or base_config.nas.library_path
            nas_resources_value = nas_resources_path or base_config.nas.resources_path
            esde_roms_value = esde_roms_path or base_config.esde.roms_path
            emudeck_bios_value = emudeck_bios_path or base_config.emudeck.bios_path
            emudeck_media_value = emudeck_media_path or base_config.emudeck.media_path

    if not url_value:
        console.print("[red]Configuration error:[/red] RomM URL cannot be empty.")
        raise SystemExit(EXIT_CONFIG_ERROR)

    if not token_value.startswith("rmm_"):
        console.print("[red]Configuration error:[/red] RomM token must start with 'rmm_'.")
        raise SystemExit(EXIT_CONFIG_ERROR)

    config = AppConfig(
        romm=RommConfig(
            url=url_value,
            client_token=token_value,
            device_id=base_config.romm.device_id,
        ),
        nas=NasConfig(
            library_path=nas_library_value,
            resources_path=nas_resources_value,
            roms_subpath=base_config.nas.roms_subpath,
            bios_subpath=base_config.nas.bios_subpath,
        ),
        esde=EsdeConfig(
            roms_path=esde_roms_value,
            gamelists_path=base_config.esde.gamelists_path,
            custom_systems_path=base_config.esde.custom_systems_path,
        ),
        emudeck=EmudeckConfig(
            bios_path=emudeck_bios_value,
            media_path=emudeck_media_value,
            saves_path=base_config.emudeck.saves_path,
        ),
        assets=base_config.assets,
        sync=base_config.sync,
        output=base_config.output,
    )

    if not skip_verify:
        try:
            with RommApiClient(config) as client:
                heartbeat = client.heartbeat()
        except AuthenticationError as exc:
            console.print(f"[red]Authentication error:[/red] {exc}")
            raise SystemExit(EXIT_AUTH_ERROR) from exc
        except (NetworkError, ApiError) as exc:
            console.print(f"[red]API error:[/red] {exc}")
            raise SystemExit(EXIT_API_ERROR) from exc

        heartbeat_text = heartbeat.status or heartbeat.message or "ok"
        console.print(f"[green]Heartbeat verified:[/green] {heartbeat_text}")

    save_path = save_config(config, resolved_path)
    console.print(f"[green]Configuration saved:[/green] {save_path}")
    raise SystemExit(EXIT_OK)


# ---------------------------------------------------------------------------
# watch-saves
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@main.command(help="Run diagnostics: check paths, connectivity, and service health.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override config file path.",
)
@click.option("--log", "write_log", is_flag=True, help="Also write the report to the log file.")
def doctor(config_path: Path | None, write_log: bool) -> None:
    import subprocess

    from bifrost.logging_setup import _log_dir, setup_file_logging

    console = Console()
    resolved_path = config_path or default_config_path()
    has_errors = False

    if write_log:
        setup_file_logging()
    _log = logging.getLogger("bifrost.doctor")

    def _ok(label: str, detail: str = "") -> None:
        suffix = f"  {detail}" if detail else ""
        console.print(f"  [green]✓[/green] {label}{suffix}")
        if write_log:
            _log.info("doctor OK: %s %s", label, detail)

    def _warn(label: str, detail: str = "") -> None:
        suffix = f"\n    {detail}" if detail else ""
        console.print(f"  [yellow]⚠[/yellow] {label}{suffix}")
        if write_log:
            _log.warning("doctor WARN: %s %s", label, detail)

    def _err(label: str, detail: str = "") -> None:
        nonlocal has_errors
        has_errors = True
        suffix = f"\n    [dim]{detail}[/dim]" if detail else ""
        console.print(f"  [red]✗[/red] {label}{suffix}")
        if write_log:
            _log.error("doctor ERR: %s %s", label, detail)

    console.print("\n[bold]Bifrost Doctor[/bold]\n")

    # ── 1. Config ────────────────────────────────────────────────────────
    console.print("[bold]Config[/bold]")
    try:
        config = load_config(resolved_path)
        _ok("Config file loaded", str(resolved_path))
    except ConfigError as exc:
        _err("Config file", str(exc))
        console.print("\n[red]Cannot continue without a valid config.[/red]")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc

    # ── 2. NAS paths ─────────────────────────────────────────────────────
    console.print("\n[bold]NAS paths[/bold]")
    nas_lib = Path(config.nas.library_path).expanduser()
    nas_res = Path(config.nas.resources_path).expanduser()
    for label, path in [("NAS library", nas_lib), ("NAS resources", nas_res)]:
        if not path.exists():
            _err(label, f"not found: {path}")
        else:
            try:
                count = sum(1 for _ in path.iterdir())
                if count == 0:
                    _warn(label, f"exists but empty (dead mount?): {path}")
                else:
                    _ok(label, str(path))
            except PermissionError:
                _err(label, f"permission denied: {path}")

    # ── 3. Local paths ───────────────────────────────────────────────────
    console.print("\n[bold]Local paths[/bold]")
    local_paths: list[tuple[str, str]] = [
        ("ES-DE ROMs", config.esde.roms_path),
        ("ES-DE gamelists", config.esde.gamelists_path),
        ("BIOS", config.emudeck.bios_path),
        ("Saves", config.emudeck.saves_path),
        ("Media", config.emudeck.media_path),
    ]
    for label, raw in local_paths:
        p = Path(raw).expanduser()
        if p.exists():
            _ok(label, str(p))
        else:
            _warn(label, f"does not exist yet (will be created on first sync): {p}")

    # ── 4. Disk space ────────────────────────────────────────────────────
    console.print("\n[bold]Disk space[/bold]")
    try:
        usage = shutil.disk_usage(Path.home())
        free_gb = usage.free / (1024**3)
        total_gb = usage.total / (1024**3)
        detail = f"{free_gb:.1f} GB free / {total_gb:.1f} GB total"
        if free_gb < 0.5:
            _err("Home partition", detail + " — critically low!")
        elif free_gb < 2:
            _warn("Home partition", detail)
        else:
            _ok("Home partition", detail)
    except OSError as exc:
        _warn("Home partition", str(exc))

    # ── 5. RomM connectivity ─────────────────────────────────────────────
    console.print("\n[bold]RomM connectivity[/bold]")
    try:
        with RommApiClient(config) as client:
            hb = client.heartbeat()
        status_text = hb.status or hb.message or "ok"
        _ok("Heartbeat", f"{config.romm.url} — {status_text}")
    except AuthenticationError as exc:
        _err("Authentication", str(exc))
    except (NetworkError, ApiError) as exc:
        _err("Connectivity", str(exc))

    # ── 6. Systemd units ─────────────────────────────────────────────────
    console.print("\n[bold]Systemd units[/bold]")
    unit_names = [
        "bifrost-sync.timer",
        "bifrost-save-sync.timer",
        "bifrost-save-watch.service",
    ]
    for unit in unit_names:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True, text=True,
        )
        state = result.stdout.strip()
        if state == "active":
            _ok(unit, "active")
        elif state == "inactive":
            _warn(unit, "installed but not active — run: systemctl --user start " + unit)
        else:
            _warn(unit, f"state={state or 'not installed'}")

    # ── 7. Recent log ────────────────────────────────────────────────────
    console.print("\n[bold]Recent log (last 20 lines)[/bold]")
    log_file = _log_dir() / "bifrost.log"
    if log_file.exists():
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-20:]:
            lvl = "red" if " ERROR " in line else "yellow" if " WARNING " in line else "dim"
            console.print(f"  [{lvl}]{line}[/{lvl}]")
    else:
        console.print("  [dim]No log file yet.[/dim]")

    # ── summary ──────────────────────────────────────────────────────────
    console.print("")
    if has_errors:
        console.print("[red bold]Diagnostics found errors — see above.[/red bold]")
        raise SystemExit(EXIT_CONFIG_ERROR)
    console.print("[green bold]All checks passed.[/green bold]")
    raise SystemExit(EXIT_OK)


@main.command(
    name="watch-saves",
    help="Watch the local save directory and trigger save/state sync on changes (for systemd).",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override config file path.",
)
def watch_saves(config_path: Path | None) -> None:
    import shutil

    from bifrost.logging_setup import setup_file_logging
    from bifrost.watcher import run_save_watcher

    setup_file_logging()
    console = Console()
    resolved_path = config_path or default_config_path()

    try:
        cfg = load_config(resolved_path)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc

    saves_path = Path(cfg.emudeck.saves_path).expanduser()
    bifrost_bin = shutil.which("bifrost") or sys.executable + " -m bifrost.cli"

    console.print(f"Watching [cyan]{saves_path}[/cyan] for save file changes...")
    console.print("Press Ctrl+C to stop.\n")

    run_save_watcher(saves_path, bifrost_bin)
    raise SystemExit(EXIT_OK)


# ---------------------------------------------------------------------------
# systemd subcommand group
# ---------------------------------------------------------------------------

_UNIT_FILES = [
    "bifrost-sync.service",
    "bifrost-sync.timer",
    "bifrost-save-sync.service",
    "bifrost-save-sync.timer",
    "bifrost-save-watch.service",
]

_TIMERS = ["bifrost-sync.timer", "bifrost-save-sync.timer"]
_PERSISTENT_SERVICES = ["bifrost-save-watch.service"]


def _systemd_data_dir() -> Path:
    """Return the path to bundled systemd unit templates."""
    return Path(__file__).parent / "data" / "systemd"


def _systemd_user_dir() -> Path:
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config).expanduser() if xdg_config else Path.home() / ".config"
    return base / "systemd" / "user"


def _detect_nas_mount_unit(nas_path: str) -> str | None:
    """Try to find the systemd mount unit that covers nas_path.

    Queries both user and system mounts, returns the unit name (e.g. 'mnt-nas.mount')
    or None if not found.
    """
    import json
    import subprocess

    nas = Path(nas_path).expanduser().resolve()

    for scope in (["--user"], []):
        try:
            out = subprocess.run(
                ["systemctl", *scope, "list-units", "--type=mount", "--output=json", "--no-pager"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode != 0:
                continue
            units = json.loads(out.stdout)
        except (json.JSONDecodeError, subprocess.TimeoutExpired, FileNotFoundError):
            continue

        for entry in units:
            unit_name: str = entry.get("unit", "")
            if not unit_name.endswith(".mount"):
                continue
            # Derive the mount path from the unit name: unescape systemd path encoding
            try:
                escaped = unit_name[: -len(".mount")]
                # systemd replaces / with - (except leading /)
                # Use systemd-escape --unescape if available, else heuristic
                unescape = subprocess.run(
                    ["systemd-escape", "--unescape", "--path", escaped],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                mount_path = Path(unescape.stdout.strip()) if unescape.returncode == 0 else None
            except (subprocess.TimeoutExpired, FileNotFoundError):
                mount_path = None

            if mount_path and (nas == mount_path or str(nas).startswith(str(mount_path) + "/")):
                return unit_name

    return None


def _patch_unit_with_mount(content: str, mount_unit: str) -> str:
    """Inject After=<mount_unit> and BindsTo=<mount_unit> into [Unit] section."""
    marker = "After=network-online.target"
    inject = f"After={mount_unit}\nBindsTo={mount_unit}\n"
    if mount_unit in content:
        return content  # already patched
    return content.replace(marker, inject + marker)


@main.group(name="systemd", help="Manage Bifrost systemd user services and timers.")
def systemd_group() -> None:
    """Systemd unit management."""


@systemd_group.command(name="install", help="Install and enable Bifrost systemd units.")
@click.option("--dry-run", is_flag=True, help="Show what would be done without changing anything.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override config file path (used to detect NAS mount unit).",
)
@click.option(
    "--nas-mount",
    "nas_mount_unit",
    default=None,
    help="Systemd mount unit for the NAS (e.g. mnt-nas.mount). Auto-detected if omitted.",
)
def systemd_install(dry_run: bool, config_path: Path | None, nas_mount_unit: str | None) -> None:
    import subprocess

    console = Console()
    src_dir = _systemd_data_dir()
    dst_dir = _systemd_user_dir()

    if not src_dir.exists():
        console.print(f"[red]Unit templates not found:[/red] {src_dir}")
        raise SystemExit(EXIT_CONFIG_ERROR)

    # ── NAS mount unit detection ──────────────────────────────────────────
    if nas_mount_unit is None:
        resolved_path = config_path or default_config_path()
        try:
            cfg = load_config(resolved_path)
            nas_mount_unit = _detect_nas_mount_unit(cfg.nas.library_path)
        except ConfigError:
            cfg = None
            nas_mount_unit = None

        if nas_mount_unit:
            console.print(f"[green]Detected NAS mount unit:[/green] {nas_mount_unit}")
        elif sys.stdin.isatty():
            console.print(
                "[yellow]Could not auto-detect NAS mount unit.[/yellow]\n"
                "  Service files will use network-online.target only.\n"
                "  If your NAS is mounted via systemd, provide the unit name with --nas-mount\n"
                "  (e.g. --nas-mount mnt-nas.mount). You can find it with:\n"
                "    systemctl list-units --type=mount"
            )
        else:
            console.print(
                "[yellow]NAS mount unit not detected — "
                "services will depend on network only.[/yellow]"
            )

    # ── copy (and optionally patch) unit files ───────────────────────────
    if not dry_run:
        dst_dir.mkdir(parents=True, exist_ok=True)

    for unit in _UNIT_FILES:
        src = src_dir / unit
        dst = dst_dir / unit
        if not src.exists():
            console.print(f"[yellow]Missing template:[/yellow] {unit} — skipping")
            continue

        content = src.read_text(encoding="utf-8")
        if nas_mount_unit and unit.endswith(".service"):
            content = _patch_unit_with_mount(content, nas_mount_unit)

        if dry_run:
            patch_note = f" [dim](+{nas_mount_unit})[/dim]" if nas_mount_unit else ""
            console.print(f"[cyan]would write[/cyan]  {dst}{patch_note}")
        else:
            dst.write_text(content, encoding="utf-8")
            console.print(f"[green]written[/green]  {dst}")

    if dry_run:
        for unit in _TIMERS + _PERSISTENT_SERVICES:
            console.print(f"[cyan]would enable + start[/cyan]  {unit}")
        console.print("\n[cyan]Dry run — no changes made.[/cyan]")
        raise SystemExit(EXIT_OK)

    # ── reload + enable ───────────────────────────────────────────────────
    def _ctl(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["systemctl", "--user", *args], capture_output=True, text=True
        )

    reload = _ctl("daemon-reload")
    if reload.returncode != 0:
        console.print(f"[yellow]daemon-reload warning:[/yellow] {reload.stderr.strip()}")

    for unit in _TIMERS + _PERSISTENT_SERVICES:
        enable = _ctl("enable", "--now", unit)
        if enable.returncode == 0:
            console.print(f"[green]enabled + started[/green]  {unit}")
        else:
            console.print(f"[yellow]enable failed[/yellow]  {unit}: {enable.stderr.strip()}")

    # Enable linger so user services survive logout (important on Steam Deck game mode)
    username = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if username:
        linger = subprocess.run(
            ["loginctl", "enable-linger", username], capture_output=True, text=True
        )
        if linger.returncode == 0:
            console.print(f"[green]linger enabled[/green] for user {username}")
        else:
            console.print(
                f"[yellow]linger not enabled[/yellow] ({linger.stderr.strip()}) "
                "— services may stop on logout"
            )

    console.print(
        "\n[green]Systemd units installed.[/green] "
        "Use [cyan]bifrost systemd status[/cyan] to verify."
    )
    raise SystemExit(EXIT_OK)


@systemd_group.command(name="status", help="Show status of all Bifrost systemd units.")
def systemd_status() -> None:
    import subprocess

    console = Console()
    table = Table(title="Bifrost Systemd Units")
    table.add_column("Unit")
    table.add_column("Loaded")
    table.add_column("Active")
    table.add_column("Last run / trigger")

    for unit in _UNIT_FILES:
        result = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "--property=LoadState,ActiveState,SubState,ExecMainExitTimestamp,NextElapseUSecRealtime"],
            capture_output=True,
            text=True,
        )
        props: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                props[k] = v

        loaded = props.get("LoadState", "?")
        active = props.get("ActiveState", "?")
        sub = props.get("SubState", "")
        active_str = f"{active}/{sub}" if sub and sub != active else active

        if unit.endswith(".timer"):
            ts = props.get("NextElapseUSecRealtime", "")
            last_str = f"next: {ts[:19]}" if ts and ts != "0" else "—"
        else:
            ts = props.get("ExecMainExitTimestamp", "")
            last_str = ts[:19] if ts and ts != "0" else "—"

        color = "green" if active in ("active", "inactive") and loaded == "loaded" else "yellow"
        table.add_row(unit, loaded, f"[{color}]{active_str}[/{color}]", last_str)

    console.print(table)
    raise SystemExit(EXIT_OK)


@systemd_group.command(name="uninstall", help="Disable and remove Bifrost systemd units.")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
def systemd_uninstall(yes: bool) -> None:
    import subprocess

    console = Console()
    dst_dir = _systemd_user_dir()

    if not yes:
        if not Confirm.ask("This will stop and remove all Bifrost systemd units. Continue?"):
            console.print("Aborted.")
            raise SystemExit(EXIT_OK)

    def _ctl(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["systemctl", "--user", *args], capture_output=True, text=True
        )

    for unit in _TIMERS + _PERSISTENT_SERVICES:
        _ctl("disable", "--now", unit)
        console.print(f"[yellow]disabled[/yellow]  {unit}")

    _ctl("daemon-reload")

    for unit in _UNIT_FILES:
        dst = dst_dir / unit
        if dst.exists():
            dst.unlink()
            console.print(f"[red]removed[/red]  {dst}")

    console.print("\n[green]Uninstall complete.[/green]")
    raise SystemExit(EXIT_OK)


if __name__ == "__main__":
    main()
