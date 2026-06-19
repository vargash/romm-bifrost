"""Bifrost CLI entrypoint."""

from __future__ import annotations

import os
import platform as sys_platform
import re
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn
from rich.table import Table

from bifrost.api.client import RommApiClient, exchange_pairing_code
from bifrost.api.models import DeviceCreatePayload
from bifrost.config import (
    AppConfig,
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
from bifrost.save_sync import build_save_sync_preview, execute_save_sync_preview
from bifrost.state_sync import build_state_sync_preview, execute_state_sync_preview
from bifrost.symlink_manager import (
    apply_operation,
    evaluate_operation,
    plan_symlink_operations,
)

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_AUTH_ERROR = 3
EXIT_API_ERROR = 4

PAIRING_CODE_PATTERN = re.compile(r"^[A-Z0-9]{4}-?[A-Z0-9]{4}$", re.IGNORECASE)


@click.group(help="Bifrost: RomM <-> ES-DE bridge CLI")
def main() -> None:
    """Main CLI group."""


@main.group(help="Debug helpers for inspecting local paths and RomM discovery.")
def debug() -> None:
    """Debug command group."""


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
            for dirpath, dirnames, filenames in os.walk(child, followlinks=True):
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

    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
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
def gamelist(config_path: Path | None, apply: bool) -> None:
    """Generate merge-safe gamelist.xml plans or files."""

    console = Console()
    resolved_path = config_path or default_config_path()
    rows: list[dict[str, Any]] = []

    try:
        config = load_config(resolved_path)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc

    try:
        with RommApiClient(config) as client:
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
def sync(config_path: Path | None, apply: bool) -> None:
    """Create a dry-run plan or apply symlink operations."""

    console = Console()
    resolved_path = config_path or default_config_path()

    try:
        config = load_config(resolved_path)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc

    try:
        with RommApiClient(config) as client:
            ops = plan_symlink_operations(config, client)
    except AuthenticationError as exc:
        console.print(f"[red]Authentication error:[/red] {exc}")
        raise SystemExit(EXIT_AUTH_ERROR) from exc
    except (NetworkError, ApiError) as exc:
        console.print(f"[red]API error:[/red] {exc}")
        raise SystemExit(EXIT_API_ERROR) from exc

    results: list[Any]
    if apply:
        if not ops:
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
                task_id = progress.add_task("Applying symlinks", total=len(ops))
                for op in ops:
                    results.append(apply_operation(op))
                    progress.advance(task_id)
        mode_label = "apply"
    else:
        results = [evaluate_operation(op) for op in ops]
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
    summary.add_row("Asset dir symlinks", str(by_category.get("asset-dir", 0)))
    summary.add_row("Create", str(counts.get("create", 0)))
    summary.add_row("Replace", str(counts.get("replace", 0)))
    summary.add_row("Already OK", str(counts.get("ok", 0)))
    summary.add_row("Conflicts", str(counts.get("conflict", 0)))
    summary.add_row("Errors", str(counts.get("error", 0)))
    console.print(summary)

    details = Table(title="Sync Operation Preview")
    details.add_column("Category")
    details.add_column("Action")
    details.add_column("Destination")
    details.add_column("Target")
    details.add_column("Detail")

    preview_results = sorted(results, key=lambda item: (item.action != "error", item.action))
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
def save_sync(
    config_path: Path | None,
    device_id: str | None,
    apply: bool,
    only_files: tuple[str, ...],
) -> None:
    """Scan local saves and preview the RomM sync negotiation."""

    console = Console()
    resolved_path = config_path or default_config_path()

    try:
        config = load_config(resolved_path)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc

    try:
        with RommApiClient(config) as client:
            preview = build_save_sync_preview(
                config,
                client,
                device_id=device_id,
                file_filters=list(only_files),
            )
            execution = None
            if apply:
                execution = execute_save_sync_preview(
                    config,
                    client,
                    preview,
                    file_filters=list(only_files),
                )
    except AuthenticationError as exc:
        console.print(f"[red]Authentication error:[/red] {exc}")
        raise SystemExit(EXIT_AUTH_ERROR) from exc
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc
    except (NetworkError, ApiError) as exc:
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
    if only_files:
        summary.add_row("File filter", ", ".join(only_files))
    console.print(summary)

    operations = Table(title="Sync Operations")
    operations.add_column("Action")
    operations.add_column("ROM")
    operations.add_column("Save")
    operations.add_column("Reason")
    for operation in selected_operations[:25]:
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
def state_sync(
    config_path: Path | None,
    apply: bool,
    only_files: tuple[str, ...],
) -> None:
    """Scan local state files and preview/apply state sync actions."""

    console = Console()
    resolved_path = config_path or default_config_path()

    try:
        config = load_config(resolved_path)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc

    try:
        with RommApiClient(config) as client:
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
        console.print(f"[red]Authentication error:[/red] {exc}")
        raise SystemExit(EXIT_AUTH_ERROR) from exc
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise SystemExit(EXIT_CONFIG_ERROR) from exc
    except (NetworkError, ApiError) as exc:
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
    for operation in selected_operations[:25]:
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
    device_name_value = (name or click.prompt("Device name", default=f"Bifrost on {hostname_value}")).strip()
    platform_value = (platform or click.prompt("Device platform", default=sys_platform.system().lower())).strip()
    client_value = (client_name or click.prompt("Client name", default="bifrost")).strip()
    client_version_value = (
        client_version or click.prompt("Client version", default="0.1.0")
    ).strip()
    hostname_reported = (hostname or click.prompt("Hostname", default=hostname_value)).strip()
    sync_mode_value = (sync_mode or click.prompt("Sync mode", default=config.sync.sync_mode)).strip()

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
    """Run setup using manual token or device pairing flow."""

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

    url_value = (romm_url or click.prompt("RomM URL")).strip().rstrip("/")

    if not url_value:
        console.print("[red]Configuration error:[/red] RomM URL cannot be empty.")
        raise SystemExit(EXIT_CONFIG_ERROR)

    if pair and client_token:
        console.print("[red]Configuration error:[/red] Use either --pair or --token, not both.")
        raise SystemExit(EXIT_CONFIG_ERROR)

    if not pair and pair_code:
        console.print("[red]Configuration error:[/red] --pair-code requires --pair.")
        raise SystemExit(EXIT_CONFIG_ERROR)

    if pair:
        code_value = (pair_code or click.prompt("RomM Pairing Code (8 digits)")).strip()
        if not PAIRING_CODE_PATTERN.fullmatch(code_value):
            console.print(
                "[red]Configuration error:[/red] Pairing code must be 8 alphanumeric characters,"
                " optionally formatted with a hyphen (AAAA-BBBB)."
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
        token_value = (client_token or click.prompt("RomM Client Token", hide_input=True)).strip()

    if not token_value.startswith("rmm_"):
        console.print("[red]Configuration error:[/red] RomM token must start with 'rmm_'.")
        raise SystemExit(EXIT_CONFIG_ERROR)

    base_config = existing_config or AppConfig(
        romm=RommConfig(url=url_value, client_token=token_value, device_id="")
    )

    should_prompt_paths = configure_paths
    if should_prompt_paths:
        nas_library_value = (
            nas_library_path
            if nas_library_path is not None
            else click.prompt("NAS library path", default=base_config.nas.library_path)
        )
        nas_resources_value = (
            nas_resources_path
            if nas_resources_path is not None
            else click.prompt("NAS resources path", default=base_config.nas.resources_path)
        )
        esde_roms_value = (
            esde_roms_path
            if esde_roms_path is not None
            else click.prompt("ES-DE ROMs path", default=base_config.esde.roms_path)
        )
        emudeck_bios_value = (
            emudeck_bios_path
            if emudeck_bios_path is not None
            else click.prompt("EmuDeck BIOS path", default=base_config.emudeck.bios_path)
        )
        emudeck_media_value = (
            emudeck_media_path
            if emudeck_media_path is not None
            else click.prompt("EmuDeck media path", default=base_config.emudeck.media_path)
        )
    else:
        nas_library_value = nas_library_path or base_config.nas.library_path
        nas_resources_value = nas_resources_path or base_config.nas.resources_path
        esde_roms_value = esde_roms_path or base_config.esde.roms_path
        emudeck_bios_value = emudeck_bios_path or base_config.emudeck.bios_path
        emudeck_media_value = emudeck_media_path or base_config.emudeck.media_path

    config = AppConfig(
        romm=RommConfig(url=url_value, client_token=token_value, device_id=""),
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


if __name__ == "__main__":
    main()
