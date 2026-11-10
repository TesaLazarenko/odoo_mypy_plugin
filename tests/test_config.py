"""Unit tests for reading ``[tool.odoo_typing]`` settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from odoo_typing.config import ConfigError, load_settings


def _config(tmp_path: Path, addons_path: list[str]) -> str:
    odoo = tmp_path / "odoo"
    (odoo / "addons").mkdir(parents=True)
    (odoo / "release.py").write_text("")
    config = tmp_path / "pyproject.toml"
    config.write_text(f'[tool.odoo_typing]\nodoo_path = "odoo"\naddons_path = {addons_path!r}\n')
    return str(config)


def _addon(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "__manifest__.py").write_text("{'name': 'x'}")


def test_load_settings_parent_of_addons_returns_resolved_paths(tmp_path: Path) -> None:
    _addon(tmp_path / "addons" / "my_mod")
    config = _config(tmp_path, ["addons"])

    settings = load_settings(config)

    assert settings.addons_paths == ((tmp_path / "addons").resolve(),)


def test_load_settings_entry_is_itself_an_addon_raises_with_parent_hint(tmp_path: Path) -> None:
    _addon(tmp_path / "addons" / "my_mod")
    config = _config(tmp_path, ["addons/my_mod"])

    with pytest.raises(ConfigError, match=r"my_mod is itself an addon.*list its parent"):
        load_settings(config)


def test_load_settings_entry_without_addons_raises(tmp_path: Path) -> None:
    (tmp_path / "empty" / "not_an_addon").mkdir(parents=True)
    config = _config(tmp_path, ["empty"])

    with pytest.raises(ConfigError, match=r"empty contains no addon"):
        load_settings(config)


def test_load_settings_missing_entry_raises(tmp_path: Path) -> None:
    config = _config(tmp_path, ["nowhere"])

    with pytest.raises(ConfigError, match="not directories"):
        load_settings(config)
