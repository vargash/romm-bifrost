"""Gamelist generation and merge logic (F3 MVP)."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from bifrost.api.client import RommApiClient
from bifrost.api.models import PlatformSummary
from bifrost.config import AppConfig

ESDE_PRESERVED_TAGS = {
    "playcount",
    "playtime",
    "lastplayed",
    "alternativeEmulator",
    "favorite",
    "hidden",
    "kidgame",
}

BIFROST_OWNED_TAGS = {
    "path",
    "name",
    "desc",
    "developer",
    "publisher",
    "genre",
    "players",
    "lang",
    "region",
    "releasedate",
    "rating",
}


@dataclass(frozen=True)
class GamelistPlan:
    platform_slug: str
    output_path: Path
    total_roms: int
    new_entries: int
    updated_entries: int
    unchanged_entries: int
    removed_entries: int


@dataclass(frozen=True)
class GamelistApplyResult:
    plan: GamelistPlan
    written: bool


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return ", ".join(items) if items else None
    if isinstance(value, dict):
        for key in ("name", "label", "value"):
            if key in value:
                return _as_text(value[key])
        return None
    return None


def _pick_text(rom: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        if key in rom:
            text = _as_text(rom.get(key))
            if text:
                return text
    return None


def _pick_text_from_sources(
    rom: dict[str, Any],
    source_keys: tuple[str, ...],
    *value_keys: str,
) -> str | None:
    for source_key in source_keys:
        source = rom.get(source_key)
        if not isinstance(source, dict):
            continue
        text = _pick_text(source, *value_keys)
        if text:
            return text
    return None


def _pick_raw(rom: dict[str, Any], *keys: str) -> Any | None:
    for key in keys:
        if key in rom and rom.get(key) is not None:
            return rom.get(key)
    return None


def _pick_raw_from_sources(
    rom: dict[str, Any],
    source_keys: tuple[str, ...],
    *value_keys: str,
) -> Any | None:
    for source_key in source_keys:
        source = rom.get(source_key)
        if not isinstance(source, dict):
            continue
        value = _pick_raw(source, *value_keys)
        if value is not None:
            return value
    return None


def _normalize_releasedate(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1_000_000_000_000:
            timestamp = timestamp / 1000.0
        try:
            date_text = datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y%m%d")
            return f"{date_text}T000000"
        except (OSError, OverflowError, ValueError):
            return None

    text = _as_text(value)
    if not text:
        return None

    if re.fullmatch(r"\d{8}T\d{6}", text):
        return f"{text[:8]}T000000"
    if re.fullmatch(r"\d{8}", text):
        return f"{text}T000000"

    digits = re.sub(r"\D", "", text)
    if digits.isdigit() and len(digits) in {10, 13}:
        timestamp = float(digits)
        if len(digits) == 13:
            timestamp = timestamp / 1000.0
        try:
            date_text = datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y%m%d")
            return f"{date_text}T000000"
        except (OSError, OverflowError, ValueError):
            return None

    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.strftime("%Y%m%dT000000")
        except ValueError:
            continue

    iso_date_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso_date_match:
        return f"{iso_date_match.group(1)}{iso_date_match.group(2)}{iso_date_match.group(3)}T000000"

    return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list):
        for item in value:
            number = _coerce_float(item)
            if number is not None:
                return number
        return None
    if isinstance(value, dict):
        for key in ("value", "rating", "score", "average"):
            if key in value:
                number = _coerce_float(value.get(key))
                if number is not None:
                    return number
        return None

    text = _as_text(value)
    if not text:
        return None

    text = text.replace(",", ".").strip()
    percentage = text.endswith("%")
    if percentage:
        text = text[:-1].strip()

    try:
        number = float(text)
    except ValueError:
        return None

    if percentage:
        return number / 100.0
    return number


def _normalize_rating(value: Any) -> str | None:
    number = _coerce_float(value)
    if number is None:
        return None

    if number > 1.0 and number <= 10.0:
        number = number / 10.0
    elif number > 10.0 and number <= 100.0:
        number = number / 100.0

    number = max(0.0, min(1.0, number))
    return f"{number:.2f}"


def _rom_path_value(rom: dict[str, Any]) -> str | None:
    fs_name = _pick_text(rom, "fs_name")
    if fs_name:
        return f"./{fs_name}"

    full_path = _pick_text(rom, "full_path")
    if full_path:
        return f"./{Path(full_path).name}"

    return None


def _build_game_element(rom: dict[str, Any]) -> ET.Element | None:
    path_value = _rom_path_value(rom)
    if not path_value:
        return None

    game = ET.Element("game")

    source_priority = (
        "metadatum",
        "igdb_metadata",
        "ss_metadata",
        "launchbox_metadata",
        "moby_metadata",
        "manual_metadata",
        "gamelist_metadata",
        "flashpoint_metadata",
        "merged_ra_metadata",
        "hltb_metadata",
    )

    developer = _pick_text(rom, "developer", "developers") or _pick_text_from_sources(
        rom,
        source_priority,
        "developer",
        "developers",
        "companies",
    )
    publisher = _pick_text(rom, "publisher", "publishers") or _pick_text_from_sources(
        rom,
        source_priority,
        "publisher",
        "publishers",
        "companies",
    )
    genre = _pick_text(rom, "genre", "genres") or _pick_text_from_sources(
        rom,
        source_priority,
        "genre",
        "genres",
    )
    players = _pick_text(rom, "players", "player_count") or _pick_text_from_sources(
        rom,
        source_priority,
        "players",
        "player_count",
        "max_players",
    )

    release_value = _pick_raw(rom, "releasedate", "release_date", "first_release_date")
    if release_value is None:
        release_value = _pick_raw_from_sources(
            rom,
            source_priority,
            "releasedate",
            "release_date",
            "first_release_date",
            "release_year",
        )

    rating_value = _pick_raw(rom, "rating")
    if rating_value is None:
        rating_value = _pick_raw_from_sources(
            rom,
            source_priority,
            "rating",
            "average_rating",
            "community_rating",
            "total_rating",
            "aggregated_rating",
            "ss_score",
            "moby_score",
        )

    fields: dict[str, str | None] = {
        "path": path_value,
        "name": _pick_text(rom, "name", "fs_name"),
        "desc": _pick_text(rom, "summary", "description"),
        "developer": developer,
        "publisher": publisher,
        "genre": genre,
        "players": players,
        "lang": _pick_text(rom, "languages", "language"),
        "region": _pick_text(rom, "regions", "region"),
        "releasedate": _normalize_releasedate(release_value),
        "rating": _normalize_rating(rating_value),
    }

    for tag, text in fields.items():
        if text is None:
            continue
        child = ET.SubElement(game, tag)
        child.text = text

    return game


def _parse_existing_tree(path: Path) -> ET.Element:
    if not path.exists():
        return ET.Element("gameList")

    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        try:
            raw_xml = path.read_text(encoding="utf-8")
        except OSError:
            return ET.Element("gameList")

        # ES-DE can emit malformed documents with top-level nodes outside
        # <gameList>; wrap and recover the embedded game list when possible.
        raw_xml = re.sub(r"^\s*<\?xml[^>]*\?>", "", raw_xml, count=1)
        wrapped = f"<bifrostRoot>{raw_xml}</bifrostRoot>"
        try:
            wrapped_root = ET.fromstring(wrapped)
        except ET.ParseError:
            return ET.Element("gameList")

        recovered = wrapped_root.find("gameList")
        if recovered is None:
            recovered = wrapped_root.find(".//gameList")
        if recovered is None:
            return ET.Element("gameList")
        return deepcopy(recovered)

    if root.tag != "gameList":
        recovered = root.find("gameList")
        if recovered is None:
            recovered = root.find(".//gameList")
        if recovered is not None:
            return deepcopy(recovered)
        return ET.Element("gameList")
    return root


def _game_path_key(game: ET.Element) -> str | None:
    path_node = game.find("path")
    if path_node is None or path_node.text is None:
        return None
    key = path_node.text.strip()
    return key or None


def _merge_game(existing_game: ET.Element, generated_game: ET.Element) -> bool:
    existing_values: dict[str, str] = {}
    for child in list(existing_game):
        if child.tag not in BIFROST_OWNED_TAGS:
            continue
        text = (child.text or "").strip()
        if text:
            existing_values[child.tag] = text

    generated_values: dict[str, str] = {}
    for child in list(generated_game):
        if child.tag not in BIFROST_OWNED_TAGS:
            continue
        text = (child.text or "").strip()
        if text:
            generated_values[child.tag] = text

    if existing_values == generated_values:
        return False

    # Remove Bifrost-owned tags and reinsert from latest API-derived element.
    for child in list(existing_game):
        if child.tag in BIFROST_OWNED_TAGS:
            existing_game.remove(child)

    for child in list(generated_game):
        if child.tag in BIFROST_OWNED_TAGS and (child.text and child.text.strip()):
            existing_game.append(deepcopy(child))

    return True


def _render_xml(root: ET.Element) -> str:
    root_for_write = deepcopy(root)
    ET.indent(root_for_write, space="  ")
    xml = ET.tostring(root_for_write, encoding="unicode")
    return "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n" + xml + "\n"


def _roms_by_platform(roms: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for rom in roms:
        platform_id = rom.get("platform_id")
        if not isinstance(platform_id, int):
            continue
        grouped.setdefault(platform_id, []).append(rom)
    return grouped


def build_gamelist_plan(config: AppConfig, client: RommApiClient) -> list[GamelistPlan]:
    platforms = client.list_platforms()
    roms = client.list_roms_raw()
    grouped = _roms_by_platform(roms)

    plans: list[GamelistPlan] = []
    gamelists_root = Path(config.esde.gamelists_path).expanduser()

    for platform in platforms:
        plan = _build_platform_plan(platform, grouped.get(platform.id, []), gamelists_root)
        if plan is not None and plan.total_roms > 0:
            plans.append(plan)

    return plans


def _build_platform_plan(
    platform: PlatformSummary,
    roms: list[dict[str, Any]],
    gamelists_root: Path,
) -> GamelistPlan | None:
    if not platform.fs_slug:
        return None

    output_path = gamelists_root / platform.fs_slug / "gamelist.xml"
    root = _parse_existing_tree(output_path)

    existing_by_path: dict[str, ET.Element] = {}
    for game in root.findall("game"):
        key = _game_path_key(game)
        if key:
            existing_by_path[key] = game

    new_entries = 0
    updated_entries = 0
    unchanged_entries = 0
    seen_keys: set[str] = set()

    for rom in roms:
        generated_game = _build_game_element(rom)
        if generated_game is None:
            continue

        key = _game_path_key(generated_game)
        if key is None:
            continue

        seen_keys.add(key)

        existing = existing_by_path.get(key)
        if existing is None:
            root.append(generated_game)
            existing_by_path[key] = generated_game
            new_entries += 1
            continue

        changed = _merge_game(existing, generated_game)
        if changed:
            updated_entries += 1
        else:
            unchanged_entries += 1

    removed_entries = sum(1 for key in existing_by_path if key not in seen_keys)

    total_roms = new_entries + updated_entries + unchanged_entries
    return GamelistPlan(
        platform_slug=platform.fs_slug,
        output_path=output_path,
        total_roms=total_roms,
        new_entries=new_entries,
        updated_entries=updated_entries,
        unchanged_entries=unchanged_entries,
        removed_entries=removed_entries,
    )


def apply_gamelist_plan(config: AppConfig, client: RommApiClient) -> list[GamelistApplyResult]:
    platforms = client.list_platforms()
    roms = client.list_roms_raw()
    grouped = _roms_by_platform(roms)

    results: list[GamelistApplyResult] = []
    gamelists_root = Path(config.esde.gamelists_path).expanduser()

    for platform in platforms:
        if not platform.fs_slug:
            continue

        output_path = gamelists_root / platform.fs_slug / "gamelist.xml"
        root = _parse_existing_tree(output_path)

        existing_by_path: dict[str, ET.Element] = {}
        for game in root.findall("game"):
            key = _game_path_key(game)
            if key:
                existing_by_path[key] = game

        new_entries = 0
        updated_entries = 0
        unchanged_entries = 0
        seen_keys: set[str] = set()

        for rom in grouped.get(platform.id, []):
            generated_game = _build_game_element(rom)
            if generated_game is None:
                continue

            key = _game_path_key(generated_game)
            if key is None:
                continue

            seen_keys.add(key)

            existing = existing_by_path.get(key)
            if existing is None:
                root.append(generated_game)
                existing_by_path[key] = generated_game
                new_entries += 1
                continue

            changed = _merge_game(existing, generated_game)
            if changed:
                updated_entries += 1
            else:
                unchanged_entries += 1

        stale_games = [game for key, game in existing_by_path.items() if key not in seen_keys]
        for game in stale_games:
            root.remove(game)
        removed_entries = len(stale_games)

        total_roms = new_entries + updated_entries + unchanged_entries
        plan = GamelistPlan(
            platform_slug=platform.fs_slug,
            output_path=output_path,
            total_roms=total_roms,
            new_entries=new_entries,
            updated_entries=updated_entries,
            unchanged_entries=unchanged_entries,
            removed_entries=removed_entries,
        )

        if total_roms == 0:
            continue

        written = False
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(_render_xml(root), encoding="utf-8")
        written = True

        results.append(GamelistApplyResult(plan=plan, written=written))

    return results
