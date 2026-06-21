from __future__ import annotations

import json
from pathlib import Path

from bifrost.cache import BifrostCache, merge_by_id
from bifrost.config import CacheConfig


def make_cache(tmp_path: Path, **overrides: object) -> BifrostCache:
    cfg = CacheConfig(cache_dir=str(tmp_path), **overrides)  # type: ignore[arg-type]
    return BifrostCache(cfg)


# ---------------------------------------------------------------------------
# merge_by_id
# ---------------------------------------------------------------------------


def test_merge_by_id_adds_new_items() -> None:
    old = [{"id": 1, "name": "A"}]
    delta = [{"id": 2, "name": "B"}]
    result = merge_by_id(old, delta)
    ids = {item["id"] for item in result}
    assert ids == {1, 2}


def test_merge_by_id_updates_existing_items() -> None:
    old = [{"id": 1, "name": "old"}]
    delta = [{"id": 1, "name": "new"}]
    result = merge_by_id(old, delta)
    assert len(result) == 1
    assert result[0]["name"] == "new"


def test_merge_by_id_ignores_items_without_integer_id() -> None:
    old = [{"id": 1, "name": "A"}]
    delta = [{"name": "no_id"}, {"id": "str_id", "name": "bad"}]
    result = merge_by_id(old, delta)
    assert len(result) == 1
    assert result[0]["id"] == 1


def test_merge_by_id_empty_delta_returns_old() -> None:
    old = [{"id": 1}, {"id": 2}]
    result = merge_by_id(old, [])
    assert {item["id"] for item in result} == {1, 2}


def test_merge_by_id_empty_old_returns_delta() -> None:
    delta = [{"id": 3, "name": "C"}]
    result = merge_by_id([], delta)
    assert len(result) == 1
    assert result[0]["id"] == 3


# ---------------------------------------------------------------------------
# BifrostCache.get / set / miss
# ---------------------------------------------------------------------------


