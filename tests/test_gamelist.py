from pathlib import Path

from bifrost.api.models import PlatformSummary
from bifrost.config import AppConfig, EmudeckConfig, EsdeConfig, NasConfig, RommConfig
from bifrost.gamelist import apply_gamelist_plan, build_gamelist_plan


class StubClient:
    def list_platforms(self):
        return [PlatformSummary(id=1, fs_slug="nes", name="NES")]

    def list_roms_raw(self, use_cache: bool = True):
        return [
            {
                "id": 10,
                "platform_id": 1,
                "fs_name": "Mario.nes",
                "name": "Super Mario Bros",
                "summary": "Classic platform game",
                "developer": "Nintendo",
                "publisher": "Nintendo",
                "genres": ["Platform"],
                "players": 2,
            }
        ]


class StubClientWithCrLfSummary(StubClient):
    def list_roms_raw(self, use_cache: bool = True):
        return [
            {
                "id": 10,
                "platform_id": 1,
                "fs_name": "Mario.nes",
                "name": "Super Mario Bros",
                "summary": "Line one\r\n\r\nLine two",
                "developer": "Nintendo",
                "publisher": "Nintendo",
                "genres": ["Platform"],
                "players": 2,
            }
        ]


class StubClientNestedMetadata(StubClient):
    def list_roms_raw(self, use_cache: bool = True):
        return [
            {
                "id": 10,
                "platform_id": 1,
                "fs_name": "Mario.nes",
                "name": "Super Mario Bros",
                "summary": "Classic platform game",
                "metadatum": {
                    "companies": ["Nintendo"],
                    "genres": ["Platform"],
                    "player_count": "2",
                    "first_release_date": 502243200,
                    "average_rating": 78,
                },
            }
        ]


class StubClientMetadataPrecedence(StubClient):
    def list_roms_raw(self, use_cache: bool = True):
        return [
            {
                "id": 10,
                "platform_id": 1,
                "fs_name": "Mario.nes",
                "name": "Super Mario Bros",
                "summary": "Classic platform game",
                "developer": "Top Level Dev",
                "publisher": "Top Level Pub",
                "genres": ["Top Level Genre"],
                "players": 3,
                "release_date": "1990-12-01",
                "rating": 0.45,
                "metadatum": {
                    "companies": ["Nested Company"],
                    "genres": ["Nested Genre"],
                    "player_count": "1",
                    "first_release_date": 502243200,
                    "average_rating": 90,
                },
            }
        ]


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        romm=RommConfig(url="http://localhost", client_token="rmm_token"),
        nas=NasConfig(
            library_path=str(tmp_path / "library"),
            resources_path=str(tmp_path / "resources"),
        ),
        esde=EsdeConfig(
            roms_path=str(tmp_path / "roms"),
            gamelists_path=str(tmp_path / "gamelists"),
        ),
        emudeck=EmudeckConfig(
            bios_path=str(tmp_path / "bios"),
            media_path=str(tmp_path / "media"),
        ),
    )


def test_build_gamelist_plan_detects_new_entries(tmp_path: Path):
    config = make_config(tmp_path)
    plans = build_gamelist_plan(config, StubClient())

    assert len(plans) == 1
    assert plans[0].platform_slug == "nes"
    assert plans[0].new_entries == 1
    assert plans[0].updated_entries == 0


def test_apply_gamelist_plan_preserves_esde_owned_tags(tmp_path: Path):
    config = make_config(tmp_path)
    gamelist_path = tmp_path / "gamelists" / "nes" / "gamelist.xml"
    gamelist_path.parent.mkdir(parents=True, exist_ok=True)
    gamelist_path.write_text(
        """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<gameList>
  <game>
    <path>./Mario.nes</path>
    <name>Old Name</name>
    <favorite>true</favorite>
  </game>
</gameList>
""",
        encoding="utf-8",
    )

    results = apply_gamelist_plan(config, StubClient())

    assert len(results) == 1
    assert results[0].plan.updated_entries == 1
    xml = gamelist_path.read_text(encoding="utf-8")
    assert "<name>Super Mario Bros</name>" in xml
    assert "<favorite>true</favorite>" in xml


def test_apply_gamelist_plan_recovers_from_malformed_xml(tmp_path: Path):
    config = make_config(tmp_path)
    gamelist_path = tmp_path / "gamelists" / "nes" / "gamelist.xml"
    gamelist_path.parent.mkdir(parents=True, exist_ok=True)
    gamelist_path.write_text("<gameList></gameList><broken>", encoding="utf-8")

    results = apply_gamelist_plan(config, StubClient())

    assert len(results) == 1
    assert results[0].written is True
    xml = gamelist_path.read_text(encoding="utf-8")
    assert "<gameList>" in xml
    assert "./Mario.nes" in xml


