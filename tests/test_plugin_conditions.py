"""Aliases of conditions and ``assertTrue`` narrow what they test (odoo_typing.conditions)."""

from __future__ import annotations

from conftest import RunMypy

T_BASE = """
from typing import Any

from odoo import fields, models

class Order(models.Model):
    _name = "t.order"

    name = fields.Char()
    day = fields.Datetime()
    partner_id = fields.Many2one("res.partner")
"""

ALIASES = """
import re
from typing import Any

from odoo import models
from odoo.tests import TransactionCase

class Order(models.Model):
    _inherit = "t.order"

    def trigger(self, sending: bool, text: str) -> None:
        match = re.match("x", text)
        enabled = match and match.start() > 0
        if sending and enabled:
            match.groupdict()

    def scores(self, result: dict[str, Any], key: str) -> None:
        found = result.get(key)
        value = found.get("x") if found else False
        if value:
            found["total"]

    def dates(self, allowed: list[str]) -> None:
        dates = (self.day and allowed) or []
        if not dates or self.day.date().isoformat() in dates:
            pass

    def rebound(self, text: str) -> None:
        match = re.match("x", text)
        enabled = match and match.start() > 0
        match = re.match("y", text)
        if enabled:
            match.groupdict()

class TestOrder(TransactionCase):
    def test_day(self) -> None:
        order = self.env["t.order"].create({})
        self.assertTrue(order.day, "a day")
        order.day.date()
        self.assertTrue(order.name.upper())
"""


def test_aliases_of_conditions_and_assert_true_narrow(run_mypy: RunMypy) -> None:
    """``cron_enabled = cron and ...`` then ``if cron_enabled:``; ``self.assertTrue(x)``."""
    result = run_mypy(
        {
            "t_base": {"models.py": T_BASE},
            "t_use": {"depends": ["t_base"], "models.py": ALIASES},
        },
        check=["t_use"],
    )

    # A name bound again after the alias keeps its own type; an error in the asserted
    # value is reported once.
    assert [message.split(";")[0] for message in result.messages("t_use/models.py")] == [
        '33: error: Item "None" of "Match[str] | None" has no attribute "groupdict"  [union-attr]',
        '40: error: Item "bool" of "str | Literal[False]" has no attribute "upper"  [union-attr]',
    ]


REPEATED = """
from odoo import Command, models

class Order(models.Model):
    _inherit = "t.order"

    def guarded(self) -> None:
        if not self.env.context.get("active_ids"):
            return
        Command.set(self.env.context.get("active_ids"))

    def sized(self) -> None:
        if len(self.env.context.get("active_ids") or []) < 2:
            raise ValueError("two")
        Command.set(self.env.context.get("active_ids"))

    def inside(self) -> None:
        if self.env.context.get("active_ids"):
            Command.set(self.env.context["active_ids"])
            Command.set(self.env.context.get("active_ids"))
        Command.set(self.env.context.get("active_ids"))

    def partner(self) -> None:
        partner = self.env.ref("base.res_partner_2", raise_if_not_found=False)
        if not partner:
            raise ValueError("demo")
        partner = self.env.ref("base.res_partner_2", raise_if_not_found=False)
        partner.name

    def unguarded(self) -> None:
        partner = self.env.ref("base.res_partner_2", raise_if_not_found=False)
        partner.name

    def changed(self) -> None:
        if not self.env.context.get("active_ids"):
            return
        self = self.with_context(active_ids=False)
        Command.set(self.env.context.get("active_ids"))
"""


DEMO = '<odoo><record id="base.res_partner_2" model="res.partner"/></odoo>'


def test_a_read_of_env_proven_true_before_is_true_again(run_mypy: RunMypy) -> None:
    """``if not self.env.context.get("k"): return``, then ``self.env.context.get("k")``."""
    result = run_mypy(
        {
            "t_base": {"models.py": T_BASE},
            "t_use": {
                "depends": ["t_base"],
                "models.py": REPEATED,
                "demo": {
                    "demo.xml": '<odoo><record id="base.res_partner_2" model="res.partner"/></odoo>'
                },
            },
        },
        check=["t_use"],
    )

    # Not proven: after the if; without a test; after ``self`` is bound again. (The
    # res.partner class is ``Partner`` in Odoo 17 and 18.)
    messages = result.messages("t_use/models.py")
    assert [line.split(";")[0].replace('"Partner |', '"ResPartner |') for line in messages] == [
        '21: error: Argument 1 to "set" of "Command" has incompatible type "Any | None"',
        '32: error: Item "None" of "ResPartner | None" has no attribute "name"  [union-attr]',
        '38: error: Argument 1 to "set" of "Command" has incompatible type "Any | None"',
    ]
