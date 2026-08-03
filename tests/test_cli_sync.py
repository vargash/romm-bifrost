import re
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from bifrost import cli
from bifrost.config import CacheConfig
from bifrost.symlink_manager import OrphanPlatformFolder

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    return " ".join(_ANSI_RE.sub("", output).split())


def test_sync_dry_run_uses_plan_and_prints_summary(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("[romm]\nbase_url='http://localhost'\napi_token='token'\n")

    class DummyConfig:
        class romm:
            timeout_seconds = 10.0

        class sync:
            parallel_workers = 1
            prune_orphan_platforms = False
            orphan_platform_strategy = "ask"

        class nas:
            library_path = "/tmp/nas"

        cache = CacheConfig(enabled=False)

    class DummyClient:
        def __init__(self, config, **kwargs):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def list_platforms(self):
            return []

    class DummyOp:
        def __init__(self):
            self.category = "rom"
            self.destination = Path("/tmp/dest")
            self.target = Path("/tmp/target")

    class DummyResult:
        def __init__(self, action: str):
            self.action = action
            self.detail = ""
            self.operation = DummyOp()

    monkeypatch.setattr(cli, "load_config", lambda _: DummyConfig())
    monkeypatch.setattr(cli, "RommApiClient", DummyClient)
    monkeypatch.setattr(cli, "run_nas_check", lambda _cfg: cli.PreflightResult())
    monkeypatch.setattr(cli, "plan_symlink_operations", lambda _cfg, _client: [DummyOp()])
    monkeypatch.setattr(cli, "plan_m3u_operations", lambda _cfg, _client: [])
    monkeypatch.setattr(cli, "plan_stale_removals", lambda _cfg, _ops: [])
    monkeypatch.setattr(cli, "find_orphan_platform_folders", lambda _cfg, _platforms: [])
    monkeypatch.setattr(cli, "evaluate_operations", lambda ops, workers=1: [DummyResult("create") for _ in ops])

    runner = CliRunner()
    result = runner.invoke(cli.main, ["sync", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Bifrost Sync (dry-run)" in result.output
    assert "Dry-run mode" in result.output


def test_sync_apply_calls_apply_operations(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("[romm]\nbase_url='http://localhost'\napi_token='token'\n")

    class DummyConfig:
        class romm:
            timeout_seconds = 10.0

        class sync:
            parallel_workers = 1
            prune_orphan_platforms = False
            orphan_platform_strategy = "ask"

        class nas:
            library_path = "/tmp/nas"

        cache = CacheConfig(enabled=False)

    class DummyClient:
        def __init__(self, config, **kwargs):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def list_platforms(self):
            return []

    class DummyOp:
        def __init__(self):
            self.category = "bios"
            self.destination = Path("/tmp/bios")
            self.target = Path("/tmp/src")

    class DummyResult:
        def __init__(self, action: str):
            self.action = action
            self.detail = ""
            self.operation = DummyOp()

    called = {"apply": False}

    monkeypatch.setattr(cli, "load_config", lambda _: DummyConfig())
    monkeypatch.setattr(cli, "RommApiClient", DummyClient)
    monkeypatch.setattr(cli, "run_nas_check", lambda _cfg: cli.PreflightResult())
    monkeypatch.setattr(cli, "run_sync_preflight", lambda _cfg: cli.PreflightResult())
    monkeypatch.setattr(cli, "plan_symlink_operations", lambda _cfg, _client: [DummyOp()])
    monkeypatch.setattr(cli, "plan_m3u_operations", lambda _cfg, _client: [])
    monkeypatch.setattr(cli, "plan_stale_removals", lambda _cfg, _ops: [])
    monkeypatch.setattr(cli, "find_orphan_platform_folders", lambda _cfg, _platforms: [])


    def fake_apply(_op):
        called["apply"] = True
        return DummyResult("create")

    monkeypatch.setattr(cli, "apply_operation", fake_apply)

    runner = CliRunner()
    result = runner.invoke(cli.main, ["sync", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0
    assert called["apply"] is True
    assert "Bifrost Sync (apply)" in result.output


# ---------------------------------------------------------------------------
# Orphan platform folder pruning
# ---------------------------------------------------------------------------


def _dummy_config(tmp_path: Path, *, prune: bool, strategy: str) -> SimpleNamespace:
    return SimpleNamespace(
        romm=SimpleNamespace(timeout_seconds=10.0),
        sync=SimpleNamespace(
            parallel_workers=1,
            prune_orphan_platforms=prune,
            orphan_platform_strategy=strategy,
        ),
        nas=SimpleNamespace(library_path=str(tmp_path / "nas")),
        cache=CacheConfig(enabled=False),
    )


class _DummyClient:
    def __init__(self, config, **kwargs):
        self.config = config

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def list_platforms(self):
        return []


def _patch_common_sync(monkeypatch, config_path: Path, config: SimpleNamespace) -> None:
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "RommApiClient", _DummyClient)
    monkeypatch.setattr(cli, "run_nas_check", lambda _cfg: cli.PreflightResult())
    monkeypatch.setattr(cli, "run_sync_preflight", lambda _cfg: cli.PreflightResult())
    monkeypatch.setattr(cli, "plan_symlink_operations", lambda _cfg, _client: [])
    monkeypatch.setattr(cli, "plan_m3u_operations", lambda _cfg, _client: [])
    monkeypatch.setattr(cli, "plan_stale_removals", lambda _cfg, _ops: [])


def test_sync_reports_orphans_without_pruning(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("")
    orphan_dir = tmp_path / "roms" / "n64"
    orphan_dir.mkdir(parents=True)

    config = _dummy_config(tmp_path, prune=False, strategy="ask")
    _patch_common_sync(monkeypatch, config_path, config)
    monkeypatch.setattr(
        cli,
        "find_orphan_platform_folders",
        lambda _cfg, _platforms: [OrphanPlatformFolder(path=orphan_dir, safe=True)],
    )

    removed = {"called": False}
    monkeypatch.setattr(
        cli, "apply_orphan_removal", lambda *a, **k: removed.__setitem__("called", True)
    )

    runner = CliRunner()
    result = runner.invoke(cli.main, ["sync", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0
    output = _plain(result.output)
    assert "orphan platform folder" in output
    assert removed["called"] is False
    assert orphan_dir.exists()


def test_sync_dry_run_suggests_prune_command_when_orphans_found(monkeypatch, tmp_path: Path):
    """Without --apply, the final summary must suggest the exact command to prune orphans."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("")
    orphan_dir = tmp_path / "roms" / "n64"
    orphan_dir.mkdir(parents=True)

    config = _dummy_config(tmp_path, prune=False, strategy="ask")
    _patch_common_sync(monkeypatch, config_path, config)
    monkeypatch.setattr(
        cli,
        "find_orphan_platform_folders",
        lambda _cfg, _platforms: [OrphanPlatformFolder(path=orphan_dir, safe=True)],
    )

    runner = CliRunner()
    result = runner.invoke(cli.main, ["sync", "--config", str(config_path)])

    assert result.exit_code == 0
    output = _plain(result.output)
    assert "Dry-run mode" in output
    assert "bifrost sync --apply --prune-orphans" in output
    assert orphan_dir.exists()


def test_sync_dry_run_no_prune_suggestion_when_no_orphans(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("")

    config = _dummy_config(tmp_path, prune=False, strategy="ask")
    _patch_common_sync(monkeypatch, config_path, config)
    monkeypatch.setattr(cli, "find_orphan_platform_folders", lambda _cfg, _platforms: [])

    runner = CliRunner()
    result = runner.invoke(cli.main, ["sync", "--config", str(config_path)])

    assert result.exit_code == 0
    output = _plain(result.output)
    assert "--prune-orphans" not in output


def test_sync_prune_orphans_headless_ask_skips(monkeypatch, tmp_path: Path):
    """Under CliRunner there's no real TTY, so strategy='ask' must safely no-op."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("")
    orphan_dir = tmp_path / "roms" / "n64"
    orphan_dir.mkdir(parents=True)

    config = _dummy_config(tmp_path, prune=True, strategy="ask")
    _patch_common_sync(monkeypatch, config_path, config)
    monkeypatch.setattr(
        cli,
        "find_orphan_platform_folders",
        lambda _cfg, _platforms: [OrphanPlatformFolder(path=orphan_dir, safe=True)],
    )

    runner = CliRunner()
    result = runner.invoke(cli.main, ["sync", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0
    assert orphan_dir.exists()


def test_sync_apply_orphan_strategy_remove_no_prompt(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("")
    orphan_dir = tmp_path / "roms" / "n64"
    orphan_dir.mkdir(parents=True)

    config = _dummy_config(tmp_path, prune=True, strategy="remove")
    _patch_common_sync(monkeypatch, config_path, config)
    monkeypatch.setattr(
        cli,
        "find_orphan_platform_folders",
        lambda _cfg, _platforms: [OrphanPlatformFolder(path=orphan_dir, safe=True)],
    )

    def fail_confirm(*_a, **_k):
        raise AssertionError("Confirm.ask must not be called for strategy='remove'")

    monkeypatch.setattr(cli, "Confirm", SimpleNamespace(ask=fail_confirm))

    runner = CliRunner()
    result = runner.invoke(cli.main, ["sync", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0
    assert not orphan_dir.exists()


def test_sync_apply_orphan_strategy_skip_no_removal(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("")
    orphan_dir = tmp_path / "roms" / "n64"
    orphan_dir.mkdir(parents=True)

    config = _dummy_config(tmp_path, prune=True, strategy="skip")
    _patch_common_sync(monkeypatch, config_path, config)
    monkeypatch.setattr(
        cli,
        "find_orphan_platform_folders",
        lambda _cfg, _platforms: [OrphanPlatformFolder(path=orphan_dir, safe=True)],
    )

    runner = CliRunner()
    result = runner.invoke(cli.main, ["sync", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0
    assert orphan_dir.exists()


def test_sync_prune_orphans_flag_without_apply_is_dry_run_preview(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("")
    orphan_dir = tmp_path / "roms" / "n64"
    orphan_dir.mkdir(parents=True)

    config = _dummy_config(tmp_path, prune=False, strategy="ask")
    _patch_common_sync(monkeypatch, config_path, config)
    monkeypatch.setattr(
        cli,
        "find_orphan_platform_folders",
        lambda _cfg, _platforms: [OrphanPlatformFolder(path=orphan_dir, safe=True)],
    )

    runner = CliRunner()
    result = runner.invoke(
        cli.main, ["sync", "--config", str(config_path), "--prune-orphans"]
    )

    assert result.exit_code == 0
    assert "Dry-run mode" in result.output
    assert orphan_dir.exists()  # dry-run: nothing removed regardless of flag


def test_sync_unsafe_orphan_headless_never_removed(monkeypatch, tmp_path: Path):
    """Unsafe orphans (real content) must never be force-removed without an interactive review."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("")
    unsafe_dir = tmp_path / "roms" / "emulators"
    unsafe_dir.mkdir(parents=True)
    (unsafe_dir / "ryujinx.sh").write_text("#!/bin/sh\n")

    config = _dummy_config(tmp_path, prune=True, strategy="remove")
    _patch_common_sync(monkeypatch, config_path, config)
    monkeypatch.setattr(
        cli,
        "find_orphan_platform_folders",
        lambda _cfg, _platforms: [
            OrphanPlatformFolder(path=unsafe_dir, safe=False, reason="contains file: ryujinx.sh")
        ],
    )

    def fail_confirm(*_a, **_k):
        raise AssertionError("Confirm.ask must not be called headlessly")

    monkeypatch.setattr(cli, "Confirm", SimpleNamespace(ask=fail_confirm))

    runner = CliRunner()
    result = runner.invoke(cli.main, ["sync", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0
    assert unsafe_dir.exists()
    assert (unsafe_dir / "ryujinx.sh").exists()


def test_sync_dry_run_reports_unsafe_orphans_needing_review(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("")
    unsafe_dir = tmp_path / "roms" / "emulators"
    unsafe_dir.mkdir(parents=True)

    config = _dummy_config(tmp_path, prune=False, strategy="ask")
    _patch_common_sync(monkeypatch, config_path, config)
    monkeypatch.setattr(
        cli,
        "find_orphan_platform_folders",
        lambda _cfg, _platforms: [
            OrphanPlatformFolder(path=unsafe_dir, safe=False, reason="contains file: ryujinx.sh")
        ],
    )

    runner = CliRunner()
    result = runner.invoke(cli.main, ["sync", "--config", str(config_path)])

    assert result.exit_code == 0
    output = _plain(result.output)
    assert "contain real files" in output
    assert "bifrost sync --apply --prune-orphans" in output
