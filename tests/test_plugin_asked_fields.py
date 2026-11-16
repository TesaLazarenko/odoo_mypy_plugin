"""``"f" in records``: the field an addon asks for before it reads it."""

from __future__ import annotations

from conftest import RunMypy

T_BASE = {
    "models.py": """
    from odoo import fields, models

    class Thing(models.Model):
        _name = "t.thing"

        name = fields.Char()
        owner_id = fields.Many2one("t.thing")
    """,
}
T_EXTRA = {
    "depends": ["t_base"],
    "models.py": """
    from odoo import fields, models

    class Thing(models.Model):
        _inherit = "t.thing"

        extra = fields.Char()
    """,
}

USE = """
from odoo import models

class Thing(models.Model):
    _inherit = "t.thing"

    def asked(self) -> None:
        thing = self.env["t.thing"].search([], limit=1)
        thing.extra
        if "extra" in thing:
            reveal_type(thing.extra)
            thing.other
        owner = thing.owner_id
        if "extra" not in owner:
            return
        owner.extra = "x"
        reveal_type(owner.name)

    def asked_of_fields(self) -> None:
        self.extra
        if "extra" in self._fields:
            self.extra
        things = self.search([])
        if "extra" in things._fields and things.extra:
            things.extra

    def asked_of_the_model(self) -> None:
        self.env["t.thing"].extra
        if "extra" in self.env["t.thing"]:
            self.env["t.thing"].browse(1).extra

    def not_asked(self) -> None:
        self.env["t.thing"].extra
        values = {"name": "x"}
        if "missing" in values:
            self.missing
"""


def test_a_field_asked_for_is_read_without_an_error(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {"t_base": T_BASE, "t_extra": T_EXTRA, "t_use": {"depends": ["t_base"], "models.py": USE}},
        check=["t_use"],
    )

    assert result.messages("t_use/models.py") == [
        '9: error: "Thing" has no attribute "extra"  [attr-defined]',
        '11: note: Revealed type is "Any"',
        '12: error: "Thing" has no attribute "other"  [attr-defined]',
        '17: note: Revealed type is "str | Literal[False]"',
        '20: error: "Thing" has no attribute "extra"  [attr-defined]',
        '28: error: "Thing" has no attribute "extra"  [attr-defined]',
        '33: error: "Thing" has no attribute "extra"  [attr-defined]',
        '36: error: "Thing" has no attribute "missing"  [attr-defined]',
    ]


TESTS = """
from odoo.tests.common import TransactionCase

class TestThing(TransactionCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.thing = cls.env["t.thing"].create({"name": "x"})

    def test_asked_of_the_model(self) -> None:
        self.thing.extra
        if "extra" in self.env["t.thing"]:
            self.thing.extra = "x"

    def test_asked_of_the_records(self) -> None:
        thing = self.thing
        if "extra" in thing:
            self.assertEqual(thing.extra, "x")
        if "extra" in self.thing:
            self.thing.extra

    def test_not_asked(self) -> None:
        self.thing.extra
"""


def test_records_a_test_case_keeps_take_the_field_their_model_was_asked_for(
    run_mypy: RunMypy,
) -> None:
    result = run_mypy(
        {
            "t_base": T_BASE,
            "t_extra": T_EXTRA,
            "t_use": {
                "depends": ["t_base"],
                "tests/__init__.py": "from . import test_thing\n",
                "tests/test_thing.py": TESTS,
            },
        },
        check=["t_use"],
    )

    assert result.messages("t_use/tests/test_thing.py") == [
        '11: error: "Thing" has no attribute "extra"  [attr-defined]',
        '23: error: "Thing" has no attribute "extra"  [attr-defined]',
    ]