def test_build_plan_recovers_esde_top_level_node_and_keeps_idempotency(tmp_path: Path):
        config = make_config(tmp_path)
        gamelist_path = tmp_path / "gamelists" / "nes" / "gamelist.xml"
        gamelist_path.parent.mkdir(parents=True, exist_ok=True)
        gamelist_path.write_text(
                """<?xml version="1.0"?>
<alternativeEmulator>
    <label>DuckStation (Standalone)</label>
</alternativeEmulator>
<gameList>
    <game>
        <path>./Mario.nes</path>
        <name>Super Mario Bros</name>
    </game>
</gameList>
""",
                encoding="utf-8",
        )

        plans = build_gamelist_plan(config, StubClient())

        assert len(plans) == 1
        assert plans[0].new_entries == 0
        assert plans[0].updated_entries == 1
        assert plans[0].unchanged_entries == 0


def test_build_gamelist_plan_is_idempotent_after_apply(tmp_path: Path):
    config = make_config(tmp_path)

    apply_results = apply_gamelist_plan(config, StubClient())
    assert len(apply_results) == 1
    assert apply_results[0].plan.new_entries == 1

    plans = build_gamelist_plan(config, StubClient())
    assert len(plans) == 1
    assert plans[0].new_entries == 0
    assert plans[0].updated_entries == 0
    assert plans[0].unchanged_entries == 1


def test_build_gamelist_plan_is_idempotent_with_crlf_descriptions(tmp_path: Path):
    config = make_config(tmp_path)

    apply_results = apply_gamelist_plan(config, StubClientWithCrLfSummary())
    assert len(apply_results) == 1

    plans = build_gamelist_plan(config, StubClientWithCrLfSummary())
    assert len(plans) == 1
    assert plans[0].updated_entries == 0
    assert plans[0].unchanged_entries == 1


def test_apply_gamelist_plan_uses_nested_metadata_fields(tmp_path: Path):
    config = make_config(tmp_path)

    results = apply_gamelist_plan(config, StubClientNestedMetadata())

    assert len(results) == 1
    gamelist_path = tmp_path / "gamelists" / "nes" / "gamelist.xml"
    xml = gamelist_path.read_text(encoding="utf-8")
    assert "<developer>Nintendo</developer>" in xml
    assert "<publisher>Nintendo</publisher>" in xml
    assert "<genre>Platform</genre>" in xml
    assert "<players>2</players>" in xml
    assert "<releasedate>19851201T000000</releasedate>" in xml
    assert "<rating>0.78</rating>" in xml


def test_apply_gamelist_plan_prefers_top_level_metadata(tmp_path: Path):
    config = make_config(tmp_path)

    apply_gamelist_plan(config, StubClientMetadataPrecedence())

    gamelist_path = tmp_path / "gamelists" / "nes" / "gamelist.xml"
    xml = gamelist_path.read_text(encoding="utf-8")
    assert "<developer>Top Level Dev</developer>" in xml
    assert "<publisher>Top Level Pub</publisher>" in xml
    assert "<genre>Top Level Genre</genre>" in xml
    assert "<players>3</players>" in xml
    assert "<releasedate>19901201T000000</releasedate>" in xml
    assert "<rating>0.45</rating>" in xml


def test_build_gamelist_plan_detects_removed_entries(tmp_path: Path):
        config = make_config(tmp_path)
        gamelist_path = tmp_path / "gamelists" / "nes" / "gamelist.xml"
        gamelist_path.parent.mkdir(parents=True, exist_ok=True)
        gamelist_path.write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<gameList>
    <game>
        <path>./Mario.nes</path>
        <name>Super Mario Bros</name>
    </game>
    <game>
        <path>./Legacy.m3u</path>
        <name>Legacy Entry</name>
    </game>
</gameList>
""",
                encoding="utf-8",
        )

        plans = build_gamelist_plan(config, StubClient())

        assert len(plans) == 1
        assert plans[0].new_entries == 0
        assert plans[0].updated_entries == 1
        assert plans[0].unchanged_entries == 0
        assert plans[0].removed_entries == 1


def test_apply_gamelist_plan_prunes_removed_entries(tmp_path: Path):
        config = make_config(tmp_path)
        gamelist_path = tmp_path / "gamelists" / "nes" / "gamelist.xml"
        gamelist_path.parent.mkdir(parents=True, exist_ok=True)
        gamelist_path.write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<gameList>
    <game>
        <path>./Mario.nes</path>
        <name>Super Mario Bros</name>
    </game>
    <game>
        <path>./Legacy.m3u</path>
        <name>Legacy Entry</name>
    </game>
</gameList>
""",
                encoding="utf-8",
        )

        results = apply_gamelist_plan(config, StubClient())

        assert len(results) == 1
        assert results[0].plan.removed_entries == 1
        xml = gamelist_path.read_text(encoding="utf-8")
        assert "./Mario.nes" in xml
        assert "./Legacy.m3u" not in xml
