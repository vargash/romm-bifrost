"""Multi-disc ROM detection and M3U playlist generation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bifrost.config import AppConfig

DISC_PATTERN = re.compile(
    r"[\[(](?:Disc|Disk|CD)\s*(\d+)[\])]",
    re.IGNORECASE,
)


def extract_disc_number(name: str) -> int | None:
    """Return the disc number from a filename or display name, or None if absent."""
    match = DISC_PATTERN.search(name)
    return int(match.group(1)) if match else None


def strip_disc_marker(name: str) -> str:
    """Remove the disc marker token and collapse any resulting double spaces."""
    stripped = DISC_PATTERN.sub("", name).strip()
    return re.sub(r" {2,}", " ", stripped)


def base_name_for_m3u(fs_name: str) -> str | None:
    """Return the .m3u stem for a disc file, or None if it has no disc marker."""
    if extract_disc_number(fs_name) is None:
        return None
    return strip_disc_marker(Path(fs_name).stem)


def group_multidisc_roms_raw(
    roms: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group raw ROM dicts by base name (disc marker stripped from fs_name stem).

    Returns only groups with ≥2 discs.  Values are sorted ascending by disc number.
    """
    candidates: dict[str, list[dict[str, Any]]] = {}

    for rom in roms:
        fs_name = rom.get("fs_name")
        if not isinstance(fs_name, str) or not fs_name:
            continue
        if extract_disc_number(fs_name) is None:
            continue
        base = strip_disc_marker(Path(fs_name).stem)
        candidates.setdefault(base, []).append(rom)

    return {
        base: sorted(discs, key=lambda r: extract_disc_number(r.get("fs_name", "")) or 0)
        for base, discs in candidates.items()
        if len(discs) >= 2
    }


def _disc_filenames_for_folder_rom(client: Any, rom_id: int) -> list[str]:
    """Fetch per-ROM detail and return disc file names sorted by disc number.

    Calls GET /api/roms/<id> which returns the ``files`` list by default.
    Returns [] on any API error or when fewer than 2 disc files are found.
    """
    try:
        detail = client.get_rom(rom_id)
    except Exception:
        return []

    files: list[dict[str, Any]] = detail.get("files") or []
    disc_files = [
        f["file_name"]
        for f in files
        if isinstance(f.get("file_name"), str)
        and extract_disc_number(f["file_name"]) is not None
    ]
    return sorted(disc_files, key=lambda n: extract_disc_number(n) or 0)


def detect_folder_multidisc_from_api(
    roms_raw: list[dict[str, Any]],
    client: Any,
) -> dict[int, list[str]]:
    """Return ``{rom_id: [sorted disc filenames]}`` for folder-based multi-disc ROMs.

    Uses the ``has_multiple_files`` flag to filter candidates, then fetches
    ``GET /api/roms/<id>`` for each one to get the actual file names.
    Only ROM IDs with ≥2 disc files are included in the result.
    """
    result: dict[int, list[str]] = {}
    for rom in roms_raw:
        if not rom.get("has_multiple_files"):
            continue
        rom_id = rom.get("id")
        if not isinstance(rom_id, int):
            continue
        disc_files = _disc_filenames_for_folder_rom(client, rom_id)
        if len(disc_files) >= 2:
            result[rom_id] = disc_files
    return result


@dataclass(frozen=True)
class M3uOperation:
    """Plan to write an M3U playlist file for a multi-disc game."""

    category: str  # always "m3u"
    destination: Path
    disc_filenames: tuple[str, ...]

    @property
    def target(self) -> Path:
        """Compatibility shim: M3U files are self-contained, target == destination."""
        return self.destination

    @property
    def is_dir(self) -> bool:
        return False


@dataclass(frozen=True)
class M3uResult:
    """Result of evaluating or applying one M3uOperation."""

    operation: M3uOperation
    action: str
    detail: str = ""


def _m3u_content(disc_filenames: tuple[str, ...]) -> str:
    return "\n".join(disc_filenames) + "\n"