def test_get_returns_none_on_miss(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    assert cache.get("roms") is None


def test_set_then_get_returns_data(tmp_path: Path) -> None:
    cache = make_cache(tmp_path, ttl_roms_hours=24)
    data = [{"id": 1, "name": "ROM"}]
    cache.set("roms", data)
    result = cache.get("roms")
    assert result == data


def test_get_returns_none_when_expired(tmp_path: Path) -> None:
    # ttl=0 means any positive age exceeds TTL
    cache = make_cache(tmp_path, ttl_roms_hours=0)
    cache.set("roms", [{"id": 1}])
    assert cache.get("roms") is None


def test_get_returns_none_on_format_version_mismatch(tmp_path: Path) -> None:
    cache = make_cache(tmp_path, ttl_roms_hours=24)
    cache.set("roms", [{"id": 1}])
    # Corrupt the format version in metadata
    meta_path = tmp_path / "cache_meta.json"
    meta = json.loads(meta_path.read_text())
    meta["cache_format_version"] = 999
    meta_path.write_text(json.dumps(meta))
    assert cache.get("roms") is None


def test_set_writes_json_file(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    cache.set("platforms", [{"id": 5}])
    data_file = tmp_path / "platforms.json"
    assert data_file.exists()
    loaded = json.loads(data_file.read_text())
    assert loaded == [{"id": 5}]


def test_set_writes_metadata(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    cache.set("platforms", [{"id": 1}, {"id": 2}], full_fetch=True)
    meta = json.loads((tmp_path / "cache_meta.json").read_text())
    assert "platforms" in meta
    assert meta["platforms"]["item_count"] == 2
    assert meta["platforms"]["full_fetch"] is True
    assert "fetched_at" in meta["platforms"]


def test_set_leaves_no_tmp_file(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    cache.set("roms", [{"id": 1}])
    assert not list(tmp_path.glob("*.tmp"))


def test_set_full_fetch_false_recorded(tmp_path: Path) -> None:
    cache = make_cache(tmp_path, ttl_roms_hours=24)
    cache.set("roms", [{"id": 1}], full_fetch=False)
    meta = json.loads((tmp_path / "cache_meta.json").read_text())
    assert meta["roms"]["full_fetch"] is False


# ---------------------------------------------------------------------------
# get_stale
# ---------------------------------------------------------------------------


def test_get_stale_returns_data_when_expired(tmp_path: Path) -> None:
    cache = make_cache(tmp_path, ttl_roms_hours=0)
    data = [{"id": 1}]
    cache.set("roms", data)
    assert cache.get("roms") is None      # expired via normal get
    assert cache.get_stale("roms") == data  # still readable via get_stale


def test_get_stale_returns_none_on_miss(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    assert cache.get_stale("firmware") is None


# ---------------------------------------------------------------------------
# last_fetched_at
# ---------------------------------------------------------------------------


def test_last_fetched_at_returns_none_on_miss(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    assert cache.last_fetched_at("roms") is None


def test_last_fetched_at_returns_datetime_after_set(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    cache = make_cache(tmp_path)
    before = datetime.now(UTC)
    cache.set("platforms", [{"id": 1}])
    after = datetime.now(UTC)
    ts = cache.last_fetched_at("platforms")
    assert ts is not None
    assert before <= ts <= after


def test_last_fetched_at_survives_expired_ttl(tmp_path: Path) -> None:
    cache = make_cache(tmp_path, ttl_roms_hours=0)
    cache.set("roms", [{"id": 1}])
    # Even with ttl=0, last_fetched_at still returns a datetime
    assert cache.last_fetched_at("roms") is not None


# ---------------------------------------------------------------------------
# invalidate
# ---------------------------------------------------------------------------


def test_invalidate_single_key_removes_data(tmp_path: Path) -> None:
    cache = make_cache(tmp_path, ttl_roms_hours=24, ttl_platforms_hours=24)
    cache.set("roms", [{"id": 1}])
    cache.set("platforms", [{"id": 2}])
    cache.invalidate("roms")
    assert cache.get("roms") is None
    assert cache.get("platforms") is not None


def test_invalidate_all_removes_all_keys(tmp_path: Path) -> None:
    cache = make_cache(tmp_path, ttl_roms_hours=24, ttl_platforms_hours=24, ttl_firmware_hours=24)
    cache.set("roms", [{"id": 1}])
    cache.set("platforms", [{"id": 2}])
    cache.set("firmware", [{"id": 3}])
    cache.invalidate()
    assert cache.get("roms") is None
    assert cache.get("platforms") is None
    assert cache.get("firmware") is None


def test_invalidate_removes_json_file(tmp_path: Path) -> None:
    cache = make_cache(tmp_path, ttl_roms_hours=24)
    cache.set("roms", [{"id": 1}])
    cache.invalidate("roms")
    assert not (tmp_path / "roms.json").exists()


def test_invalidate_nonexistent_key_is_safe(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    cache.invalidate("roms")  # should not raise


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_shows_all_keys(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    statuses = cache.status()
    assert set(statuses.keys()) == {"firmware", "platforms", "roms"}


def test_status_fresh_key_not_expired(tmp_path: Path) -> None:
    cache = make_cache(tmp_path, ttl_roms_hours=24)
    cache.set("roms", [{"id": 1}, {"id": 2}])
    st = cache.status()["roms"]
    assert not st.is_expired
    assert st.item_count == 2
    assert st.fetched_at is not None
    assert st.age_seconds is not None and st.age_seconds >= 0


def test_status_expired_key(tmp_path: Path) -> None:
    cache = make_cache(tmp_path, ttl_roms_hours=0)
    cache.set("roms", [{"id": 1}])
    st = cache.status()["roms"]
    assert st.is_expired


def test_status_missing_key_shows_no_data(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    st = cache.status()["roms"]
    assert st.fetched_at is None
    assert st.item_count == 0
    assert st.age_seconds is None
    assert st.is_expired


# ---------------------------------------------------------------------------
# different keys use their own TTLs
# ---------------------------------------------------------------------------


def test_separate_ttls_per_key(tmp_path: Path) -> None:
    cache = make_cache(tmp_path, ttl_roms_hours=0, ttl_platforms_hours=24)
    cache.set("roms", [{"id": 1}])
    cache.set("platforms", [{"id": 2}])
    assert cache.get("roms") is None        # ttl=0 → expired
    assert cache.get("platforms") is not None  # ttl=24 → fresh


# ---------------------------------------------------------------------------
# cache_dir creation
# ---------------------------------------------------------------------------


def test_cache_dir_created_on_set(tmp_path: Path) -> None:
    subdir = tmp_path / "nested" / "bifrost"
    cache = BifrostCache(CacheConfig(cache_dir=str(subdir)))
    cache.set("roms", [])
    assert subdir.is_dir()
