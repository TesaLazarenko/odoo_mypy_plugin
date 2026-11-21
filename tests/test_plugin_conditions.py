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


BOUND_AGAIN = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def updated(self, allowed: list[str]) -> bool:
        dates = (self.day and allowed) or []
        if not dates or self.day.date().isoformat() in dates:
            return True
        self.day = False
        return False

    def changed_before(self, allowed: list[str]) -> None:
        order = self
        dates = (order.day and allowed) or []
        order = self.browse(1)
        if dates:
            order.day.date()

    def changed_in_loop(self, allowed: list[str]) -> None:
        dates = (self.day and allowed) or []
        for order in self:
            if dates:
                self.day.date()
            self.day = order.day

    def each(self, allowed: list[str]) -> None:
        for order in self:
            dates = (order.day and allowed) or []
            if dates:
                order.day.date()
            order.day = False

    def stale(self, allowed: list[str]) -> None:
        for order in self:
            if order.name:
                dates = (order.day and allowed) or []
            if dates:
                order.day.date()

    def twice(self, allowed: list[str]) -> None:
        dates = (self.day and allowed) or []
        if dates:
            self.day.date()
        dates = (self.day and allowed[1:]) or []
        assert dates
        self.day.date()

    def alias_changed_in_loop(self, allowed: list[str]) -> None:
        dates = (self.day and allowed) or []
        for order in self:
            if dates:
                self.day.date()
            dates = order.name and allowed

    def alias_skipped(self, allowed: list[str]) -> None:
        dates = allowed
        if self.name:
            dates = (self.day and allowed) or []
        if dates:
            self.day.date()

    def after_loop(self, allowed: list[str]) -> None:
        for order in self:
            if order.name:
                dates = (order.day and allowed) or []
        if dates:
            order.day.date()
"""


def test_an_alias_holds_until_what_it_tested_is_bound_again(run_mypy: RunMypy) -> None:
    """``dates = (self.day and allowed) or []``, ``if dates:``, later ``self.day = x``."""
    result = run_mypy(
        {
            "t_base": {"models.py": T_BASE},
            "t_use": {"depends": ["t_base"], "models.py": BOUND_AGAIN},
        },
        check=["t_use"],
    )

    # The alias says nothing where the name it tested, or the alias itself, may have
    # been bound again since: in between, later in a loop around both, or on a path
    # that skipped the alias (an earlier value, an earlier pass of the loop).
    error = 'error: Item "bool" of "datetime | Literal[False]" has no attribute "date"'
    messages = [message.split("  [")[0] for message in result.messages("t_use/models.py")]
    assert messages == [f"{line}: {error}" for line in (19, 25, 40, 54, 62, 69)], messages


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
                "demo": {"demo.xml": DEMO},
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
from odoo.tools import format_date, format_datetime, get_lang, str2bool

class Order(models.Model):
    _inherit = "t.order"

    def labels(self) -> None:
        format_date(self.env, self.day)  # ``if not value: return ''``
        str2bool(self.name)  # ``if type(s) is bool: return s``
        format_datetime(self.env, self.day)  # no such guard
        get_lang(self.env, self.name)  # ``if lang_code and lang_code in langs:`` alone

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
    dates = ["Any", "Any", "Any"]  # Odoo 18 does not annotate to_date
    if odoo_version >= 19:
        dates = [
            "datetime.date",  # the context value was found true
            "datetime.date | None",  # "" gives None
            "datetime.date | None",
        ]
    assert messages[1:] == [
        f'{line}: note: Revealed type is "{date}"'
        for line, date in zip((17, 18, 19), dates, strict=True)
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
        reveal_type(rows[0].get("partner_id"))  # mypy's own get of a TypedDict
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
        "tuple[int, str]",  # a key of a total TypedDict
        "str",
        "str",
        "str | Literal[False]",
    ], revealed


SEARCHED_COMPARED = """
from datetime import datetime

from odoo import fields, models

class Order(models.Model):
    _inherit = "t.order"

    arrive = fields.Datetime(compute="_compute_arrive", search="_search_arrive")

    def _compute_arrive(self) -> None:
        self.arrive = False

    def _search_arrive(self, operator: str, value: datetime) -> list[tuple[str, str, datetime]]:
        return [("day", operator, value)]

    def check(self, start: datetime, prefix: str, flag: datetime | bool) -> None:
        late = self.search([("day", "<=", fields.Datetime.now()), ("day", ">", start)])
        reveal_type(late.day)
        late = late.filtered(lambda order: order.partner_id).sorted("day")
        for order in late:
            reveal_type(order.day)
        coded = self.search([("name", "=like", f"{prefix}%")])
        reveal_type(coded.name)
        named = self.search([("name", "<", "m")])  # Odoo 19: "" < "m" takes the empty names
        reveal_type(named.name)
        either = self.search([("day", "<", flag)])  # a Datetime field, whatever the value
        reveal_type(either.day)
        wide = self.search([("name", "like", prefix)])  # "" matches every record
        reveal_type(wide.name)
        grown = self.search([("day", "<", start)])
        grown = self.search([])
        reveal_type(grown.day)
        texts = self.search([("day", ">=", fields.Datetime.to_string(start))])
        reveal_type(texts.day)  # a Datetime field: NULL is not greater
        exact = self.search([("name", "=like", prefix)])  # Odoo 19: "" is ``= False``
        reveal_type(exact.name)
        joined = self.search([("name", "=ilike", prefix + "%")])
        reveal_type(joined.name)
        arriving = self.search([("arrive", "<", start)])  # its search= method decides
        reveal_type(arriving.arrive)
