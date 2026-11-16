"""End-to-end: every way a field class reaches a model, across addons (overlay root).

One project with three addons and a library: a subclass re-exported through a third
addon, a subclass taking its comodel positionally, a subclass of a class of Odoo's own
modules, a factory function, ``functools.partial`` and a library's class; with them,
``env.cr`` and required fields.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_addon
from test_plugin_overlay import PYPROJECT, Project, _messages, _run

from odoo_typing.config import load_settings
from odoo_typing.overlay import build_overlay

CUSTOM = """
from functools import partial
from odoo import fields
from {float_module} import Float

class BigInteger(fields.Integer):
    column_type = ("int8", "int8")

class Partner(fields.Many2one):
    pass

class Rate(Float):
    pass

def money(**kwargs):
    return fields.Float(digits=(16, 2), **kwargs)

Label = partial(fields.Char, translate=True)
"""
CORE = {
    "__init__.py": "from . import custom, models\nfrom .custom import BigInteger\n",
    "models.py": """
        from odoo import fields, models

        class Chat(models.Model):
            _name = "fc.chat"
            _description = "Chat"

            name = fields.Char(required=True)
            note = fields.Char()
    """,
}
MIDDLE = {
    "depends": ["fc_core"],
    "__init__.py": (
        "from odoo.addons.fc_core import BigInteger\n"
        "from odoo.addons.fc_core.custom import Label, Partner, Rate, money\n"
    ),
}
USE = {
    "depends": ["fc_mid"],
    "models.py": """
        from odoo import models
        from odoo.addons.fc_mid import BigInteger, Label, Partner, Rate, money
        from fc_library.fields import Money

        class Chat(models.Model):
            _inherit = "fc.chat"

            chat_int_id = BigInteger()
            owner_id = Partner("res.partner")
            rate = Rate()
            price = money()
            label = Label()
            total = Money()

            def check(self) -> None:
                self.search([("chat_int_id", "=", 1), ("rate", ">", 0.5), ("price", "<", 9)])
                self.search([("label", "!=", False), ("total", ">", 1)])
                self.search([("owner_id.email", "=", "x")])
                self.write({"chat_int_id": 1, "rate": 0.5, "price": 2, "label": "x"})
                self.write({"owner_id": 1, "total": 2.5})
                self.write({"chat_int_id": "one"})
                self.write({"rate": "high"})
                self.write({"total": "much"})
                self.search([("owner_id.emial", "=", "x")])
                self.search([("chat_int_idd", "=", 1)])
                self.env.cr.execute("SELECT 1")
                rows = self.env.cr.fetchall()
                title: str = self.name
                other: str = self.note
    """,
}
LIBRARY = "from odoo import fields\n\n\nclass Money(fields.Float):\n    pass\n"


@pytest.fixture
def project(
    odoo_path: Path,
    odoo_root: Path,
    odoo_version: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Project:
    del odoo_root  # requested for its skip: no profile for this Odoo version yet
    # Odoo 19 defines its field classes in odoo.orm.fields_*; odoo.fields imports them.
    float_module = "odoo.orm.fields_numeric" if odoo_version >= 19 else "odoo.fields"
    core = {**CORE, "custom.py": CUSTOM.format(float_module=float_module)}
    write_addon(tmp_path / "addons" / "fc_core", core)
    write_addon(tmp_path / "addons" / "fc_mid", MIDDLE)
    write_addon(tmp_path / "addons" / "fc_use", USE)
    library = tmp_path / "site" / "fc_library"
    library.mkdir(parents=True)
    (library / "__init__.py").write_text("")
    (library / "py.typed").write_text("")
    (library / "fields.py").write_text(LIBRARY)
    monkeypatch.syspath_prepend(str(tmp_path / "site"))  # where the plugin finds it
    config = tmp_path / "pyproject.toml"
    text = PYPROJECT.format(
        root=tmp_path / ".odoo-root",
        odoo_path=odoo_path,
        odoo_version=odoo_version,
        package_bases=str(odoo_version >= 19).lower(),
    )
    search = f'mypy_path = ["{tmp_path / ".odoo-root"}"'
    config.write_text(text.replace(search, f'{search}, "{tmp_path / "site"}"'))
    build_overlay(load_settings(str(config), require_overlay=False), tmp_path / ".odoo-root")
    return Project(root=tmp_path, config=config)


def test_field_classes_from_everywhere_are_fields_of_their_odoo_class(
    project: Project, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    lines, err = _run(project, tmp_path / "cache", capfd, str(project.overlay_addons / "fc_use"))

    messages = _messages(lines, "models.py")
    found = [
        (message.partition(":")[0], message.rpartition("[")[2].rstrip("]")) for message in messages
    ]
    assert found == [
        ("22", "odoo-vals-type"),  # "one" for a BigInteger: checked as Integer
        ("23", "odoo-vals-type"),  # "high" for a subclass of Odoo's Float
        ("24", "odoo-vals-type"),  # "much" for the library's subclass of Float
        ("25", "odoo-domain"),  # a typo in a field of the comodel given positionally
        ("26", "odoo-domain"),  # a typo in the name of the re-exported class's field
        ("30", "assignment"),  # a field that is not required still reads False
    ], messages
    assert 'no field "emial"' in messages[3] and 'did you mean "email"' in messages[3]
    assert 'did you mean "chat_int_id"' in messages[4]
    assert err == ""