def plan_m3u_operations(config: AppConfig, client: Any) -> list[M3uOperation]:
    """Plan M3U playlist files for all multi-disc games in the RomM library.

    Handles two storage models:

    * **Flat-file**: multiple ROM entries each with a disc marker in ``fs_name``
      (e.g. ``FF7 (Disc 1).bin``, ``FF7 (Disc 2).bin``).  Grouped by the
      stripped base name; M3U lists the bare filenames.

    * **Folder-based**: a single ROM entry with ``has_multiple_files=True``.
      Disc files are discovered via ``GET /api/roms/<id>``; M3U lists
      ``<folder>/<file>`` relative paths so emulators can resolve them through
      the folder symlink created by ``bifrost sync``.
    """
    platforms = client.list_platforms()
    roms_raw: list[dict[str, Any]] = client.list_roms_raw()

    platform_slug_by_id: dict[int, str] = {p.id: p.fs_slug for p in platforms if p.fs_slug}
    esde_roms_root = Path(config.esde.roms_path).expanduser()

    ops: list[M3uOperation] = []

    # ── Flat-file multi-disc ─────────────────────────────────────────────────
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for rom in roms_raw:
        platform_id = rom.get("platform_id")
        fs_name = rom.get("fs_name")
        if not isinstance(platform_id, int) or not isinstance(fs_name, str) or not fs_name:
            continue
        if extract_disc_number(fs_name) is None:
            continue
        base = strip_disc_marker(Path(fs_name).stem)
        groups.setdefault((platform_id, base), []).append(rom)

    for (platform_id, base_name), disc_roms in groups.items():
        if len(disc_roms) < 2:
            continue
        slug = platform_slug_by_id.get(platform_id)
        if not slug:
            continue
        sorted_discs = sorted(
            disc_roms, key=lambda r: extract_disc_number(r.get("fs_name", "")) or 0
        )
        disc_filenames = tuple(r["fs_name"] for r in sorted_discs if r.get("fs_name"))
        ops.append(
            M3uOperation(
                category="m3u",
                destination=esde_roms_root / slug / f"{base_name}.m3u",
                disc_filenames=disc_filenames,
            )
        )

    # ── Folder-based multi-disc ──────────────────────────────────────────────
    folder_discs = detect_folder_multidisc_from_api(roms_raw, client)
    for rom in roms_raw:
        rom_id = rom.get("id")
        if not isinstance(rom_id, int) or rom_id not in folder_discs:
            continue
        fs_name = rom.get("fs_name")
        platform_id = rom.get("platform_id")
        if not isinstance(fs_name, str) or not isinstance(platform_id, int):
            continue
        slug = platform_slug_by_id.get(platform_id)
        if not slug:
            continue
        # Paths relative to the M3U file; disc files sit one level below via
        # the folder symlink that bifrost sync creates.
        disc_filenames = tuple(f"{fs_name}/{f}" for f in folder_discs[rom_id])
        ops.append(
            M3uOperation(
                category="m3u",
                destination=esde_roms_root / slug / f"{fs_name}.m3u",
                disc_filenames=disc_filenames,
            )
        )

    return ops


def evaluate_m3u_operation(op: M3uOperation) -> M3uResult:
    """Check whether the M3U file needs to be created or updated."""
    expected = _m3u_content(op.disc_filenames)
    if op.destination.exists():
        try:
            current = op.destination.read_text(encoding="utf-8")
        except OSError:
            return M3uResult(op, "replace")
        return M3uResult(op, "ok" if current == expected else "replace")
    return M3uResult(op, "create")


def apply_m3u_operation(op: M3uOperation) -> M3uResult:
    """Write or update the M3U playlist file atomically."""
    eval_result = evaluate_m3u_operation(op)
    if eval_result.action == "ok":
        return eval_result

    try:
        op.destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return M3uResult(op, "error", f"Failed to create parent directory: {exc}")

    content = _m3u_content(op.disc_filenames)
    tmp = op.destination.with_suffix(".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(op.destination)
    except OSError as exc:
        return M3uResult(op, "error", f"Failed to write M3U file: {exc}")

    return M3uResult(op, eval_result.action)
