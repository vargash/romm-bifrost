"""Settings schema derived from the pydantic config models.

New options show up in the GUI without any GUI change: every field of
:class:`bifrost.config.AppConfig` is listed with its type and description.
Structured values (dicts and lists of models) are not editable here.
"""

from __future__ import annotations

import types
from dataclasses import dataclass
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from bifrost.config import AppConfig

Kind = Literal["bool", "choice", "number", "text", "path", "secret", "list"]

SECRET_KEYS = frozenset({"romm.client_token"})
_PATH_HINTS = ("_path", "_dir", "log_file")


@dataclass(frozen=True)
class Setting:
    """One editable config option."""

    key: str  # dotted path, e.g. "esde.roms_path"
    section: str
    label: str
    kind: Kind
    description: str
    choices: tuple[str, ...] = ()

    @property
    def leaf(self) -> str:
        return self.key.rsplit(".", 1)[-1]


def _unwrap_optional(annotation: Any) -> Any:
    if get_origin(annotation) in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _kind_of(key: str, annotation: Any) -> tuple[Kind, tuple[str, ...]] | None:
    annotation = _unwrap_optional(annotation)
    origin = get_origin(annotation)
    if origin is Literal:
        return "choice", tuple(str(c) for c in get_args(annotation))
    if origin is list:
        (item,) = get_args(annotation) or (str,)
        return ("list", ()) if item is str else None
    if origin is dict:
        return None
    if annotation is bool:
        return "bool", ()
    if annotation in (int, float):
        return "number", ()
    if annotation is str:
        if key in SECRET_KEYS:
            return "secret", ()
        if any(hint in key.rsplit(".", 1)[-1] for hint in _PATH_HINTS):
            return "path", ()
        return "text", ()
    return None


def _collect(model: type[BaseModel], prefix: str, section: str, out: list[Setting]) -> None:
    for name, field in model.model_fields.items():
        key = f"{prefix}.{name}"
        annotation = _unwrap_optional(field.annotation)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            _collect(annotation, key, section, out)
            continue
        found = _kind_of(key, annotation)
        if found is None:
            continue
        kind, choices = found
        out.append(
            Setting(
                key=key,
                section=section,
                label=key[len(section) + 1 :],
                kind=kind,
                description=field.description or "",
                choices=choices,
            )
        )


def build_settings() -> list[Setting]:
    """Every editable option of AppConfig, grouped by top-level section in field order."""
    settings: list[Setting] = []
    for section, field in AppConfig.model_fields.items():
        model = field.annotation
        if isinstance(model, type) and issubclass(model, BaseModel):
            _collect(model, section, section, settings)
    return settings


def get_text(config: AppConfig, setting: Setting) -> str:
    """Current value of setting as the text the user would type."""
    value: Any = config
    for part in setting.key.split("."):
        value = getattr(value, part)
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def display_text(config: AppConfig, setting: Setting) -> str:
    """Like get_text, but with secrets masked."""
    text = get_text(config, setting)
    if setting.kind == "secret" and text:
        return text[:4] + "…" if len(text) > 4 else "…"
    return text
