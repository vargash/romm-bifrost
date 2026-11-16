from __future__ import annotations

from pathlib import Path

from bifrost.config import AppConfig, EsdeConfig, RommConfig
from bifrost.esde_check import hidden_links


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        romm=RommConfig(url="http://x", client_token="rmm_1"),
        esde=EsdeConfig(
            roms_path=str(tmp_path / "roms"), custom_systems_path=str(tmp_path / "custom")
        ),
    )


def test_hidden_links_flags_unaccepted_extensions_and_folders(tmp_path: Path) -> None:
    (tmp_path / "custom").mkdir()
    (tmp_path / "custom" / "es_systems.xml").write_text(
        "<systemList><system><name>switch</name>"
        "<extension>.nsp .NSP .xci</extension></system>"
        "<system><name>snes</name><extension>.sfc</extension></system></systemList>",
        encoding="utf-8",
    )
    switch = tmp_path / "roms" / "switch"
    switch.mkdir(parents=True)
    for name in ("Good.nsp", "Upper.NSP", "Packed.nsz", "FolderGame"):
        (switch / name).symlink_to(tmp_path)
    (switch / "metadata.txt").touch()  # not a Bifrost link

    assert hidden_links(_config(tmp_path)) == {"switch": ["FolderGame", "Packed.nsz"]}


def test_hidden_links_empty_without_es_systems(tmp_path: Path) -> None:
    assert hidden_links(_config(tmp_path)) == {}
