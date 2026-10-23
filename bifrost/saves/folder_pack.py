"""Deterministic zip packing for directory-based saves (e.g. PCSX2 folder memory cards).

RomM stores one file per save, so a game's save directories are shipped as a
single zip whose top-level entries are the directories themselves (a game can
own several, e.g. one per in-game save slot). The archive is byte-for-byte
reproducible for identical directory contents (sorted entries, fixed
timestamps and permissions) so the content hash is stable and RomM's
hash-based dedup keeps working.
"""

from __future__ import annotations

import io
import os
import shutil
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

_FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_FILE_MODE = 0o100644 << 16
_DIR_MODE = (0o040755 << 16) | 0x10


class UnsafeArchiveError(ValueError):
    """Raised when an archive entry would escape the extraction directory."""


def _entries(root: Path) -> list[tuple[str, Path]]:
    """Return (arcname, path) for root and everything under it, sorted by arcname."""
    items: list[tuple[str, Path]] = [(root.name, root)]
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dirnames.sort()
        base = Path(dirpath)
        for name in sorted(dirnames) + sorted(filenames):
            path = base / name
            items.append((path.relative_to(root.parent).as_posix(), path))
    return sorted(items, key=lambda item: item[0])


def pack_dirs(roots: Sequence[Path]) -> bytes:
    """Zip the given directories, each kept as its own top-level entry.

    All roots must share a parent. A single root yields the same bytes as the
    historical one-directory archive, so saves already on the server stay valid.
    """
    entries = sorted(
        (entry for root in roots for entry in _entries(root)), key=lambda item: item[0]
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for arcname, path in entries:
            if path.is_dir():
                info = zipfile.ZipInfo(arcname + "/", date_time=_FIXED_DATE_TIME)
                info.external_attr = _DIR_MODE
                archive.writestr(info, b"")
            else:
                info = zipfile.ZipInfo(arcname, date_time=_FIXED_DATE_TIME)
                info.external_attr = _FILE_MODE
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes(), compresslevel=6)
    return buffer.getvalue()


def pack_dir(root: Path) -> bytes:
    """Zip a single directory (the directory itself is the sole top-level entry)."""
    return pack_dirs([root])


def archive_dir_names(data: bytes) -> list[str]:
    """Sorted names of the top-level directories inside a packed archive."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        tops = {PurePosixPath(n).parts[0] for n in archive.namelist() if n.strip("/")}
    return sorted(tops)


def unpack_to_dirs(data: bytes, dest_parent: Path) -> list[Path]:
    """Extract a packed archive under dest_parent, replacing any existing directories.

    The new trees are extracted into a staging directory first; each previous
    directory (if any) is kept as <name>.bak (replacing an older backup) before
    the swap. Returns the final directory paths.
    """
    names = archive_dir_names(data)
    if not names:
        raise UnsafeArchiveError("archive has no top-level directory")
    for name in names:
        if name in {"", ".", ".."} or "/" in name or "\\" in name:
            raise UnsafeArchiveError(f"unsafe top-level name: {name!r}")

    dest_parent.mkdir(parents=True, exist_ok=True)
    staging = dest_parent / ".bifrost-unpack.part"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    targets: list[Path] = []
    try:
        staging_root = staging.resolve()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                member_path = PurePosixPath(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise UnsafeArchiveError(f"unsafe archive entry: {member.filename!r}")
                out = (staging / Path(*member_path.parts)).resolve()
                if staging_root != out and staging_root not in out.parents:
                    raise UnsafeArchiveError(f"unsafe archive entry: {member.filename!r}")
                if member.is_dir():
                    out.mkdir(parents=True, exist_ok=True)
                else:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(archive.read(member))
        for name in names:
            if not (staging / name).is_dir():
                raise UnsafeArchiveError(f"archive has no directory {name!r}")
        for name in names:
            target = dest_parent / name
            backup = dest_parent / f"{name}.bak"
            if target.exists():
                shutil.rmtree(backup, ignore_errors=True)
                os.replace(target, backup)
            os.replace(staging / name, target)
            targets.append(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return targets
