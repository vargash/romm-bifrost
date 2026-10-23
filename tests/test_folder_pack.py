from __future__ import annotations

import hashlib
import io
import os
import zipfile
from pathlib import Path

import pytest

from bifrost.saves.folder_pack import (
    UnsafeArchiveError,
    archive_dir_names,
    pack_dir,
    pack_dirs,
    unpack_to_dirs,
)


def _make_game_dir(root: Path, name: str = "BASLUS-20000") -> Path:
    game = root / name
    (game / "sub").mkdir(parents=True)
    (game / "icon.sys").write_bytes(b"icon")
    (game / "sub" / "data.bin").write_bytes(b"\x00\x01\x02" * 100)
    return game


def test_pack_is_deterministic_across_mtimes_and_locations(tmp_path: Path) -> None:
    a = _make_game_dir(tmp_path / "a")
    b = _make_game_dir(tmp_path / "b")
    os.utime(b / "icon.sys", (1_000_000_000, 1_000_000_000))
    os.utime(b / "sub" / "data.bin", (1_700_000_000, 1_700_000_000))

    assert hashlib.md5(pack_dir(a)).hexdigest() == hashlib.md5(pack_dir(b)).hexdigest()


def test_pack_changes_when_content_changes(tmp_path: Path) -> None:
    game = _make_game_dir(tmp_path)
    before = pack_dir(game)
    (game / "icon.sys").write_bytes(b"changed")

    assert pack_dir(game) != before


def test_roundtrip_restores_tree(tmp_path: Path) -> None:
    src = _make_game_dir(tmp_path / "src")
    data = pack_dir(src)
    assert archive_dir_names(data) == ["BASLUS-20000"]

    [out] = unpack_to_dirs(data, tmp_path / "dst")

    assert out == tmp_path / "dst" / "BASLUS-20000"
    assert (out / "icon.sys").read_bytes() == b"icon"
    assert (out / "sub" / "data.bin").read_bytes() == b"\x00\x01\x02" * 100
    assert pack_dir(out) == data


def test_unpack_replaces_existing_dir_and_keeps_backup(tmp_path: Path) -> None:
    src = _make_game_dir(tmp_path / "src")
    data = pack_dir(src)
    dst = tmp_path / "dst"
    old = dst / "BASLUS-20000"
    old.mkdir(parents=True)
    (old / "stale.bin").write_bytes(b"old")

    unpack_to_dirs(data, dst)

    assert not (dst / "BASLUS-20000" / "stale.bin").exists()
    assert (dst / "BASLUS-20000.bak" / "stale.bin").read_bytes() == b"old"
    assert not (dst / ".bifrost-unpack.part").exists()


@pytest.mark.parametrize("entry", ["../evil.txt", "/abs/evil.txt", "GAME/../../evil.txt"])
def test_unpack_rejects_path_traversal(tmp_path: Path, entry: str) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("GAME/ok.bin", b"ok")
        archive.writestr(entry, b"evil")

    with pytest.raises(UnsafeArchiveError):
        unpack_to_dirs(buffer.getvalue(), tmp_path / "dst")

    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "dst" / "GAME").exists()
    assert not (tmp_path / "dst" / ".bifrost-unpack.part").exists()


def test_pack_dirs_roundtrip_restores_every_directory(tmp_path: Path) -> None:
    src = tmp_path / "src"
    slots = [_make_game_dir(src, f"BISLPM-66750FF120{i}") for i in range(3)]
    (slots[1] / "icon.sys").write_bytes(b"slot-1")
    data = pack_dirs(slots)

    assert archive_dir_names(data) == [s.name for s in slots]

    out = unpack_to_dirs(data, tmp_path / "dst")

    assert [o.name for o in out] == [s.name for s in slots]
    assert (tmp_path / "dst" / slots[1].name / "icon.sys").read_bytes() == b"slot-1"
    assert pack_dirs(out) == data


def test_pack_dirs_is_independent_of_input_order(tmp_path: Path) -> None:
    a, b = _make_game_dir(tmp_path, "A-1"), _make_game_dir(tmp_path, "B-2")

    assert pack_dirs([a, b]) == pack_dirs([b, a])


def test_pack_dirs_single_root_matches_pack_dir(tmp_path: Path) -> None:
    game = _make_game_dir(tmp_path)

    assert pack_dirs([game]) == pack_dir(game)
