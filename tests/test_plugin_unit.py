"""Unit tests of the plugin object itself (no mypy run)."""

from __future__ import annotations

from pathlib import Path

import pytest
from mypy.options import Options
from pytest_mock import MockerFixture

from odoo_typing import addons as addons_module
from odoo_typing.plugin import OdooPlugin


def test_plugin_init_fully_cached_run_reads_no_manifest(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    odoo = tmp_path / "odoo"
    (odoo / "addons" / "base").mkdir(parents=True)
    (odoo / "addons" / "base" / "__manifest__.py").write_text("{'depends': []}")
    (odoo / "release.py").write_text("")
    config = tmp_path / "pyproject.toml"
    config.write_text('[tool.odoo_typing]\nodoo_path = "odoo"\nodoo_version = 18\n')
    options = Options()
    options.config_file = str(config)
    read_depends = mocker.spy(addons_module, "read_depends")

    OdooPlugin(options)

    assert read_depends.call_count == 0


def _plugin_options(tmp_path: Path, release: str, version: str = "odoo_version = 18\n") -> Options:
    odoo = tmp_path / "odoo"
    (odoo / "addons" / "base").mkdir(parents=True)
    (odoo / "addons" / "base" / "__manifest__.py").write_text("{'depends': []}")
    (odoo / "release.py").write_text(release)
    config = tmp_path / "pyproject.toml"
    config.write_text('[tool.odoo_typing]\nodoo_path = "odoo"\n' + version)
    options = Options()
    options.config_file = str(config)
    return options


def test_plugin_init_release_differs_from_config_warns_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    OdooPlugin(_plugin_options(tmp_path, "version_info = (17, 0, 0, 'final', 0, '')\n"))

    err = capsys.readouterr().err
    assert "odoo-typing: warning:" in err
    assert "odoo_version = 18" in err
    assert "Odoo 17.0" in err


def test_plugin_init_release_matches_config_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    OdooPlugin(_plugin_options(tmp_path, "version_info = (18, 0, 0, 'final', 0, '')\n"))

    assert capsys.readouterr().err == ""


def test_plugin_init_without_odoo_version_exits_with_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        OdooPlugin(_plugin_options(tmp_path, "", version=""))

    assert exit_info.value.code == 2
    assert "odoo-typing: error: set odoo_version" in capsys.readouterr().err


def test_plugin_init_version_without_profile_exits_with_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        OdooPlugin(_plugin_options(tmp_path, "", version="odoo_version = 20\n"))

    assert exit_info.value.code == 2
    assert "Odoo 20 is not supported yet" in capsys.readouterr().err


def _overlay_options(tmp_path: Path, version: int, explicit_package_bases: bool) -> Options:
    options = _plugin_options(
        tmp_path, "", version=f'odoo_version = {version}\noverlay_root = ".odoo-root"\n'
    )
    (tmp_path / ".odoo-root" / "odoo" / "addons").mkdir(parents=True)
    (tmp_path / ".odoo-root" / ".odoo-typing-root").write_text("")
    options.explicit_package_bases = explicit_package_bases
    return options


def test_plugin_init_odoo_19_overlay_without_explicit_package_bases_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    OdooPlugin(_overlay_options(tmp_path, 19, explicit_package_bases=False))

    err = capsys.readouterr().err
    assert "odoo-typing: warning:" in err
    assert "explicit_package_bases = true" in err


@pytest.mark.parametrize(("version", "explicit"), [(19, True), (18, False)])
def test_plugin_init_overlay_package_bases_not_needed_or_set_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: int, explicit: bool
) -> None:
    OdooPlugin(_overlay_options(tmp_path, version, explicit_package_bases=explicit))

    assert capsys.readouterr().err == ""
