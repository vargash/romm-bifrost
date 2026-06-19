from pathlib import Path

from click.testing import CliRunner

from bifrost import cli


def test_gamelist_dry_run_outputs_summary(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[romm]\nurl='http://localhost'\nclient_token='rmm_token'\n",
        encoding="utf-8",
    )

    class DummyConfig:
        pass

    class DummyClient:
        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyPlan:
        platform_slug = "nes"
        output_path = Path("/tmp/gamelist.xml")
        total_roms = 3
        new_entries = 1
        updated_entries = 1
        unchanged_entries = 1
        removed_entries = 0

    monkeypatch.setattr(cli, "load_config", lambda _: DummyConfig())
    monkeypatch.setattr(cli, "RommApiClient", DummyClient)
    monkeypatch.setattr(cli, "build_gamelist_plan", lambda _cfg, _c: [DummyPlan()])

    runner = CliRunner()
    result = runner.invoke(cli.main, ["gamelist", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Bifrost Gamelist (dry-run)" in result.output
    assert "Dry-run mode" in result.output


def test_gamelist_apply_outputs_written_count(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[romm]\nurl='http://localhost'\nclient_token='rmm_token'\n",
        encoding="utf-8",
    )

    class DummyConfig:
        pass

    class DummyClient:
        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyPlan:
        platform_slug = "nes"
        output_path = Path("/tmp/gamelist.xml")
        total_roms = 2
        new_entries = 2
        updated_entries = 0
        unchanged_entries = 0
        removed_entries = 0

    class DummyResult:
        plan = DummyPlan()
        written = True

    monkeypatch.setattr(cli, "load_config", lambda _: DummyConfig())
    monkeypatch.setattr(cli, "RommApiClient", DummyClient)
    monkeypatch.setattr(cli, "apply_gamelist_plan", lambda _cfg, _c: [DummyResult()])

    runner = CliRunner()
    result = runner.invoke(cli.main, ["gamelist", "--apply", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Bifrost Gamelist (apply)" in result.output
    assert "Files written" in result.output
