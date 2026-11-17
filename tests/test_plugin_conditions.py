"""Aliases of conditions and ``assertTrue`` narrow what they test (odoo_typing.conditions)."""

from __future__ import annotations

import pytest
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


GUARDED = """
from odoo import fields, models
from odoo.tools import format_date, format_datetime, str2bool

class Order(models.Model):
    _inherit = "t.order"

    def labels(self) -> None:
        format_date(self.env, self.day)  # ``if not value: return ''``
        str2bool(self.name)  # ``if type(s) is bool: return s``
        format_datetime(self.env, self.day)  # no such guard

    def dates(self, text: str) -> None:
        if self.env.context.get("date"):
            day = fields.Date.from_string(self.env.context["date"])
            reveal_type(day)
        reveal_type(fields.Date.to_date(text))
        reveal_type(fields.Date.to_date(self.day))
"""


@pytest.mark.odoo(18, 19, 20)
def test_odoo_functions_take_the_empty_values_they_handle(
    run_mypy: RunMypy, odoo_version: int
) -> None:
    """``format_date(env, line.date_maturity)``: Odoo starts it with ``if not value:``."""
    base = T_BASE.replace("day = fields.Datetime()", "day = fields.Date()")
    result = run_mypy(
        {"t_base": {"models.py": base}, "t_use": {"depends": ["t_base"], "models.py": GUARDED}},
        check=["t_use"],
    )

    messages = [line.split(";")[0] for line in result.messages("t_use/models.py")]
    assert messages[0].startswith('11: error: Argument 2 to "format_datetime" has incompatible')
    if odoo_version >= 19:  # Odoo 18 does not annotate to_date
        assert messages[1:] == [
            '16: note: Revealed type is "datetime.date"',  # the context value was found true
            '17: note: Revealed type is "datetime.date | None"',  # "" gives None
            '18: note: Revealed type is "datetime.date | None"',
        ]


SEARCHED = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self, ids: list[int]) -> None:
        rows = self.search_read(
            [("partner_id", "in", ids), "!", ("name", "=", "x")], ["partner_id", "name"]
        )
        reveal_type(rows[0]["partner_id"])
        reveal_type(rows[0]["name"])
        found = self.search([("name", "ilike", "x")])
        reveal_type(found.name)
        for order in found:
            reveal_type(order.name)
        either = self.search(["|", ("name", "ilike", "x"), ("day", "!=", False)])
        reveal_type(either.name)
"""


def test_fields_the_search_domain_sets_have_no_false(run_mypy: RunMypy) -> None:
    """``search_read([("product_id", "in", subquery)])``, ``search([("x", "ilike", "y")])``."""
    result = run_mypy(
        {"t_base": {"models.py": T_BASE}, "t_use": {"depends": ["t_base"], "models.py": SEARCHED}},
        check=["t_use"],
    )

    revealed = [line.partition('"')[2].rstrip('"') for line in result.messages("t_use/models.py")]
    # Under ``!`` and ``|`` the domain sets nothing.
    assert revealed == [
        "tuple[int, str]",
        "str | Literal[False]",
        "str",
        "str",
        "str | Literal[False]",
    ], revealed
