from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bifrost.cache import BifrostCache
from bifrost.cli import EXIT_OK, main
from bifrost.config import AppConfig, CacheConfig, RommConfig, save_config


def make_config_file(tmp_path: Path, cache_dir: str) -> Path:
    cfg = AppConfig(
        romm=RommConfig(url="http://romm.local", client_token="rmm_token"),
        cache=CacheConfig(cache_dir=cache_dir, ttl_roms_hours=24, ttl_platforms_hours=24),
    )
    config_file = tmp_path / "config.toml"
    save_config(cfg, config_file)
    return config_file


def test_cache_status_shows_all_keys(tmp_path: Path) -> None:
    cache_dir = str(tmp_path / "cache")
    config_file = make_config_file(tmp_path, cache_dir)

    runner = CliRunner()
    result = runner.invoke(main, ["cache", "status", "--config", str(config_file)])

    assert result.exit_code == EXIT_OK
    assert "firmware" in result.output
    assert "platforms" in result.output
    assert "roms" in result.output


def test_cache_status_shows_fresh_after_population(tmp_path: Path) -> None:
    cache_dir = str(tmp_path / "cache")
    config_file = make_config_file(tmp_path, cache_dir)

    cache = BifrostCache(CacheConfig(cache_dir=cache_dir, ttl_roms_hours=24))
    cache.set("roms", [{"id": 1}, {"id": 2}])

    runner = CliRunner()
    result = runner.invoke(main, ["cache", "status", "--config", str(config_file)])

    assert result.exit_code == EXIT_OK
    assert "fresh" in result.output
    assert "2" in result.output  # item_count


def test_cache_invalidate_all(tmp_path: Path) -> None:
    cache_dir = str(tmp_path / "cache")
    config_file = make_config_file(tmp_path, cache_dir)

    cache = BifrostCache(CacheConfig(cache_dir=cache_dir, ttl_roms_hours=24))
    cache.set("roms", [{"id": 1}])
    cache.set("platforms", [{"id": 2}])

    runner = CliRunner()
    result = runner.invoke(main, ["cache", "invalidate", "--config", str(config_file)])

    assert result.exit_code == EXIT_OK
    assert "all keys" in result.output

    # Verify data actually gone
    fresh_cache = BifrostCache(CacheConfig(cache_dir=cache_dir, ttl_roms_hours=24))
    assert fresh_cache.get("roms") is None
    assert fresh_cache.get("platforms") is None


def test_cache_invalidate_single_key(tmp_path: Path) -> None:
    cache_dir = str(tmp_path / "cache")
    config_file = make_config_file(tmp_path, cache_dir)

    cfg = CacheConfig(cache_dir=cache_dir, ttl_roms_hours=24, ttl_platforms_hours=24)
    cache = BifrostCache(cfg)
    cache.set("roms", [{"id": 1}])
    cache.set("platforms", [{"id": 2}])

    runner = CliRunner()
    result = runner.invoke(
        main, ["cache", "invalidate", "--key", "roms", "--config", str(config_file)]
    )

    assert result.exit_code == EXIT_OK
    assert "roms" in result.output

    fresh_cache = BifrostCache(cfg)
    assert fresh_cache.get("roms") is None
    assert fresh_cache.get("platforms") is not None


def test_cache_status_without_config_uses_defaults(tmp_path: Path) -> None:
    """status should work even when config file is absent."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["cache", "status", "--config", str(tmp_path / "nonexistent.toml")],
    )
    # Should exit OK and show the table (all keys as never-fetched)
    assert result.exit_code == EXIT_OK
    assert "roms" in result.output


def test_cache_invalidate_without_config_uses_defaults(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["cache", "invalidate", "--config", str(tmp_path / "nonexistent.toml")],
    )
    assert result.exit_code == EXIT_OK
