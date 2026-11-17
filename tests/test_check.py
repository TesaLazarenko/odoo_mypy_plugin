"""Unit tests of ``odoo-typing-check`` (mypy itself is replaced by a function)."""

from __future__ import annotations

from pathlib import Path

import pytest

from odoo_typing.check import main, real_paths


def _addon(root: Path, name: str) -> Path:
    path = root / name
    (path / "models").mkdir(parents=True)
    (path / "__manifest__.py").write_text(repr({"name": name, "depends": []}))
    (path / "__init__.py").write_text("")
    (path / "models" / "sale.py").write_text("")
    (path / "models" / "stock.py").write_text("")
    return path


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project directory (the current one) with two addons and a fake Odoo next to it."""
    odoo = tmp_path / "dist" / "odoo"
    (odoo / "addons").mkdir(parents=True)
    for name in ("__init__.py", "release.py", "addons/__init__.py"):
        (odoo / name).write_text("")
    _addon(odoo / "addons", "base")
    root = tmp_path / "project"
    _addon(root / "addons", "my_mod")
    _addon(root / "addons", "other_mod")
    (root / "pyproject.toml").write_text(
        "[tool.odoo_typing]\n"
        f'odoo_path = "{odoo}"\n'
        "odoo_version = 18\n"
        'addons_path = ["addons"]\n'
        'overlay_root = ".odoo-root"\n'
    )
    monkeypatch.chdir(root)
    return root


class Mypy:
    """Stands for mypy: records its arguments, answers with a fixed output."""

    def __init__(self, stdout: str = "", status: int = 0) -> None:
        self.stdout = stdout
        self.status = status
        self.arguments: list[str] = []

    def __call__(self, arguments: list[str]) -> tuple[str, str, int]:
        self.arguments = arguments
        return self.stdout, "", self.status


OUTPUT = """\
.odoo-root/odoo/addons/my_mod/models/sale.py:3: error: Unknown Odoo model "x"  [odoo-model]
.odoo-root/odoo/addons/my_mod/models/sale.py:3: note: A note
.odoo-root/odoo/addons/my_mod/models/stock.py:7:5: error: Name "y" is not defined  [name-defined]
.odoo-root/odoo/addons/other_mod/models/sale.py:1: error: Something else  [misc]
Found 3 errors in 3 files (checked 9 source files)
"""


def test_every_addon_of_the_addons_path_is_checked_by_default(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mypy = Mypy(OUTPUT, status=1)

    status = main([], run=mypy)

    assert status == 1
    assert mypy.arguments == [
        "--config-file",
        "pyproject.toml",
        "-p",
        "odoo.addons.my_mod",
        "-p",
        "odoo.addons.other_mod",
    ]
    captured = capsys.readouterr()
    # The overlay root is generated on the way; the paths are the files themselves.
    assert (project / ".odoo-root" / "odoo" / "addons" / "my_mod").is_symlink()
    assert f"odoo-typing-check: wrote {project / '.odoo-root'} (3 addons)" in captured.err
    assert captured.out.splitlines() == [
        'addons/my_mod/models/sale.py:3: error: Unknown Odoo model "x"  [odoo-model]',
        "addons/my_mod/models/sale.py:3: note: A note",
        'addons/my_mod/models/stock.py:7:5: error: Name "y" is not defined  [name-defined]',
        "addons/other_mod/models/sale.py:1: error: Something else  [misc]",
        "Found 3 errors in 3 files (checked 9 source files)",
    ]


@pytest.mark.parametrize("target", ["my_mod", "addons/my_mod", "addons/my_mod/"])
def test_an_addon_by_name_or_directory(project: Path, target: str) -> None:
    mypy = Mypy()

    status = main([target, "--", "--strict"], run=mypy)

    assert status == 0
    assert mypy.arguments == [
        "--config-file",
        "pyproject.toml",
        "--strict",
        "-p",
        "odoo.addons.my_mod",
    ]


def test_files_get_their_own_errors_only(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mypy = Mypy(OUTPUT, status=1)

    status = main(["addons/my_mod/models/sale.py"], run=mypy)

    assert status == 1
    assert mypy.arguments[-2:] == ["-p", "odoo.addons.my_mod"]
    assert "--no-pretty" in mypy.arguments
    assert capsys.readouterr().out.splitlines() == [
        'addons/my_mod/models/sale.py:3: error: Unknown Odoo model "x"  [odoo-model]',
        "addons/my_mod/models/sale.py:3: note: A note",
        "odoo-typing-check: 1 errors in the given files (2 elsewhere)",
    ]


def test_a_clean_file_of_an_addon_with_errors_is_clean(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "addons" / "my_mod" / "models" / "clean.py").write_text("")

    status = main(["addons/my_mod/models/clean.py"], run=Mypy(OUTPUT, status=1))

    assert status == 0
    assert capsys.readouterr().out.splitlines() == [
        "odoo-typing-check: 0 errors in the given files (3 elsewhere)"
    ]


def test_a_directory_inside_an_addon_and_a_whole_addon(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mypy = Mypy(OUTPUT, status=1)

    status = main(["addons/my_mod/models", "other_mod"], run=mypy)

    assert status == 1
    assert mypy.arguments[-4:] == ["-p", "odoo.addons.my_mod", "-p", "odoo.addons.other_mod"]
    assert capsys.readouterr().out.splitlines()[-1] == (
        "odoo-typing-check: 3 errors in the given files (0 elsewhere)"
    )


def test_a_failure_of_mypy_is_passed_on(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    status = main(["addons/my_mod/models/sale.py"], run=Mypy("INTERNAL ERROR\n", status=2))

    assert status == 2
    assert capsys.readouterr().out == "INTERNAL ERROR\n"


def test_an_unknown_addon_returns_2(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mypy = Mypy()

    status = main(["no_such_addon"], run=mypy)

    assert status == 2
    assert mypy.arguments == []
    assert "odoo-typing-check: error: no_such_addon is neither" in capsys.readouterr().err


def test_files_outside_the_addons_are_skipped(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pre-commit hook passes every changed file."""
    mypy = Mypy()

    assert main(["pyproject.toml"], run=mypy) == 0
    assert mypy.arguments == []
    captured = capsys.readouterr()
    assert "pyproject.toml is in no addon: skipped" in captured.err
    assert captured.out == "odoo-typing-check: no addon to check\n"

    assert main(["pyproject.toml", "addons/my_mod/models/sale.py"], run=mypy) == 0
    assert mypy.arguments[-2:] == ["-p", "odoo.addons.my_mod"]


def test_addons_without_an_overlay_root_are_top_level_packages(project: Path) -> None:
    config = project / "pyproject.toml"
    config.write_text(config.read_text().replace('overlay_root = ".odoo-root"\n', ""))
    mypy = Mypy()

    main(["my_mod"], run=mypy)

    assert mypy.arguments == ["--config-file", "pyproject.toml", "-p", "my_mod"]
    assert not (project / ".odoo-root").exists()


def test_real_paths_keep_lines_without_an_existing_path(tmp_path: Path) -> None:
    lines = ["nowhere/x.py:1: error: Gone  [misc]", "Success: no issues found"]

    assert real_paths(lines, tmp_path) == [(lines[0], None), (lines[1], None)]
