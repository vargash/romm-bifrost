from pathlib import Path

from click.testing import CliRunner

from bifrost import cli


def test_sync_dry_run_uses_plan_and_prints_summary(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("[romm]\nbase_url='http://localhost'\napi_token='token'\n")

    class DummyConfig:
        pass

    class DummyClient:
        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

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
    monkeypatch.setattr(cli, "plan_symlink_operations", lambda _cfg, _client: [DummyOp()])
    monkeypatch.setattr(cli, "evaluate_operation", lambda _op: DummyResult("create"))

    runner = CliRunner()
    result = runner.invoke(cli.main, ["sync", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Bifrost Sync (dry-run)" in result.output
    assert "Dry-run mode" in result.output


def test_sync_apply_calls_apply_operations(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("[romm]\nbase_url='http://localhost'\napi_token='token'\n")

    class DummyConfig:
        pass

    class DummyClient:
        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

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
    monkeypatch.setattr(cli, "plan_symlink_operations", lambda _cfg, _client: [DummyOp()])

    def fake_apply(_op):
        called["apply"] = True
        return DummyResult("create")

    monkeypatch.setattr(cli, "apply_operation", fake_apply)

    runner = CliRunner()
    result = runner.invoke(cli.main, ["sync", "--config", str(config_path), "--apply"])

    assert result.exit_code == 0
    assert called["apply"] is True
    assert "Bifrost Sync (apply)" in result.output
