"""Tests for the cross-core compat resolution helpers in bifrost.saves.profiles."""

from __future__ import annotations

from bifrost.saves.profiles import (
    find_compatible_profile,
    resolve_core_mapping,
)


def test_find_compatible_profile_returns_curated_alias() -> None:
    found = find_compatible_profile("mednafen_psx_hw")
    assert found is not None
    profile, alias = found
    assert profile.romm_emulator == "duckstation"
    assert alias.core_slug == "mednafen_psx_hw"
    assert alias.expected_size_bytes == 131072


def test_find_compatible_profile_returns_none_for_unknown_slug() -> None:
    assert find_compatible_profile("totally_unknown_core") is None
    assert find_compatible_profile(None) is None


def test_resolve_core_mapping_rejects_unknown_local_emulator() -> None:
    resolution = resolve_core_mapping(
        platform="psx", remote_core="mednafen_psx_hw", local_emulator="not_a_real_emulator"
    )
    assert resolution.ok is False
    assert "no local profile named" in resolution.rejected_reason


def test_resolve_core_mapping_rejects_unsupported_profile() -> None:
    resolution = resolve_core_mapping(
        platform="ps2", remote_core="some_core", local_emulator="pcsx2"
    )
    assert resolution.ok is False
    assert "unsupported" in resolution.rejected_reason


def test_resolve_core_mapping_rejects_platform_mismatch() -> None:
    resolution = resolve_core_mapping(
        platform="ps2", remote_core="mednafen_psx_hw", local_emulator="duckstation"
    )
    assert resolution.ok is False
    assert "platform mismatch" in resolution.rejected_reason


def test_resolve_core_mapping_verified_for_curated_pair() -> None:
    resolution = resolve_core_mapping(
        platform="psx", remote_core="mednafen_psx_hw", local_emulator="duckstation"
    )
    assert resolution.ok is True
    assert resolution.verified is True
    assert resolution.target_profile is not None
    assert resolution.target_profile.romm_emulator == "duckstation"
    assert resolution.expected_size_bytes == 131072
    assert resolution.note == "both use the standard 128KB PS1 per-game memory card image"


def test_resolve_core_mapping_caller_size_overrides_curated_default() -> None:
    resolution = resolve_core_mapping(
        platform="psx",
        remote_core="mednafen_psx_hw",
        local_emulator="duckstation",
        expected_size_bytes=999,
    )
    assert resolution.ok is True
    assert resolution.verified is True
    assert resolution.expected_size_bytes == 999


def test_resolve_core_mapping_unverified_for_custom_pair() -> None:
    resolution = resolve_core_mapping(
        platform="psx", remote_core="some_other_psx_core", local_emulator="duckstation"
    )
    assert resolution.ok is True
    assert resolution.verified is False
    assert "not verified by Bifrost" in resolution.note
    assert resolution.expected_size_bytes is None


def test_resolve_core_mapping_accepts_multi_platform_target_for_any_platform() -> None:
    resolution = resolve_core_mapping(
        platform="n64", remote_core="some_n64_core", local_emulator="retroarch"
    )
    assert resolution.ok is True
    assert resolution.target_profile is not None
    assert resolution.target_profile.romm_emulator == "retroarch"
