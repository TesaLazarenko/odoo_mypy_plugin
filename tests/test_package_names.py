"""The names Odoo binds on its namespace package (``odoo.Command = Command``, Odoo 19+)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import RunMypy

from odoo_typing.package_names import bound_on_package


def test_names_assigned_to_a_package_are_read_with_their_imports(tmp_path: Path) -> None:
    (tmp_path / "init.py").write_text(
        "import pkg\n"
        "import other\n"
        "from .orm.commands import Command\n"
        "from .tools.translate import _, _lt as lazy\n"
        "from library import thing\n"
        "from . import release\n"
        "pkg.Command = Command\n"
        "pkg._ = _\n"
        "pkg.lazy_gettext = lazy\n"
        "pkg.thing = thing\n"
        "pkg.release = release\n"
        "pkg.local = 1\n"
        "other.Command = Command\n"
    )
    (tmp_path / "release.py").write_text("version = '1'\n")
    (tmp_path / "broken.py").write_text("import pkg\npkg.x = (\n")

    assert bound_on_package(tmp_path, "pkg") == {
        "Command": ("pkg.orm.commands", "Command"),
        "_": ("pkg.tools.translate", "_"),
        "lazy_gettext": ("pkg.tools.translate", "_lt"),
        "thing": ("library", "thing"),
        "release": ("pkg", "release"),
    }


MODELS = """
from odoo import SUPERUSER_ID, Command, _, api, fields, models

reveal_type(SUPERUSER_ID)

class Thing(models.Model):
    _name = "t.thing"
    _description = "Thing"

    name = fields.Char()
    tag_ids = fields.Many2many("t.thing", "t_thing_rel", "a", "b")

    def action(self) -> None:
        self.write({"tag_ids": [Command.link(1)], "name": _("Name")})
        self.write({"tag_ids": [Command.lnik(1)]})
"""


@pytest.mark.odoo(19, 20)
def test_names_bound_on_the_odoo_package_are_imported(run_mypy: RunMypy) -> None:
    addons = {"my_mod": {"models.py": MODELS}}

    for result in (run_mypy(addons, fresh_cache=True), run_mypy(addons, fresh_cache=True)):
        messages = result.messages("models.py")
        assert messages[0] == '4: note: Revealed type is "int"'
        assert len(messages) == 2, messages
        assert 'has no attribute "lnik"' in messages[1]
