"""Unit tests for reading ``[tool.odoo_typing]`` settings."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from odoo_typing.config import ConfigError, Settings, default_scan_cache_dir, load_settings


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
        scan_cache_dir=default_scan_cache_dir(),
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
    (tmp_path / ".odoo-root" / ".odoo-typing-root").write_text("")

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


# -- options (roadmap: cross-cutting rule 6, item 4.10) ------------------------------------------


def _options_config(tmp_path: Path, options: str, name: str = "pyproject.toml") -> str:
    odoo = tmp_path / "odoo"
    (odoo / "addons").mkdir(parents=True)
    (odoo / "release.py").write_text("")
    config = tmp_path / name
    toml = name.endswith(".toml")
    header = (
        '[tool.odoo_typing]\nodoo_path = "odoo"\n' if toml else "[odoo_typing]\nodoo_path = odoo\n"
    )
    config.write_text(header + options)
    return str(config)


def test_load_settings_dynamic_env_default_is_any(tmp_path: Path) -> None:
    settings = load_settings(_options_config(tmp_path, ""))

    assert settings.dynamic_env == "any"


def test_load_settings_dynamic_env_base_is_kept(tmp_path: Path) -> None:
    settings = load_settings(_options_config(tmp_path, 'dynamic_env = "base"\n'))

    assert settings.dynamic_env == "base"


def test_load_settings_dynamic_env_from_ini(tmp_path: Path) -> None:
    settings = load_settings(_options_config(tmp_path, "dynamic_env = base\n", "mypy.ini"))

    assert settings.dynamic_env == "base"


def test_load_settings_dynamic_env_invalid_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r'dynamic_env must be one of "any", "base"'):
        load_settings(_options_config(tmp_path, 'dynamic_env = "strict"\n'))


def test_load_settings_unknown_option_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r'unknown option "addon_path".*addons_path'):
        load_settings(_options_config(tmp_path, 'addon_path = ["x"]\n'))


def test_settings_option_data_changes_with_dynamic_env(tmp_path: Path) -> None:
    default = load_settings(_options_config(tmp_path / "a", ""))
    base = load_settings(_options_config(tmp_path / "b", 'dynamic_env = "base"\n'))

    assert default.option_data() != base.option_data()


# -- literal_selection (roadmap 4.2) ---------------------------------------------------------


def test_load_settings_literal_selection_default_is_off(tmp_path: Path) -> None:
    assert load_settings(_options_config(tmp_path, "")).literal_selection is False


@pytest.mark.parametrize(
    ("options", "name"),
    [("literal_selection = true\n", "pyproject.toml"), ("literal_selection = yes\n", "mypy.ini")],
)
def test_load_settings_literal_selection_on(tmp_path: Path, options: str, name: str) -> None:
    assert load_settings(_options_config(tmp_path, options, name)).literal_selection is True


def test_load_settings_literal_selection_invalid_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="literal_selection must be true or false"):
        load_settings(_options_config(tmp_path, 'literal_selection = "sometimes"\n'))


def test_settings_option_data_changes_with_literal_selection(tmp_path: Path) -> None:
    default = load_settings(_options_config(tmp_path / "a", ""))
    literal = load_settings(_options_config(tmp_path / "b", "literal_selection = true\n"))

    assert default.option_data() != literal.option_data()


# -- odoo_version: the Odoo version the checks target (single source of truth) ----------------


@pytest.mark.parametrize(("value", "expected"), [("18", 18), ("17", 17), ('"20"', 20)])
def test_load_settings_odoo_version_is_an_int(tmp_path: Path, value: str, expected: int) -> None:
    settings = load_settings(_options_config(tmp_path, f"odoo_version = {value}\n"))

    assert settings.odoo_version == expected


def test_load_settings_odoo_version_from_ini(tmp_path: Path) -> None:
    settings = load_settings(_options_config(tmp_path, "odoo_version = 19\n", "mypy.ini"))

    assert settings.odoo_version == 19


def test_load_settings_odoo_version_missing_is_none(tmp_path: Path) -> None:
    assert load_settings(_options_config(tmp_path, "")).odoo_version is None


@pytest.mark.parametrize("value", ["16", "21", '"18.0"', '"x"', "true"])
def test_load_settings_odoo_version_invalid_raises_config_error(tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigError, match=r"odoo_version must be one of 17, 18, 19, 20"):
        load_settings(_options_config(tmp_path, f"odoo_version = {value}\n"))


def test_settings_option_data_changes_with_odoo_version(tmp_path: Path) -> None:
    v17 = load_settings(_options_config(tmp_path / "a", "odoo_version = 17\n"))
    v18 = load_settings(_options_config(tmp_path / "b", "odoo_version = 18\n"))

    assert v17.option_data() != v18.option_data()


def test_load_settings_vals_ignore_keys_default_is_manual_fields(tmp_path: Path) -> None:
    settings = load_settings(_options_config(tmp_path, ""))

    assert settings.vals_ignore_keys == ("x_*",)


def test_load_settings_vals_ignore_keys_from_ini_list(tmp_path: Path) -> None:
    options = "vals_ignore_keys = x_*,\n    sel_groups_*\n"
    settings = load_settings(_options_config(tmp_path, options, "mypy.ini"))

    assert settings.vals_ignore_keys == ("x_*", "sel_groups_*")


def test_load_settings_vals_ignore_keys_not_a_list_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="vals_ignore_keys must be a list of strings"):
        load_settings(_options_config(tmp_path, 'vals_ignore_keys = "x_*"\n'))


def test_settings_option_data_changes_with_vals_ignore_keys(tmp_path: Path) -> None:
    default = load_settings(_options_config(tmp_path / "a", ""))
    empty = load_settings(_options_config(tmp_path / "b", "vals_ignore_keys = []\n"))

    assert default.option_data() != empty.option_data()


def test_scan_cache_dir_defaults_to_the_user_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    settings = load_settings(_config(tmp_path, []))

    assert settings.scan_cache_dir == tmp_path / "xdg" / "odoo-typing" / "scans"


def test_scan_cache_dir_relative_to_config_or_empty_for_none(tmp_path: Path) -> None:
    config = Path(_config(tmp_path, []))
    text = config.read_text()

    config.write_text(text + 'scan_cache_dir = ".scans"\n')
    relative = load_settings(str(config)).scan_cache_dir
    config.write_text(text + 'scan_cache_dir = ""\n')
    disabled = load_settings(str(config)).scan_cache_dir

    assert (relative, disabled) == (tmp_path / ".scans", None)


def test_scan_cache_dir_not_a_string_is_an_error(tmp_path: Path) -> None:
    config = Path(_config(tmp_path, []))
    config.write_text(config.read_text() + "scan_cache_dir = 1\n")

    with pytest.raises(ConfigError, match="scan_cache_dir must be a string"):
        load_settings(str(config))
