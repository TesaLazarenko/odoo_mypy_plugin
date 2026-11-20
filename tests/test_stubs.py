"""Tests of ``odoo-typing-stubs`` (roadmap 5.1): stubs for editors."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from conftest import OdooNames, run_mypy_in_process, write_addon

from odoo_typing.config import Settings
from odoo_typing.scanner import FieldInfo
from odoo_typing.stubs import (
    _environment_stub,
    _vals_value,
    _value,
    main,
    model_class_name,
    write_stubs,
)

ENV_STUB = """\
import typing

IdType: typing.TypeAlias

class Environment:
    def __getitem__(self, model_name: str) -> BaseModel: ...
    def ref(self, xml_id): ...
"""

ADDON = {
    "models.py": """
    from odoo import fields, models

    class Thing(models.Model):
        _name = "t.thing"

        name = fields.Char()
        float = fields.Float()
        partner_id = fields.Many2one("res.partner")
        line_ids = fields.One2many("t.line", "thing_id")

    class Line(models.Model):
        _name = "t.line"

        thing_id = fields.Many2one("t.thing")
    """,
}

CHECK = """
from odoo.api import Environment

def f(env: Environment) -> None:
    thing = env["t.thing"]
    reveal_type(thing)
    reveal_type(thing.name)
    reveal_type(thing.float)
    reveal_type(thing.line_ids.thing_id.partner_id)
    reveal_type(env["no.such"])
"""


def test_model_class_name_is_an_identifier() -> None:
    assert model_class_name("res.partner") == "res_partner"
    assert model_class_name("global") == "global_"


def test_environment_stub_adds_overloads_and_fixes_aliases() -> None:
    stub = _environment_stub(ENV_STUB, ["res.partner"])

    assert "def __getitem__(self, model_name: Literal['res.partner']) -> " in stub
    assert "odoo_typing_models.res_partner: ..." in stub
    assert "def __getitem__(self, model_name: str) -> odoo.models.BaseModel: ..." in stub
    assert "IdType: typing.TypeAlias = typing.Any" in stub
    assert "def ref(self, xml_id): ..." in stub


def test_stubs_give_an_editor_the_model_types(
    tmp_path: Path, odoo_path: Path, odoo_version: int, names: OdooNames
) -> None:
    write_addon(tmp_path / "addons" / "t_mod", ADDON)
    settings = Settings(
        odoo_path=odoo_path, addons_paths=(tmp_path / "addons",), odoo_version=odoo_version
    )
    stubs = tmp_path / "typings"
    assert write_stubs(settings, "t_mod", stubs) > 2
    # What an editor sees: the odoo package with the stub in place of the module.
    editor = tmp_path / "editor"
    environment = next(stubs.joinpath("odoo").rglob("*.pyi"))
    relative = environment.relative_to(stubs / "odoo")
    _link_package(odoo_path, editor / "odoo", relative)
    (editor / "odoo" / relative).write_text(environment.read_text())
    (editor / "odoo_typing_models.pyi").write_text((stubs / "odoo_typing_models.pyi").read_text())
    (editor / "t_mod").symlink_to(tmp_path / "addons" / "t_mod")
    (tmp_path / "check.py").write_text(textwrap.dedent(CHECK))
    config = tmp_path / "mypy.ini"
    config.write_text(f"[mypy]\nmypy_path = {editor}\n[mypy-odoo.*]\nfollow_imports = silent\n")

    stdout, _, _ = run_mypy_in_process(
        [
            "--config-file",
            str(config),
            "--cache-dir",
            str(tmp_path / "cache"),
            str(tmp_path / "check.py"),
        ]
    )

    revealed = [line.split("note: ", 1)[1] for line in stdout.splitlines() if "Revealed" in line]
    assert revealed == [
        'Revealed type is "odoo_typing_models.t_thing"',
        'Revealed type is "str | Literal[False]"',
        'Revealed type is "float"',
        'Revealed type is "odoo_typing_models.res_partner"',
        f'Revealed type is "{names.base_model}"',
    ]
    assert not [line for line in stdout.splitlines() if "check.py" in line and "error" in line]


def _link_package(source: Path, target: Path, replaced: Path) -> None:
    """A copy of a package made of symlinks, with the directories leading to ``replaced``."""
    target.mkdir(parents=True)
    head = replaced.parts[0]
    for entry in source.iterdir():
        if entry.name == head and len(replaced.parts) > 1:
            _link_package(entry, target / head, Path(*replaced.parts[1:]))
        elif entry.name != replaced.with_suffix(".py").name:
            (target / entry.name).symlink_to(entry)


def test_main_unknown_addon_returns_2(
    tmp_path: Path, odoo_path: Path, odoo_version: int, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "pyproject.toml"
    config.write_text(
        f'[tool.odoo_typing]\nodoo_path = "{odoo_path}"\nodoo_version = {odoo_version}\n'
    )

    status = main(["--config", str(config), "--addon", "no_such_addon"])

    assert status == 2
    assert "unknown addon" in capsys.readouterr().err


def test_main_check_reports_stale_stubs(
    tmp_path: Path, odoo_path: Path, odoo_version: int, capsys: pytest.CaptureFixture[str]
) -> None:
    write_addon(tmp_path / "addons" / "t_mod", ADDON)
    config = tmp_path / "pyproject.toml"
    config.write_text(
        f'[tool.odoo_typing]\nodoo_path = "{odoo_path}"\nodoo_version = {odoo_version}\n'
        f'addons_path = ["{tmp_path / "addons"}"]\n'
    )
    arguments = ["--config", str(config), "--addon", "t_mod", "--output", str(tmp_path / "out")]
    assert main([*arguments, "--check"]) == 1
    assert main(arguments) == 0
    assert main([*arguments, "--check"]) == 0
    (tmp_path / "addons/t_mod/models.py").write_text(
        (tmp_path / "addons/t_mod/models.py").read_text() + "    extra = fields.Integer()\n"
    )

    assert main([*arguments, "--check"]) == 1
    assert "odoo_typing_models.pyi is out of date" in capsys.readouterr().out


def test_reference_reads_none_and_takes_false() -> None:
    """Empty, a ``Reference`` reads ``None``; ``create`` takes ``"model,id"`` and ``False`` too."""
    info = FieldInfo("ref", "Reference")

    assert _value(info, {}) == "odoo.models.BaseModel | None"
    assert _vals_value(info, {}) == (
        "builtins.str | odoo.models.BaseModel | typing.Literal[False] | None"
    )
