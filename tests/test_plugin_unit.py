"""Unit tests of the plugin object itself (no mypy run)."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest
from mypy.build import take_module_snapshot
from mypy.errors import CompileError
from mypy.nodes import MypyFile
from mypy.options import Options
from mypy.plugin import ReportConfigContext
from pytest_mock import MockerFixture

from odoo_typing import addons as addons_module
from odoo_typing import plugin as plugin_module
from odoo_typing.fingerprint import Fingerprints
from odoo_typing.plugin import OdooPlugin, plugin
from odoo_typing.records import source_digest
from odoo_typing.registry import Registry
from odoo_typing.startup import TESTED_MYPY


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


def test_plugin_init_odoo_without_classes_of_profile_warns_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    options = _plugin_options(tmp_path, "")  # no version to read: a patched checkout
    (tmp_path / "odoo" / "orm").mkdir()
    (tmp_path / "odoo" / "orm" / "models.py").write_text("class BaseModel:\n    pass\n")

    OdooPlugin(options)

    err = capsys.readouterr().err
    assert "odoo-typing: warning: the Odoo at" in err
    assert "does not define odoo.models.BaseModel" in err


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
        OdooPlugin(_plugin_options(tmp_path, "", version="odoo_version = 16\n"))

    assert exit_info.value.code == 2
    assert "16" in capsys.readouterr().err


def test_plugin_init_odoo_20_on_python_3_11_exits_with_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mocker: MockerFixture
) -> None:
    mocker.patch.object(sys, "version_info", (3, 11, 9, "final", 0))

    with pytest.raises(SystemExit) as exit_info:
        OdooPlugin(_plugin_options(tmp_path, "", version="odoo_version = 20\n"))

    assert exit_info.value.code == 2
    assert "Odoo 20 needs Python 3.12 or newer to parse" in capsys.readouterr().err


def test_plugin_init_odoo_20_targeting_python_3_11_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mocker: MockerFixture
) -> None:
    mocker.patch.object(sys, "version_info", (3, 12, 0, "final", 0))  # mypy runs on 3.12
    options = _plugin_options(tmp_path, "", version="odoo_version = 20\n")
    options.python_version = (3, 11)

    OdooPlugin(options)

    err = capsys.readouterr().err
    assert "odoo-typing: warning:" in err
    assert 'python_version = "3.12"' in err


@pytest.mark.parametrize(("version", "python"), [(20, (3, 12)), (18, (3, 10))])
def test_plugin_init_python_version_new_enough_prints_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mocker: MockerFixture,
    version: int,
    python: tuple[int, int],
) -> None:
    mocker.patch.object(sys, "version_info", (*python, 0, "final", 0))  # mypy runs on it
    options = _plugin_options(tmp_path, "", version=f"odoo_version = {version}\n")
    options.python_version = python

    OdooPlugin(options)

    assert capsys.readouterr().err == ""


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


@pytest.mark.parametrize("version", ["2.5.0", "2.0.1", "3.0.0", "2.5.0+dev.1a2b3c"])
def test_plugin_untested_mypy_version_warns_and_returns_plugin(
    capsys: pytest.CaptureFixture[str], version: str
) -> None:
    found = plugin(version)

    assert found is OdooPlugin
    err = capsys.readouterr().err
    assert err.startswith(f"odoo-typing: warning: mypy {version} is not a version")
    assert "(>=2.1,<2.5)" in err


@pytest.mark.parametrize("version", ["2.1.0", "2.4.1", "2.4.0+dev.1a2b3c", "unknown"])
def test_plugin_tested_or_unreadable_mypy_version_prints_nothing(
    capsys: pytest.CaptureFixture[str], version: str
) -> None:
    found = plugin(version)

    assert found is OdooPlugin
    assert capsys.readouterr().err == ""


def test_tested_mypy_matches_pyproject_requirement() -> None:
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    (requirement,) = [
        item for item in pyproject["project"]["dependencies"] if item.startswith("mypy")
    ]

    low, high = TESTED_MYPY

    assert requirement == f"mypy>={low[0]}.{low[1]},<{high[0]}.{high[1]}"


def test_take_module_snapshot_plugin_module_carries_package_digest() -> None:
    # mypy keeps its cache while this snapshot is unchanged: any file of the package counts.
    snapshot = take_module_snapshot(plugin_module)

    assert snapshot.split(":")[0] == source_digest()


# -- exceptions that escape the entry points mypy does not wrap ---------------------------


def _module_file(tmp_path: Path) -> MypyFile:
    tree = MypyFile([], [])
    tree._fullname = "my_mod.models"
    tree.path = str(tmp_path / "addons" / "my_mod" / "models.py")
    return tree


def test_get_additional_deps_registry_raises_reports_it_and_raises_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mocker: MockerFixture
) -> None:
    found = OdooPlugin(_plugin_options(tmp_path, ""))
    mocker.patch.object(Registry, "dependency_modules", side_effect=ValueError("boom"))

    with pytest.raises(ValueError, match="boom"):
        found.get_additional_deps(_module_file(tmp_path))

    assert capsys.readouterr().err == (
        "odoo-typing: internal error while finding the models my_mod.models relies on: "
        "ValueError: boom (please report it to odoo-typing)\n"
    )


def test_get_additional_deps_registry_refresh_raises_reports_it_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mocker: MockerFixture
) -> None:
    found = OdooPlugin(_plugin_options(tmp_path, ""))
    mocker.patch.object(Registry, "inputs_changed", side_effect=OSError("gone"))

    with pytest.raises(OSError, match="gone"):
        found.get_additional_deps(_module_file(tmp_path))

    assert capsys.readouterr().err.splitlines() == [
        "odoo-typing: internal error while checking the addons for changes: OSError: gone "
        "(please report it to odoo-typing)"
    ]


def test_report_config_data_fingerprint_raises_reports_it_and_raises_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mocker: MockerFixture
) -> None:
    found = OdooPlugin(_plugin_options(tmp_path, ""))
    mocker.patch.object(Fingerprints, "module_data", side_effect=KeyError("x.thing"))
    ctx = ReportConfigContext(id="my_mod.models", path="addons/my_mod/models.py", is_check=True)

    with pytest.raises(KeyError):
        found.report_config_data(ctx)

    assert capsys.readouterr().err == (
        "odoo-typing: internal error while computing the cache data of my_mod.models: "
        "KeyError: 'x.thing' (please report it to odoo-typing)\n"
    )


def test_get_additional_deps_mypy_compile_error_is_not_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mocker: MockerFixture
) -> None:
    found = OdooPlugin(_plugin_options(tmp_path, ""))
    mocker.patch.object(Registry, "dependency_modules", side_effect=CompileError(["error"]))

    with pytest.raises(CompileError):
        found.get_additional_deps(_module_file(tmp_path))

    assert capsys.readouterr().err == ""
