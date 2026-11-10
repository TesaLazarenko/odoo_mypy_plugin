"""Plugin settings, read from the ``[tool.odoo_typing]`` table of ``pyproject.toml``.

Example::

    [tool.odoo_typing]
    odoo_path = "/usr/lib/python3/dist-packages/odoo"   # dir that holds release.py
    addons_path = ["addons", "../enterprise"]           # relative to pyproject.toml

This is a boundary module: raw TOML is validated and turned into :class:`Settings`.
"""

from __future__ import annotations

import importlib.util
import tomllib
from dataclasses import dataclass
from pathlib import Path

from odoo_typing.addons import MANIFEST_NAMES, find_manifest

__all__ = ["ConfigError", "Settings", "load_settings"]

SECTION = "odoo_typing"


class ConfigError(Exception):
    """Raised when the plugin configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated plugin settings.

    Attributes:
        odoo_path: Directory of the ``odoo`` package (contains ``release.py``).
        addons_paths: Extra addons directories, besides ``<odoo_path>/addons``.
    """

    odoo_path: Path
    addons_paths: tuple[Path, ...]


def load_settings(config_file: str | None) -> Settings:
    """Load settings from mypy's config file, falling back to auto-detection.

    Args:
        config_file: Path of the config file mypy was started with, if any.

    Returns:
        Validated settings.

    Raises:
        ConfigError: If the values have the wrong shape or Odoo cannot be found.
    """
    table: dict[str, object] = {}
    base_dir = Path.cwd()
    if config_file and config_file.endswith(".toml"):
        config_path = Path(config_file).resolve()
        base_dir = config_path.parent
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
        tool = raw.get("tool", {})
        section = tool.get(SECTION, {}) if isinstance(tool, dict) else {}
        if not isinstance(section, dict):
            raise ConfigError(f"[tool.{SECTION}] must be a table")
        table = section
    return Settings(
        odoo_path=_odoo_path(table.get("odoo_path"), base_dir),
        addons_paths=_addons_paths(table.get("addons_path", []), base_dir),
    )


def _odoo_path(value: object, base_dir: Path) -> Path:
    if value is None:
        spec = importlib.util.find_spec("odoo")
        if spec is None or not spec.submodule_search_locations:
            raise ConfigError(f"Odoo not found: set odoo_path in [tool.{SECTION}]")
        path = Path(next(iter(spec.submodule_search_locations)))
    elif isinstance(value, str):
        path = (base_dir / value).resolve()
    else:
        raise ConfigError("odoo_path must be a string")
    if not (path / "release.py").is_file():
        raise ConfigError(f"odoo_path {path} does not contain release.py")
    return path


def _addons_paths(value: object, base_dir: Path) -> tuple[Path, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("addons_path must be a list of strings")
    paths = tuple((base_dir / item).resolve() for item in value)
    missing = [str(path) for path in paths if not path.is_dir()]
    if missing:
        raise ConfigError(f"addons_path entries are not directories: {', '.join(missing)}")
    for path in paths:
        _check_addons_dir(path)
    return paths


def _check_addons_dir(path: Path) -> None:
    """Reject an ``addons_path`` entry that holds no addon.

    Odoo 18 itself accepts such entries and then just finds no module there. For a
    type checker that is a silent failure: the files of the user's addons would
    belong to no addon, and their models would be reported as unknown.
    """
    try:
        if any(find_manifest(child) for child in path.iterdir()):
            return
    except OSError as exc:
        raise ConfigError(f"cannot list addons_path entry {path}: {exc.strerror}") from exc
    if find_manifest(path):
        raise ConfigError(
            f"addons_path entry {path} is itself an addon; "
            f"list its parent directory {path.parent} instead"
        )
    raise ConfigError(
        f"addons_path entry {path} contains no addon "
        f"(no subdirectory with {' or '.join(MANIFEST_NAMES)}); "
        "list the directory that contains your addons"
    )
