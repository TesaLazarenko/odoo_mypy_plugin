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
    config.write_text('[tool.odoo_typing]\nodoo_path = "odoo"\n')
    options = Options()
    options.config_file = str(config)
    read_depends = mocker.spy(addons_module, "read_depends")

    OdooPlugin(options)

    assert read_depends.call_count == 0


def _plugin_options(tmp_path: Path, release: str) -> Options:
    odoo = tmp_path / "odoo"
    (odoo / "addons" / "base").mkdir(parents=True)
    (odoo / "addons" / "base" / "__manifest__.py").write_text("{'depends': []}")
    (odoo / "release.py").write_text(release)
    config = tmp_path / "pyproject.toml"
    config.write_text('[tool.odoo_typing]\nodoo_path = "odoo"\n')
    options = Options()
    options.config_file = str(config)
    return options


def test_plugin_init_other_odoo_version_warns_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    OdooPlugin(_plugin_options(tmp_path, "version_info = (17, 0, 0, 'final', 0, '')\n"))

    assert "odoo-typing: warning: found Odoo 17.0" in capsys.readouterr().err


def test_plugin_init_odoo_18_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    OdooPlugin(_plugin_options(tmp_path, "version_info = (18, 0, 0, 'final', 0, '')\n"))

    assert capsys.readouterr().err == ""
