"""Attributes of field objects that hold the arguments of the definition (``field_setup``)."""

from __future__ import annotations

from conftest import RunMypy

FIELD_OBJECTS = """
from typing import Any

from odoo import fields, models

GROUPS = "base.group_user"
OPTIONS: dict[str, Any] = {"groups": None}


class Journal(models.Model):
    _name = "t.journal"

    code = fields.Char(size=5, groups="base.group_user,base.group_system", help="Short")
    name = fields.Char("Label")
    constant = fields.Char(groups=GROUPS)
    emptied = fields.Char(size=0, groups="")
    spread = fields.Char(help="Spread", **OPTIONS)
    total = fields.Float(group_operator="sum", aggregator="sum")
    partner_id = fields.Many2one("res.partner", "Partner", ondelete="cascade")

    def check(self) -> None:
        reveal_type(self._fields["code"].size)
        reveal_type(self._fields["code"].groups)
        reveal_type(self._fields["code"].help)
        reveal_type(self._fields["constant"].groups)
        field = self._fields["code"]
        reveal_type(field.groups)
        reveal_type(self._fields["name"].groups)
        reveal_type(self._fields["name"].help)
        reveal_type(self._fields["emptied"].groups)
        reveal_type(self._fields["spread"].groups)
        reveal_type(self._fields["partner_id"].groups)

    def other(self, name: str, flag: bool) -> None:
        reveal_type(self._fields[name].groups)
        field = self._fields["code"]
        if flag:
            field = self._fields["name"]
        reveal_type(field.groups)
"""
OPTIONAL = "str | None"


def _revealed(messages: list[str]) -> list[str]:
    return [message.partition('"')[2].rstrip('"') for message in messages]


def test_an_attribute_of_a_field_object_holds_the_argument(
    run_mypy: RunMypy, odoo_version: int
) -> None:
    """``journal._fields["code"].size - len(counter)`` of ``fields.Char(size=5)`` (Odoo 19)."""
    messages = run_mypy({"my_mod": {"models.py": FIELD_OBJECTS}}).messages("models.py")

    if odoo_version == 17:  # the field classes annotate no attribute
        assert _revealed(messages) == ["Any"] * 12, messages
        return
    assert _revealed(messages) == [
        # Declared ``None`` on ``Char`` (19), ``int | None`` (20); Odoo 18 reads the
        # attributes of ``Field``, which has none of this name: ``Any``.
        "int" if odoo_version > 18 else "Any",
        "str",
        "str",
        "str",  # a constant of the module
        "str",  # through a local bound once
        OPTIONAL,  # not passed
        OPTIONAL,
        OPTIONAL,  # an empty literal
        OPTIONAL,  # arguments that are not named
        OPTIONAL,
        OPTIONAL,  # the field is not named by a literal
        OPTIONAL,  # a local bound twice
    ], (odoo_version, messages)


EXTENSION = """
from odoo import fields, models


class Journal(models.Model):
    _inherit = "t.journal"

    code = fields.Char(groups=False)
    name = fields.Char(help="The label")

    def check_extension(self) -> None:
        reveal_type(self._fields["code"].groups)
        reveal_type(self._fields["code"].size)
        reveal_type(self._fields["name"].help)
"""
READER = """
from odoo import models


class Partner(models.Model):
    _inherit = "res.partner"

    def check_journal(self) -> None:
        journal = self.env["t.journal"]
        reveal_type(journal._fields["code"].groups)
        reveal_type(journal._fields["name"].help)
"""


def test_the_definitions_the_reading_addon_sees_decide(
    run_mypy: RunMypy, odoo_version: int
) -> None:
    """A later definition replaces the argument; an addon that does not see it keeps it."""
    addons = {
        "t_journal": {"models.py": FIELD_OBJECTS},
        "t_extension": {"depends": ["t_journal"], "models.py": EXTENSION},
        "t_reader": {"depends": ["t_journal"], "models.py": READER},
    }

    result = run_mypy(addons, check=["t_extension", "t_reader"])

    extension = _revealed(result.messages("t_extension/models.py"))
    reader = _revealed(result.messages("t_reader/models.py"))
    if odoo_version == 17:  # the field classes annotate no attribute
        assert (extension, reader) == (["Any"] * 3, ["Any"] * 2)
        return
    size = "int" if odoo_version > 18 else "Any"  # see the test above
    assert extension == [OPTIONAL, size, "str"]
    assert reader == ["str", OPTIONAL]
