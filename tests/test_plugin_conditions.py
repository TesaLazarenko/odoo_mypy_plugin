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
