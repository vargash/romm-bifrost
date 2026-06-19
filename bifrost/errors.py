"""Domain exceptions used across the Bifrost CLI."""

from __future__ import annotations


class BifrostError(Exception):
    """Base exception for all domain-level Bifrost errors."""


class ConfigError(BifrostError):
    """Raised when configuration is missing or invalid."""


class ConfigPermissionError(ConfigError):
    """Raised when the configuration file has unsafe permissions."""


class ApiError(BifrostError):
    """Raised for non-auth API failures."""


class AuthenticationError(BifrostError):
    """Raised when RomM API authentication fails."""


class NetworkError(BifrostError):
    """Raised when the API cannot be reached."""
