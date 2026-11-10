"""mypy with the plugin and a config that is broken, or not ``pyproject.toml``."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_addon
from mypy import api as mypy_api

USER = """
from odoo import models

class Thing(models.Model):
    _name = "x.thing"

    def action(self) -> None:
        reveal_type(self.env["x.thing"])
"""


def _mypy(config: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> tuple[str, int]:
    """Run mypy on ``addons/my_mod``; return everything it printed and its exit status."""
    target = str(tmp_path / "addons" / "my_mod")
    args = ["--config-file", str(config), "--cache-dir", str(tmp_path / "cache"), target]
    stdout, stderr, status = mypy_api.run(args)
    captured = capfd.readouterr()
    return "\n".join([stdout, stderr, captured.out, captured.err]), status


def test_mypy_bad_addons_path_exits_with_clean_message(
    tmp_path: Path, odoo_root: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    write_addon(tmp_path / "addons" / "my_mod", {"models.py": USER})
    config = tmp_path / "pyproject.toml"
    config.write_text(
        '[tool.mypy]\nplugins = ["odoo_typing.plugin"]\n'
        f'[tool.odoo_typing]\nodoo_path = "{odoo_root}/odoo"\naddons_path = ["missing"]\n'
    )

    output, status = _mypy(config, tmp_path, capfd)

    message = f"odoo-typing: error: addons_path entries are not directories: {tmp_path}/missing"
    assert (status, message in output, "INTERNAL ERROR" in output) == (2, True, False)


def test_mypy_ini_section_configures_plugin(
    tmp_path: Path, odoo_root: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    write_addon(tmp_path / "addons" / "my_mod", {"models.py": USER})
    config = tmp_path / "mypy.ini"
    config.write_text(
        f"[mypy]\nplugins = odoo_typing.plugin\nmypy_path = {odoo_root}:addons\n"
        "[mypy-odoo.*]\nfollow_imports = silent\n"
        "[odoo_typing]\naddons_path = addons\n"
    )

    output, status = _mypy(config, tmp_path, capfd)

    assert (status, 'Revealed type is "my_mod.models.Thing"' in output) == (0, True), output
