"""Unit tests for reading ``[tool.odoo_typing]`` settings."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from odoo_typing.config import ConfigError, Settings, load_settings


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


def _odoo(tmp_path: Path) -> Path:
    odoo = tmp_path / "odoo"
    (odoo / "addons").mkdir(parents=True)
    (odoo / "release.py").write_text("")
    return odoo


def _ini(tmp_path: Path, name: str, body: str) -> str:
    config = tmp_path / name
    config.write_text(textwrap.dedent(body))
    return str(config)


def test_load_settings_relative_toml_paths_resolve_against_config_dir(tmp_path: Path) -> None:
    _addon(tmp_path / "addons" / "my_mod")
    config = _config(tmp_path, ["addons"])

    settings = load_settings(config)

    assert settings.odoo_path == (tmp_path / "odoo").resolve()


@pytest.mark.parametrize("name", ["mypy.ini", ".mypy.ini", "setup.cfg"])
def test_load_settings_ini_section_returns_settings(tmp_path: Path, name: str) -> None:
    _odoo(tmp_path)
    _addon(tmp_path / "addons" / "my_mod")
    _addon(tmp_path / "more" / "other")
    body = """
    [mypy]
    plugins = odoo_typing.plugin

    [odoo_typing]
    odoo_path = odoo
    addons_path = addons,
        more
    """

    settings = load_settings(_ini(tmp_path, name, body))

    assert settings == Settings(
        odoo_path=(tmp_path / "odoo").resolve(),
        addons_paths=((tmp_path / "addons").resolve(), (tmp_path / "more").resolve()),
    )


def test_load_settings_ini_without_section_reads_pyproject_beside(tmp_path: Path) -> None:
    _addon(tmp_path / "addons" / "my_mod")
    _config(tmp_path, ["addons"])
    config = _ini(tmp_path, "mypy.ini", "[mypy]\nplugins = odoo_typing.plugin\n")

    settings = load_settings(config)

    assert settings.addons_paths == ((tmp_path / "addons").resolve(),)


def test_load_settings_odoo_on_mypy_path_is_found(tmp_path: Path) -> None:
    odoo = _odoo(tmp_path / "root")
    config = _ini(tmp_path, "mypy.ini", "[mypy]\n")

    settings = load_settings(config, [str(tmp_path / "elsewhere"), str(tmp_path / "root")])

    assert settings.odoo_path == odoo.resolve()


def test_load_settings_odoo_in_overlay_on_mypy_path_returns_real_package(
    tmp_path: Path,
) -> None:
    odoo = _odoo(tmp_path / "real")
    overlay = tmp_path / "root" / "odoo"
    (overlay / "addons").mkdir(parents=True)
    (overlay / "release.py").symlink_to(odoo / "release.py")
    config = _ini(tmp_path, "mypy.ini", "[mypy]\n")

    settings = load_settings(config, [str(tmp_path / "root")])

    assert settings.odoo_path == odoo.resolve()


def test_load_settings_odoo_not_found_names_both_config_formats(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch("importlib.util.find_spec", return_value=None)
    config = _ini(tmp_path, "mypy.ini", "[mypy]\n")

    with pytest.raises(ConfigError) as error:
        load_settings(config)

    assert "[tool.odoo_typing] (pyproject.toml) or [odoo_typing] (mypy.ini" in str(error.value)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[tool]\nodoo_typing = 1\n", r"\[tool.odoo_typing\] in .* must be a table"),
        ("[tool.odoo_typing]\nodoo_path = 1\n", "odoo_path must be a string"),
        ('[tool.odoo_typing]\nodoo_path = "nowhere"\n', "does not contain release.py"),
        (
            '[tool.odoo_typing]\nodoo_path = "odoo"\naddons_path = "addons"\n',
            "addons_path must be a list of strings",
        ),
        ("[tool.odoo_typing\n", "cannot read"),
    ],
)
def test_load_settings_invalid_toml_raises(tmp_path: Path, body: str, message: str) -> None:
    _odoo(tmp_path)
    config = tmp_path / "pyproject.toml"
    config.write_text(body)

    with pytest.raises(ConfigError, match=message):
        load_settings(str(config))


def test_load_settings_broken_ini_raises(tmp_path: Path) -> None:
    config = _ini(tmp_path, "setup.cfg", "no section header\n")

    with pytest.raises(ConfigError, match="cannot read"):
        load_settings(config)


def _overlay_config(tmp_path: Path, overlay_root: object) -> str:
    _addon(tmp_path / "addons" / "my_mod")
    config = _config(tmp_path, ["addons"])
    with open(config, "a") as handle:
        handle.write(f"overlay_root = {overlay_root!r}\n".replace("'", '"'))
    return config


def test_load_settings_without_overlay_root_returns_none(tmp_path: Path) -> None:
    _addon(tmp_path / "addons" / "my_mod")
    config = _config(tmp_path, ["addons"])

    assert load_settings(config).overlay_root is None


def test_load_settings_generated_overlay_root_returns_resolved_path(tmp_path: Path) -> None:
    config = _overlay_config(tmp_path, ".odoo-root")
    (tmp_path / ".odoo-root" / "odoo" / "addons").mkdir(parents=True)
    (tmp_path / ".odoo-root" / "odoo" / "addons" / "__init__.py").write_text("")

    settings = load_settings(config)

    assert settings.overlay_root == (tmp_path / ".odoo-root").resolve()


def test_load_settings_overlay_root_not_generated_raises_with_command_hint(
    tmp_path: Path,
) -> None:
    config = _overlay_config(tmp_path, ".odoo-root")

    with pytest.raises(ConfigError, match=r"overlay_root .*odoo-typing-root"):
        load_settings(config)


def test_load_settings_overlay_root_not_generated_unchecked_returns_path(tmp_path: Path) -> None:
    config = _overlay_config(tmp_path, ".odoo-root")

    settings = load_settings(config, require_overlay=False)

    assert settings.overlay_root == (tmp_path / ".odoo-root").resolve()


def test_load_settings_overlay_root_not_a_string_raises(tmp_path: Path) -> None:
    config = _overlay_config(tmp_path, 3)

    with pytest.raises(ConfigError, match="overlay_root must be a string"):
        load_settings(config)
