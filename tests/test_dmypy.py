"""The mypy daemon keeps one plugin instance: its registry must follow file edits.

One daemon session, scripted: each step edits addon files, rechecks with
``dmypy run`` and compares with what a cold mypy run reports for the same files.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import PYPROJECT, write_addon

THING = """
from odoo import fields, models

class Thing(models.Model):
    _name = "x.thing"

    name = fields.Char()
"""

EXTENSION = """

class ThingExtra(models.Model):
    _inherit = "x.thing"

    extra = fields.Integer()
"""

USER = """
from odoo import models

class User(models.Model):
    _name = "x.user"

    def action(self) -> None:
        reveal_type(self.env["x.thing"])
"""

GADGET = """

class Gadget(models.Model):
    _name = "x.gadget"

    def action(self) -> None:
        reveal_type(self.env["x.gadget"])
        reveal_type(self.env["x.thing"].extra)
"""


class Daemon:
    """A ``dmypy`` session on one config; ``check()`` returns the reported lines."""

    def __init__(self, tmp_path: Path, config: Path, targets: list[Path]) -> None:
        self._status = tmp_path / "dmypy.json"
        options = ["--config-file", str(config), "--cache-dir", str(tmp_path / "cache")]
        self._run_args = ["run", "--timeout", "300", "--", *options, *map(str, targets)]

    def _dmypy(self, *args: str) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, "-m", "mypy.dmypy", "--status-file", str(self._status)]
        return subprocess.run([*command, *args], capture_output=True, text=True, timeout=300)

    def check(self) -> list[str]:
        result = self._dmypy(*self._run_args)
        assert "INTERNAL ERROR" not in result.stdout + result.stderr, result.stdout
        assert result.returncode in (0, 1), result.stdout + result.stderr
        return [
            line.split("my_mod/", 1)[1] for line in result.stdout.splitlines() if "my_mod/" in line
        ]

    def stop(self) -> None:
        self._dmypy("stop")


@pytest.fixture
def daemon(tmp_path: Path, odoo_root: Path, odoo_version: int) -> Iterator[Daemon]:
    root = tmp_path / "addons"
    write_addon(root / "dep_mod", {"models.py": THING})
    write_addon(root / "my_mod", {"depends": ["dep_mod"], "user.py": USER})
    config = tmp_path / "pyproject.toml"
    config.write_text(
        PYPROJECT.format(
            odoo_root=odoo_root,
            odoo_version=odoo_version,
            mypy_path=json.dumps([str(odoo_root), str(root)]),
            addons_path=json.dumps([str(root)]),
        )
    )
    session = Daemon(tmp_path, config, [root / "dep_mod", root / "my_mod"])
    try:
        yield session
    finally:
        session.stop()


def _append(path: Path, source: str) -> None:
    with path.open("a") as handle:
        handle.write(textwrap.dedent(source))


def test_dmypy_new_model_extension_and_moved_module_follow_edits(
    daemon: Daemon, tmp_path: Path
) -> None:
    root = tmp_path / "addons"
    first = daemon.check()
    _append(root / "my_mod/user.py", GADGET)
    second = daemon.check()
    _append(root / "dep_mod/models.py", EXTENSION)
    third = daemon.check()
    (root / "dep_mod/models.py").write_text(textwrap.dedent(THING))
    (root / "dep_mod/extra.py").write_text("from odoo import fields, models\n" + EXTENSION)
    (root / "dep_mod/__init__.py").write_text("from . import models, extra\n")
    fourth = daemon.check()

    assert fourth == [line.replace("models.ThingExtra", "extra.ThingExtra") for line in third]
    assert (first, second, third) == (
        ['user.py:8: note: Revealed type is "dep_mod.models.Thing"'],
        [
            'user.py:8: note: Revealed type is "dep_mod.models.Thing"',
            'user.py:15: note: Revealed type is "my_mod.user.Gadget"',
            'user.py:16: error: "Thing" has no attribute "extra"  [attr-defined]',
            'user.py:16: note: Revealed type is "Any"',
        ],
        [
            'user.py:8: note: Revealed type is "dep_mod.models.ThingExtra"',
            'user.py:15: note: Revealed type is "my_mod.user.Gadget"',
            'user.py:16: note: Revealed type is "int"',
        ],
    )
