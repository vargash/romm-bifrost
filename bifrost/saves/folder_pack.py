"""Deterministic zip packing for directory-based saves (e.g. PCSX2 folder memory cards).

RomM stores one file per save, so a per-game save directory is shipped as a
single zip whose only top-level entry is the directory itself. The archive is
byte-for-byte reproducible for identical directory contents (sorted entries,
fixed timestamps and permissions) so the content hash is stable and RomM's
hash-based dedup keeps working.
"""

from __future__ import annotations

import io
import os
import shutil
import zipfile
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


def pack_dir(root: Path) -> bytes:
    """Zip root (including the directory itself as the sole top-level entry)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for arcname, path in _entries(root):
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


def archive_dir_name(data: bytes) -> str:
    """Name of the single top-level directory inside a packed archive."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        tops = {PurePosixPath(n).parts[0] for n in archive.namelist() if n.strip("/")}
    if len(tops) != 1:
        raise UnsafeArchiveError(f"expected one top-level directory, found {sorted(tops)}")
    return tops.pop()


def unpack_to_dir(data: bytes, dest_parent: Path) -> Path:
    """Extract a packed archive under dest_parent, replacing any existing directory.

    The new tree is extracted next to the target first; the previous directory (if
    any) is kept as <name>.bak (replacing an older backup) before the swap.
    Returns the final directory path.
    """
    name = archive_dir_name(data)
    if name in {"", ".", ".."} or "/" in name or "\\" in name:
        raise UnsafeArchiveError(f"unsafe top-level name: {name!r}")

    dest_parent.mkdir(parents=True, exist_ok=True)
    target = dest_parent / name
    staging = dest_parent / f"{name}.part"
    backup = dest_parent / f"{name}.bak"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
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
        extracted = staging / name
        if not extracted.is_dir():
            raise UnsafeArchiveError(f"archive has no directory {name!r}")
        if target.exists():
            shutil.rmtree(backup, ignore_errors=True)
            os.replace(target, backup)
        os.replace(extracted, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return target