"""


def test_inequalities_with_a_date_and_exact_patterns_set_the_field(run_mypy: RunMypy) -> None:
    """``search([("date_from", "<=", now)])``, ``search([("code", "=like", f"{prefix}%")])``.

    ``x = x.filtered(...).sorted(...)`` keeps some of the records: still the search's.
    """
    files = {"t_use": {"depends": ["t_base"], "models.py": SEARCHED_COMPARED}}
    result = run_mypy({"t_base": {"models.py": T_BASE}, **files}, check=["t_use"])

    revealed = [line.partition('"')[2].rstrip('"') for line in result.messages("t_use/models.py")]
    assert revealed == [
        "datetime.datetime",
        "datetime.datetime",
        "str",
        "str | Literal[False]",
        "datetime.datetime",
        "str | Literal[False]",
        "datetime.datetime | Literal[False]",
        "datetime.datetime",
        "str | Literal[False]",
        "str",
        "datetime.datetime | Literal[False]",
    ], revealed


SEARCHED_THROUGH = """
from datetime import date, datetime
from typing import Any

from odoo import fields, models

class Move(models.Model):
    _name = "t.move"

    due = fields.Date(compute="_compute_due", search="_search_due")

    def _compute_due(self) -> None:
        self.due = False

    def _search_due(self, operator: str, value: date) -> list[Any]:
        return [("id", "!=", 0)]

class Line(models.Model):
    _name = "t.line"
    _inherits = {"t.move": "move_id"}

    move_id = fields.Many2one("t.move", required=True, ondelete="cascade")
    move_due = fields.Date(related="move_id.due")

    def check(self, day: date) -> None:
        inherited = self.search([("due", "<", day)])  # the search= of t.move decides
        reveal_type(inherited.due)
        related = self.search([("move_due", "<", day)])
        reveal_type(related.move_due)

class Order(models.Model):
    _inherit = "t.order"

    def given(self, start: datetime, orders: "Order", flag: bool) -> None:
        if flag:
            orders = self.search([("day", "<", start)])
        orders = orders.sorted()  # still the caller's records when flag is false
        reveal_type(orders.day)

    def rows(self) -> None:
        rows = self.search_read([("partner_id", ">", 3)], ["partner_id"])
        reveal_type(rows[0]["partner_id"])
"""


def test_related_fields_and_parameters_keep_false_after_an_inequality(run_mypy: RunMypy) -> None:
    """A ``related`` field, one of ``_inherits``: the field it relates to is searched.

    ``search_read`` with an inequality on a ``Many2one`` gives rows where it is set.
    """
    files = {"t_use": {"depends": ["t_base"], "models.py": SEARCHED_THROUGH}}
    result = run_mypy({"t_base": {"models.py": T_BASE}, **files}, check=["t_use"])

    revealed = [line.partition('"')[2].rstrip('"') for line in result.messages("t_use/models.py")]
    assert revealed == [
        "datetime.date | Literal[False]",
        "datetime.date | Literal[False]",
        "datetime.datetime | Literal[False]",
        "tuple[int, str]",
    ], result.messages("t_use/models.py")


SEARCHED_LOOPS = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        first = self.search([("name", "ilike", "x")])
        second = self.search([("name", "ilike", "y")])
        for order in first:
            reveal_type(order.name)
        for order in second:
            reveal_type(order.name)
            order = self.browse(1)
            reveal_type(order.name)
        for order in first:
            for _ in range(2):
                reveal_type(order.name)
                order = self.browse(1)
        for order in self:
            reveal_type(order.name)
        third = self.search([("name", "ilike", "z")])
        reveal_type(third.name)
        third |= self.browse(1)
"""


def test_the_loop_around_a_read_gives_its_records(run_mypy: RunMypy) -> None:
    """``for lead in team1_leads: ...``, then ``for lead in team2_leads: lead.email_from``."""
    result = run_mypy(
        {
            "t_base": {"models.py": T_BASE},
            "t_use": {"depends": ["t_base"], "models.py": SEARCHED_LOOPS},
        },
        check=["t_use"],
    )

    revealed = [line.partition('"')[2].rstrip('"') for line in result.messages("t_use/models.py")]
    # Not where the loop variable may hold other records: bound again in the loop before
    # the read, or in a loop of its body around the read; a loop over other records; a
    # local that ``|=`` binds again, even where read before it (the local is bound once).
    assert revealed == [
        "str",
        "str",
        "str | Literal[False]",
        "str | Literal[False]",
        "str | Literal[False]",
        "str | Literal[False]",
    ], revealed
