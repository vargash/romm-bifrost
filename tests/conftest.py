"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect XDG_CACHE_HOME to a per-test tmpdir.

    Prevents cross-test pollution when the disk cache is enabled by default —
    otherwise a cache hit from one test can shadow the mocked HTTP responses
    of another test.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg_cache"))
