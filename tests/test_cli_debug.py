from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bifrost.cli import main


def test_debug_saves_reports_files_and_folders(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    save_root = tmp_path / "saves"
    retroarch_root = save_root / "retroarch" / "saves"
    duckstation_root = save_root / "duckstation" / "states"
    retroarch_root.mkdir(parents=True, exist_ok=True)
    duckstation_root.mkdir(parents=True, exist_ok=True)
    (retroarch_root / "Castlevania.srm").write_bytes(b"save-data")
    (duckstation_root / "SLES-01504_resume.sav").write_bytes(b"state-data")
    config_path.write_text(
        f"""
[romm]
url = "http://romm.local"
client_token = "rmm_token"
device_id = "device-1"

[emudeck]
saves_path = "{save_root}"
""".strip(),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    runner = CliRunner()
    result = runner.invoke(main, ["debug", "saves", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Configured saves_path" in result.output
    assert "Top-level folders" in result.output
    assert "retroarch" in result.output
    assert "duckstation" in result.output
    assert "Castlevania.srm" in result.output
    assert "SLES-01504_resume.sav" in result.output
