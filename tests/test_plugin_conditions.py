"""Aliases of conditions and ``assertTrue`` narrow what they test (odoo_typing.conditions)."""

from __future__ import annotations

import pytest
from conftest import RunMypy
from mypy.options import Options

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


BOOL_TESTS = """
from typing import TYPE_CHECKING, Any, Literal

from odoo import models
from odoo.tests import TransactionCase

class Order(models.Model):
    _inherit = "t.order"

    def tested(self, other: str | Literal[False]) -> None:
        if bool(self.name):
            reveal_type(self.name)
        if bool(self.name) or other:
            reveal_type(other)
        while not bool(other):
            return
        reveal_type(other)

    def asserted(self, other: str | Literal[False]) -> None:
        assert bool(self.name and other)
        reveal_type(other)

    def aliased(self, other: str | Literal[False]) -> None:
        named = self.name and other
        if bool(named):
            reveal_type(other)
        if str(other):
            reveal_type(other)

class TestOrder(TransactionCase):
    def test_found(
        self, rows: list[Any], text: str | Literal[False], when: Literal["day"] | None
    ) -> None:
        order = self.env["t.order"].create({})
        found = next((row for row in rows if row), False)
        self.assertTrue(bool(found), "found")
        reveal_type(found)
        self.assertTrue(bool(order.day) and bool(order.name))
        reveal_type(order.day)
        reveal_type(order.name)
        self.assertTrue(bool(text) or not bool(found))
        reveal_type(text)
        self.assertTrue(not bool(when))
        reveal_type(when)

class Checked(models.Model):
    _inherit = "t.order"

    def hook(self) -> None: ...

    def checked(self, xs: tuple[int], other: str | Literal[False]) -> None:
        if not bool(*xs):
            reveal_type(xs)
        if bool(self.hook):
            pass
        if (bool(other) and not TYPE_CHECKING) and other.upper():
            pass
"""


def test_a_test_of_bool_narrows_its_argument(run_mypy: RunMypy) -> None:
    """``if bool(x):``, ``self.assertTrue(bool(attachment))``: true exactly when ``x`` is."""
    result = run_mypy(
        {
            "t_base": {"models.py": T_BASE},
            "t_use": {"depends": ["t_base"], "models.py": BOOL_TESTS},
        },
        check=["t_use"],
    )

    assert [message.split(";")[0] for message in result.messages("t_use/models.py")] == [
        f'{line}: note: Revealed type is "{revealed}"'
        for line, revealed in [
            (12, "str"),
            (14, "str | Literal[False]"),  # ``or``: either may be true
            (17, "str"),  # after ``while not bool(other): return``
            (21, "str"),  # ``assert bool(a and b)``
            (26, "str"),  # ``bool(alias)``: what the alias implies
            (28, "str | Literal[False]"),  # ``str(False)`` is ``"False"``
            (37, "Any"),  # ``self.assertTrue(bool(found), msg)``
            (39, "datetime.datetime"),
            (40, "str"),
            (42, "str | Literal[False]"),  # ``or``
            (44, "None"),  # ``not bool(when)``
            (53, "tuple[int]"),  # ``bool(*xs)`` is the truth of ``xs[0]``: not unwrapped
        ]
    ] + [
        # ``bool(f)`` of a method is tested as ``f``, as mypy reports for ``if f:``
        '54: error: Function "hook" could always be true in boolean context  [truthy-function]'
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

    def captured(self) -> None:
        if not self.env.context.get("active_ids"):
            return
        match self.with_context(active_ids=False):
            case self: pass
        Command.set(self.env.context.get("active_ids"))

    def walrus(self) -> None:
        if not self.env.context.get("ids"):
            return
        Command.set(self.env.context.get("ids") if (self := self.with_context({})) else IDS)

    def walrus_below(self) -> None:
        if not self.env.context.get("ids"):
            return
        Command.set(
            self.env.context.get("ids")
            if (self := self.with_context({}))
            else IDS
        )

    def later(self) -> None:
        if not self.env.context.get("ids"):
            return
        f = lambda: Command.set(self.env.context.get("ids")); self = self.with_context({})
        f()

    def walrus_tested(self) -> None:
        if not self.env.context.get("ids"):
            return
        if Command.set(
            self.env.context.get("ids") if (self := self.with_context({})) else IDS
        ):
            pass

    def walrus_looped(self) -> None:
        while self.env.context.get("ids") and Command.set(
            self.env.context.get("ids") if (self := self.with_context({})) else IDS
        ):
            break

    def walrus_filtering(self) -> None:
        if not self.env.context.get("ids"):
            return
        [Command.set(self.env.context.get("ids")) for _ in [1] if (self := self.with_context({}))]

    def walrus_valued(self) -> None:
        if not self.env.context.get("ids"):
            return
        rows: dict[object, object] = {}
        rows[Command.set(self.env.context.get("ids"))] = (self := self.with_context({}))

    def matched(self) -> None:
        if not self.env.context.get("ids"):
            return
        match Command.set(self.env.context.get("ids")):
            case [*_] if (self := self.with_context({})):
                pass

    def aliased(self) -> None:
        ids = self.env.context.get("ids") or []
        if not ids:
            return
        Command.set(self.env.context.get("ids"))

    def aliased_not_none(self) -> None:
        given = bool(self.env.context.get("ids"))
        if given is not None:
            Command.set(self.env.context.get("ids"))

    def aliased_filled(self) -> None:
        ids = self.env.context.get("ids") or []
        ids.append(0)
        if ids:
            Command.set(self.env.context.get("ids"))

    def filled_not_none(self) -> None:
        given = self.env.context.get("ids")
        if given is not None:
            given.append(1)
            Command.set(self.env.context.get("ids"))

    def stale(self) -> None:
        self = self.env.context.get("ids") and self.with_context({})
        if self:
            Command.set(self.env.context.get("ids"))

    def chained(self) -> None:
        x = self = self.env.context.get("ids") and self.with_context({})
        if x:
            Command.set(self.env.context.get("ids"))


IDS: list[int] = [1]
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

    # Not proven: after the if; without a test; after ``self`` is bound again; after a
    # ``case`` capture of the name read; past a ``:=`` in the statement that runs first
    # (the test of a conditional expression, on the line or below, in an ``if`` or a
    # ``while`` test; a comprehension's condition; an assignment's value before its
    # target); in a lambda that may run after a binding later on its line. Proven: the
    # subject of a ``match``, whose guards run after it, and through a local bound to the
    # read ``or []`` (103), and one not ``None`` filled in place (120); not through one
    # only not ``None`` (108), the truth of one filled in place (114), nor a part of the
    # value that read the old ``self`` (125), also tested through another target of the
    # assignment (130). (The res.partner class is ``Partner`` in Odoo 17 and 18.)
    messages = result.messages("t_use/models.py")
    unset = 'error: Argument 1 to "set" of "Command" has incompatible type "Any | None"'
    kept = 'error: Argument 1 to "set" of "Command" has incompatible type "Any | list[int] | None"'
    assert [line.split(";")[0].replace('"Partner |', '"ResPartner |') for line in messages] == [
        f"21: {unset}",
        '32: error: Item "None" of "ResPartner | None" has no attribute "name"  [union-attr]',
        f"38: {unset}",
        f"45: {unset}",
        f"50: {kept}",
        f"56: {kept}",
        f"64: {unset}",
        f"71: {kept}",
        f"77: {kept}",
        f"84: {unset}",
        f"90: {unset}",
        f"108: {unset}",
        f"114: {unset}",
        '123: error: Incompatible types in assignment (expression has type "Any | Order | None",'
        ' variable has type "Order")  [assignment]',
        f"125: {unset}",
        '128: error: Incompatible types in assignment (expression has type "Any | Order | None",'
        ' variable has type "Order")  [assignment]',
        f"130: {unset}",
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
        if found:
            reveal_type(found.name)
        assert found
        reveal_type(found.name)
        kept = self.filtered("name")
        reveal_type(kept.name)
        if not kept:
            return
        reveal_type(kept.name)
        either = self.search(["|", ("name", "ilike", "x"), ("day", "!=", False)])
        if either:
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
        "str | Literal[False]",  # the search may find none: their name reads ``False``
        "str",  # a record of a ``for`` over them
        "str",  # tested that there are some
        "str",  # ``assert found``
        "str | Literal[False]",  # ``filtered("name")`` may keep none
        "str",  # ``if not kept: return``
        "str | Literal[False]",
    ], revealed


SEARCHED_IN_TEST = """
from odoo.tests.common import TransactionCase

class TestOrder(TransactionCase):
    def test_found(self):
        found = self.env["t.order"].search([("name", "ilike", "x")], limit=1)
        reveal_type(found.name)
        self.assertTrue(found)
        reveal_type(found.name)
        names = self.env["t.order"].search([("name", "ilike", "x")]).filtered("name")
        reveal_type(names and names.name)
"""


SEARCHED_GUARDS = """from collections import Counter; from contextlib import nullcontext
from odoo.tests.common import TransactionCase

class TestOrder(TransactionCase):
    def test_guards(self):
        Order = self.env["t.order"]
        one = Order.search([("name", "ilike", "x")], limit=1)
        one.ensure_one()
        reveal_type(one.name)
        sized = Order.search([("name", "ilike", "x")])
        self.assertEqual(len(sized), 2)
        reveal_type(sized.name)
        listed = Order.search([("name", "ilike", "x")])
        self.assertRecordValues(listed, [{"name": "x"}])
        reveal_type(listed.name)
        none = Order.search([("name", "ilike", "x")])
        self.assertRecordValues(none, [])
        reveal_type(none.name)
        counted = Order.search([("name", "ilike", "x")])
        if len(counted) != 1:
            self.skipTest("one order")
        reveal_type(counted.name)
        many = Order.search([("name", "ilike", "x")])
        if len(many) == 1:
            reveal_type(many.name)
        assert len(many)
        reveal_type(many.name)
        looped = Order.search([("name", "ilike", "x")])
        if not looped:
            return
        for _ in range(2):
            reveal_type(looped.name)
            looped = looped.filtered("name")
        nested = Order.search([("name", "ilike", "x")])
        if not nested:
            return
        if self:
            for _ in range(2):
                reveal_type(nested.name)
                nested = nested.filtered("name")
        size = Order.search([("name", "ilike", "x")])
        self.assertEqual(1, len(size))
        reveal_type(size.name)
        keyword = Order.search([("name", "ilike", "x")])
        self.assertRecordValues(records=keyword, expected_values=[{"name": "x"}])
        reveal_type(keyword.name)
        late = Order.search([("name", "ilike", "x")])
        if not late:
            return
        getter = lambda: reveal_type(late.name)
        late = late.filtered("name")
        header = Order.search([("name", "ilike", "x")])
        if not header:
            return
        for char in reveal_type(header.name):
            header = header.filtered("name")
        rows: list = []
        starred = Order.search([("name", "ilike", "x")])
        self.assertRecordValues(starred, [*rows])
        reveal_type(starred.name)
        partner = Order.search([("name", "ilike", "x")], limit=1)
        if partner:
            Order.filtered(lambda order: order.name
                           and reveal_type(partner.name))
        inner = Order.search([("name", "ilike", "x")])
        key = lambda: inner and reveal_type(inner.name)
        inner = inner.filtered("name")
        side = Order.search([("name", "ilike", "x")])
        if not side:
            return
        print(Order.filtered(lambda r: r.name), reveal_type(side.name))
        side = side.filtered("name")
        mixed = Order.search([("name", "ilike", "x")])
        self.assertRecordValues(mixed, [{"name": "x"}, *rows])
        reveal_type(mixed.name)
        same = Order.search([("name", "ilike", "x")])
        label = same and (lambda: reveal_type(same.name))
        same = same.filtered("name")
        comp = Order.search([("name", "ilike", "x")])
        if comp:
            names = [reveal_type(comp.name) for _ in range(2)]
        alias_src = Order.search([("name", "ilike", "x")])
        alias = alias_src
        for _ in range(2):
            if alias:
                reveal_type(alias_src.name)
            alias = Order.search([])
        lazy = Order.search([("name", "ilike", "x")])
        if lazy:
            gen = (reveal_type(lazy.name) for _ in range(2))
        lazy = lazy.filtered("name")
        seen_src = Order.search([("name", "ilike", "x")])
        seen = seen_src
        seen |= Order.browse(1)
        if seen:
            reveal_type(seen_src.name)
        src2 = Order.search([("name", "ilike", "x")])
        alias2 = src2
        src2 = src2.filtered(lambda r: r.id > 1)
        if alias2:
            reveal_type(src2.name)
        src3 = Order.search([("name", "ilike", "x")])
        alias3 = src3
        for _ in range(2):
            if alias3:
                reveal_type(src3.name)
            src3 = src3.filtered("name")
        src4 = Order.search([("name", "ilike", "x")])
        alias4 = src4
        key2 = lambda: alias4 and (reveal_type(src4.name) for _ in range(2))
        alias4 = Order.search([])
        gsrc = Order.search([("name", "ilike", "x")])
        if gsrc:
            print(any(reveal_type(gsrc.name) for _ in range(2)))
        gsrc = gsrc.filtered("name")
        csrc = Order.search([("name", "ilike", "x")])
        cnames = [reveal_type(csrc.name) for _ in range(2) if csrc]
        bsrc = Order.search([("name", "ilike", "x")])
        balias = Order.browse(1)
        if Order.search_count([]) > 1:
            balias = bsrc
        if balias:
            reveal_type(bsrc.name)

    def test_parameter(self, partner=None):
        found = self.env["t.order"].search([("name", "ilike", "x")])
        if not partner:
            partner = found
        if partner:
            reveal_type(found.name)

    def test_bindings_that_ran(self, partner=None):
        Order = self.env["t.order"]
        for i, p in enumerate(Order.search([])):
            lfound = Order.search([("name", "ilike", "x"), ("id", ">", p.id)])
            if i == 0:
                lfirst = lfound
            if lfirst:
                reveal_type(lfound.name)
        seq_src = Order.search([("name", "ilike", "x")])
        seq = Order.browse(1)
        seq = seq_src
        if seq:
            reveal_type(seq_src.name)
        found = Order.search([("name", "ilike", "x")])
        partner = found
        if partner:
            reveal_type(found.name)
        other = Order.browse(1)
        assert other; other = found; print(reveal_type(found.name))
        walrus_src = Order.search([("name", "ilike", "x")])
        walias = walrus_src
        print(walias := Order.browse(1))
        if walias:
            reveal_type(walrus_src.name)
        wfound = Order.search([("name", "ilike", "x")])
        print(wfound := Order.search([]))
        if wfound:
            reveal_type(wfound.name)
        calias_src = Order.search([("name", "ilike", "x")])
        calias = calias_src
        match Order.search([]):
            case calias: pass
        if calias:
            reveal_type(calias_src.name)
        for rec in Order.search([("name", "ilike", "x")]):
            print(rec := Order.browse(1))
            reveal_type(rec.name)
        sl = Order.search([("name", "ilike", "x")])
        if sl: sl = sl.filtered(lambda r: r.id > 5); print(reveal_type(sl.name))
        ids_found = Order.search([("name", "ilike", "x")])
        if ids_found.ids:
            reveal_type(ids_found.name)
        existing = Order.search([("name", "ilike", "x")])
        if not existing.exists():
            self.skipTest("none")
        reveal_type(existing.name)
        checked = Order.search([("name", "ilike", "x")])
        self.assertTrue(bool(checked))
        reveal_type(checked.name)
        via = Order.search([("name", "ilike", "x")])
        if via.env:
            reveal_type(via.name)
        sized_ids = Order.search([("name", "ilike", "x")])
        self.assertEqual(len(sized_ids.ids), 1)
        reveal_type(sized_ids.name)
        kept = Order.search([("name", "ilike", "x")])
        present = kept.exists()
        if present:
            reveal_type(kept.name)
        counted = Order.search([("name", "ilike", "x")])
        count = len(counted)
        if count:
            reveal_type(counted.name)
        argued = Order.search([("name", "ilike", "x")])
        if argued.exists(1):  # type: ignore[call-arg]
            reveal_type(argued.name)

    def test_shadowed(self):
        bool = lambda value: True
        found = self.env["t.order"].search([("name", "ilike", "x")])
        if bool(found):
            reveal_type(found.name)

    def test_aliases(self):
        Order = self.env["t.order"]
        compared = Order.search([("name", "ilike", "x")])
        single = len(compared) == 1
        self.assertTrue(single)
        reveal_type(compared.name)
        filled_src = Order.search([("name", "ilike", "x")])
        filled = filled_src.ids
        filled.append(0)
        if filled:
            reveal_type(filled_src.name)
        stored_src = Order.search([("name", "ilike", "x")])
        stored = stored_src.ids or []
        stored[0:0] = [0]
        if stored:
            reveal_type(stored_src.name)
        added_src = Order.search([("name", "ilike", "x")])
        added = Counter[int]() and added_src.ids
        added[0] += 1
        if added:
            reveal_type(added_src.name)
        pair_src = Order.search([("name", "ilike", "x")])
        pair = pair_src.ids or []
        pair[0:0], other = [0], 1
        if pair:
            reveal_type(pair_src.name)
        nested_src = Order.search([("name", "ilike", "x")])
        nested = nested_src.ids

        def fill() -> None:
            nested[0:0] = [0]

        fill()
        if nested:
            reveal_type(nested_src.name)
        second_src = Order.search([("name", "ilike", "x")])
        second = second_src.ids
        rows = second
        rows.append(0)
        if second:
            reveal_type(second_src.name)
        plus_src = Order.search([("name", "ilike", "x")])
        plus = plus_src.ids
        more = plus
        more += [0]
        if plus:
            reveal_type(plus_src.name)
        upd_src = Order.search([("name", "ilike", "x")])
        upd = upd_src
        upd_src.update({"name": "y"})
        if upd:
            reveal_type(upd_src.name)
        kept_src = Order.search([("name", "ilike", "x")])
        kept_ids = kept_src.ids
        if kept_ids:
            reveal_type(kept_src.name)
        kept_ids += [0]
        either_src = Order.search([("name", "ilike", "x")])
        either = either_src.ids
        rows2 = either and [1]
        rows2.append(0)
        if either:
            reveal_type(either_src.name)
        tup_src = Order.search([("name", "ilike", "x")])
        tup = tup_src.ids
        rows3, n3 = tup, 0
        rows3.append(n3)
        if tup:
            reveal_type(tup_src.name)
        wal_src = Order.search([("name", "ilike", "x")])
        wal = wal_src.ids
        print(rows4 := wal)
        rows4.append(0)
        if wal:
            reveal_type(wal_src.name)
        inner_src = Order.search([("name", "ilike", "x")])
        inner = inner_src.ids

        def alias_fill() -> None:
            rows5 = inner
            rows5.append(0)

        alias_fill()
        if inner:
            reveal_type(inner_src.name)
        sym_src = Order.search([("name", "ilike", "x")])
        sym = set[int]() and sym_src
        sym.symmetric_difference_update({1})  # type: ignore[union-attr]
        if sym:
            reveal_type(sym_src.name)
        sub_src = Order.search([("name", "ilike", "x")])
        sub = Counter[int]() and sub_src
        sub.subtract({1: 1})  # type: ignore[union-attr]
        if sub:
            reveal_type(sub_src.name)
        loop_src = Order.search([("name", "ilike", "x")])
        loop = Counter[int]() and loop_src.ids
        for loop[0] in [1]: pass
        if loop:
            reveal_type(loop_src.name)
        with_src = Order.search([("name", "ilike", "x")])
        within = Counter[int]() and with_src.ids
        with nullcontext(1) as within[0]: pass
        if within:
            reveal_type(with_src.name)
        and_src = Order.search([("name", "ilike", "x")])
        lst: list[int] = []
        and_rows = lst and and_src.ids
        lst.append(0)
        if and_rows:
            reveal_type(and_src.name)
        aug_src = Order.search([("name", "ilike", "x")])
        lst2: list[int] = []
        aug_rows = lst2 and aug_src.ids
        lst2 += [0]
        if aug_rows:
            reveal_type(aug_src.name)
        orl_src = Order.search([("name", "ilike", "x")])
        orl = orl_src.ids
        orl_rows = orl or []
        orl_rows.append(0)
        if orl:
            reveal_type(orl_src.name)
        cv_src = Order.search([("name", "ilike", "x")])
        cv = cv_src.ids
        rows6 = cv if self else []
        rows6.append(0)
        if cv:
            reveal_type(cv_src.name)
        wv_src = Order.search([("name", "ilike", "x")])
        lst3: list[int] = []
        wv_rows = (wt := lst3) and wv_src.ids
        lst3.append(len(wt))
        if wv_rows:
            reveal_type(wv_src.name)
        nl_src = Order.search([("name", "ilike", "x")])
        nl = nl_src.ids

        def bump() -> None:
            nonlocal nl
            nl += [0]

        bump()
        if nl:
            reveal_type(nl_src.name)
        ch_src = Order.search([("name", "ilike", "x")])
        ca = cb = ch_src.ids
        ca.append(0)
        if cb:
            reveal_type(ch_src.name)
        ex_src = Order.search([("name", "ilike", "x")])
        x5 = ex_src = ex_src.exists()
        if x5:
            reveal_type(ex_src.name)
        cls_src = Order.search([("name", "ilike", "x")])
        cls_ids = cls_src.ids

        class Filler:
            def run(self) -> None:
                cls_ids[0:0] = [0]

        Filler().run()
        if cls_ids:
            reveal_type(cls_src.name)
        sp_src = Order.search([("name", "ilike", "x")])
        sp = sp_src.ids
        if sp:
            reveal_type(sp_src.name)
        lst4: list[int] = []
        sp = lst4 and sp
        sp += [1]
        hb_src = Order.search([("name", "ilike", "x")])
        hb: list[int] = []
        hb_alias = hb
        hb_rows = hb_alias and hb_src.ids
        hb.append(0)
        if hb_rows:
            reveal_type(hb_src.name)
        ab_src = Order.search([("name", "ilike", "x")])
        ab_a = ab_b = ab_src.ids
        if ab_a:
            reveal_type(ab_src.name)
        ab_a += [0]
        cn_src = Order.search([("name", "ilike", "x")])
        cn = cn_src.ids
        if cn:
            reveal_type(cn_src.name)
        cn_rows = cn and [1]
        cn += [0]
        lp_src = Order.search([("name", "ilike", "x")])
        lp = lp_src.ids
        lx: list[int] = []
        lp_rows = lp and lx
        lx.append(0)
        if lp_rows:
            reveal_type(lp_src.name)
        ta_src = Order.search([("name", "ilike", "x")])
        ta = ta_src.ids
        tb = ta
        tc = tb
        tc.append(0)
        if ta:
            reveal_type(ta_src.name)
        tn_src = Order.search([("name", "ilike", "x")])
        td: list[int] = []
        te = td
        tg = te
        tn = tg and tn_src.ids
        td.append(0)
        if tn:
            reveal_type(tn_src.name)
        px_src = Order.search([("name", "ilike", "x")])
        px = px_src.ids
        py = px
        py.append(0)
        if px:
            reveal_type(px_src.name)
        px += [0]
        both_src = Order.search([("name", "ilike", "x")])
        both = both_src.ids
        both.append(0)
        if both:
            reveal_type(both_src.name)
        both += [1]
        co_src = Order.search([("name", "ilike", "x")])
        e2: list[int] = []
        c2 = e2
        d2 = e2
        n2 = c2 and co_src.ids
        d2.append(0)
        if n2:
            reveal_type(co_src.name)
        sf_src = Order.search([("name", "ilike", "x")])
        d3: list[int] = []
        x3 = d3
        x3 = x3 and sf_src.ids
        d3.append(0)
        if x3:
            reveal_type(sf_src.name)
"""


# mypy 2.4 parses natively by default, 2.1 with its Python parser (``--native-parser``
# off); the Python parser keeps no end of a lambda.
NATIVE_DEFAULT = getattr(Options(), "native_parser", False)
PARSERS = ["native", "python"] if NATIVE_DEFAULT else ["python"]


@pytest.mark.parametrize("parser", PARSERS)
def test_calls_and_sizes_that_prove_the_search_found_records(
    run_mypy: RunMypy, parser: str
) -> None:
    """``ensure_one()``, ``assertEqual(len(x), n)``, ``assertRecordValues(x, [...])``,
    ``len(x) == 1``, ``len(x) != 1`` then ``skipTest``, ``assert len(x)``, ``x.ids``,
    ``x.exists()``, ``bool(x)``: there are records (also ``assertEqual(1, len(x))``,
    keyword arguments, ``len(x.ids)``, a local bound to such a value). Not after
    ``x.env`` (there on no records too), ``x.exists(arg)``, a local ``bool``, through a
    local filled in place (``rows.append(v)``, ``rows[i:i] = ...``, ``more += [v]`` of
    another local that may hold it), nor after
    ``assertRecordValues(x, [])``, nor in a loop that binds the name again after the
    test, under an ``if`` too."""
    files = {
        "t_use": {
            "depends": ["t_base"],
            "tests/__init__.py": "from . import test_order\n",
            "tests/test_order.py": SEARCHED_GUARDS,
        }
    }
    flags = ["--no-native-parser"] if parser == "python" and NATIVE_DEFAULT else []
    result = run_mypy({"t_base": {"models.py": T_BASE}, **files}, check=["t_use"], flags=flags)

    lines = {
        line.split(":")[0]: line.partition('"')[2].rstrip('"')
        for line in result.messages("t_use/tests/test_order.py")
    }
    assert lines == {
        **dict.fromkeys(("9", "12", "15", "22", "25", "27", "43", "46"), "str"),
        "18": "str | Literal[False]",  # an empty list of values: maybe no records
        "32": "str | Literal[False]",  # bound again in the loop: the next pass may have none
        "39": "str | Literal[False]",  # so in a loop under an ``if``
        "50": "str | Literal[False]",  # a lambda runs later: after the name is bound again
        "55": "str",  # the iterable of a ``for`` is read once, before its body binds it
        "60": "str | Literal[False]",  # ``[*rows]`` may be empty
        "64": "str",  # on a later line of a lambda the test holds
        "66": "str",  # a test inside the lambda runs with the read
        "71": "str",  # a lambda on the line that does not hold the read
        "75": "str",  # ``[{...}, *rows]`` has an item
        "77": "str | Literal[False]",  # the test on the lambda's line is outside it
        "81": "str",  # a list comprehension runs where it is
        "86": "str | Literal[False]",  # the alias tested holds other records on the next pass
        "90": "str | Literal[False]",  # a generator runs later, after the name is bound again
        "96": "str | Literal[False]",  # ``seen |= ...``: the alias holds other records
        "101": "str | Literal[False]",  # the alias holds the records from before the binding
        "106": "str | Literal[False]",  # so on the next pass of the loop
        "110": "str | Literal[False]",  # the test in the lambda, the alias bound after it
        "114": "str",  # ``any(...)`` iterates the generator at once
        "117": "str",  # a condition of the comprehension
        "123": "str | Literal[False]",  # which binding of the alias ran is not known
        "130": "str | Literal[False]",  # a parameter may hold what the caller passed
        "139": "str | Literal[False]",  # a later pass skips the binding: the alias is stale
        "144": "str",  # the last binding, a statement on the way to the read, ran
        "148": "str",  # the parameter bound again before the test
        "150": "str | Literal[False]",  # bound after the test on its line: of ``other``
        "155": "str | Literal[False]",  # ``(walias := ...)`` binds the alias again
        "159": "str | Literal[False]",  # ``(wfound := ...)``: other records than the search's
        "165": "str | Literal[False]",  # ``case calias:`` binds the alias again
        "168": "str | Literal[False]",  # the record of the ``for``, bound again by ``:=``
        "170": "str | Literal[False]",  # bound again after the test on its line
        "173": "str",  # ``x.ids`` lists ids of the records
        "177": "str",  # ``x.exists()`` gives some of them
        "180": "str",  # ``bool(x)`` is as true as ``x``
        "183": "str | Literal[False]",  # ``x.env`` is there on no records too
        "186": "str",  # ``len(x.ids)`` of one
        "190": "str",  # through a local bound to ``x.exists()``
        "194": "str",  # through a local bound to ``len(x)``
        "197": "str | Literal[False]",  # no ``exists()`` of Odoo's: it takes no argument
        "203": "str | Literal[False]",  # a local ``bool`` is not the builtin
        "210": "str",  # through a local bound to ``len(x) == 1``
        "215": "str | Literal[False]",  # the local is filled in place: true anyway
        "220": "str | Literal[False]",  # so after an item is stored in it
        "225": "str | Literal[False]",  # ``x[k] += v`` (an empty ``Counter`` gets a key)
        "230": "str | Literal[False]",  # a store in a tuple target
        "239": "str | Literal[False]",  # a store in a nested function
        "245": "str | Literal[False]",  # filled through another local bound to it
        "251": "str | Literal[False]",  # ``more += [0]`` fills ``plus`` too
        "256": "str",  # filling ``upd_src`` leaves ``upd`` an alias of it: the same records
        "260": "str",  # ``kept_ids += [0]`` after the test binds it again, there
        "267": "str | Literal[False]",  # filled through a local that may be it (``x and [1]``)
        "273": "str | Literal[False]",  # so through a tuple binding
        "279": "str | Literal[False]",  # so through ``(rows := x)``
        "289": "str | Literal[False]",  # so through a local of a nested function
        "294": "str | Literal[False]",  # ``symmetric_difference_update`` adds items
        "299": "str | Literal[False]",  # so does ``Counter.subtract``
        "304": "str | Literal[False]",  # so does a ``for`` target ``x[k]``
        "309": "str | Literal[False]",  # so does a ``with`` target ``x[k]``
        "315": "str | Literal[False]",  # ``lst and x.ids`` is ``lst`` while it is empty
        "321": "str | Literal[False]",  # so when ``lst += [0]`` fills it
        "327": "str",  # ``x or []`` is ``x`` only when it is true already
        "333": "str | Literal[False]",  # filled through ``x if c else []``
        "339": "str | Literal[False]",  # ``(t := lst) and x.ids``, ``lst`` filled
        "349": "str | Literal[False]",  # ``nonlocal x; x += [0]`` in a nested function
        "354": "str | Literal[False]",  # ``a = b = x.ids``, ``a`` filled: ``b`` is the same list
        "358": "str",  # ``x5 = found = found.exists()``: the same records as ``x5``
        "368": "str | Literal[False]",  # filled in a method of a class in the function
        "372": "str",  # filled only by its own ``sp += [1]``, after the test (``sp = lst4 and sp``)
        "382": "str | Literal[False]",  # ``hb_rows`` may be ``hb``'s list, through ``hb_alias``
        "386": "str",  # ``ab_a += [0]`` after the test: ``ab_b`` is that list, not ``ab_a``'s test
        "391": "str",  # ``cn += [0]`` after the test, through ``cn_rows = cn and [1]`` too
        "400": "str",  # ``lp_rows`` is ``lx`` only when ``lp`` was true
        "407": "str | Literal[False]",  # ``tc``, bound to ``tb`` bound to ``ta``, is ``ta``'s list
        "415": "str | Literal[False]",  # ``tn`` may be ``tg``, ``te``, that is ``td``'s list
        "421": "str | Literal[False]",  # ``py.append(0)`` fills ``px``, whatever its own ``+=``
        "427": "str | Literal[False]",  # filled by ``append`` too, not only by its own ``+=``
        "436": "str | Literal[False]",  # ``c2`` and ``d2`` are one list, ``e2``'s
        "443": "str | Literal[False]",  # ``x3 = x3 and ...``: ``x3`` may be ``d3``'s list
    }, lines


def test_a_test_that_asserts_the_search_found_records_reads_the_field_set(
    run_mypy: RunMypy,
) -> None:
    """``self.assertTrue(found)``, then ``found.body_html``: there are records, and the
    domain set the field; before the assertion, none may have been found."""
    files = {
        "t_use": {
            "depends": ["t_base"],
            "tests/__init__.py": "from . import test_order\n",
            "tests/test_order.py": SEARCHED_IN_TEST,
        }
    }
    result = run_mypy({"t_base": {"models.py": T_BASE}, **files}, check=["t_use"])

    revealed = [
        line.partition('"')[2].rstrip('"') for line in result.messages("t_use/tests/test_order.py")
    ]
    assert revealed[:2] == ["str | Literal[False]", "str"], revealed
    # ``names and names.name``: the records when there are none, else the name, set
    assert revealed[2].startswith("t_base.models.Order") and revealed[2].endswith("| str")


SEARCHED_FIELD_READ = """
from odoo.tests.common import TransactionCase

class TestOrder(TransactionCase):
    def test_read(self):
        Order = self.env["t.order"]
        a = Order.search([("name", "ilike", "x")])
        if a.partner_id:
            reveal_type(a.name)
        b = Order.search([("name", "ilike", "x")], limit=1)
        self.assertTrue(b.day)
        reveal_type(b.name)
        c = Order.search([("name", "ilike", "x")])
        if c.env:
            reveal_type(c.name)
        d = Order.search([("name", "ilike", "x")])
        partner = d.partner_id
        if partner:
            reveal_type(d.name)
        e = Order.search([("name", "ilike", "x")])
        if not e.partner_id:
            return
        reveal_type(e.name)
        f = Order.search([("name", "ilike", "x")])
        if f.partner_id.name:
            reveal_type(f.name)
        g = Order.search([("name", "ilike", "x")])
        if g.partner_id is not None:
            reveal_type(g.name)
        h = Order.search([("name", "ilike", "x")])
        if h._name:
            reveal_type(h.name)
        k = Order.search([("name", "ilike", "x")])
        if not k.partner_id:
            reveal_type(k.name)
        m = Order.search([("name", "ilike", "x")])
        if m.partner_id:
            m = Order.search([("name", "ilike", "y")])
            reveal_type(m.name)
        n = Order.search([("name", "ilike", "x")])
        if n.line_ids.ids:
            reveal_type(n.name)
        p = Order.search([("name", "ilike", "x")])
        if len(p.line_ids):
            reveal_type(p.name)
        q = Order.search([("name", "ilike", "x")])
        if bool(q.partner_id):
            reveal_type(q.name)
        r = Order.search([("name", "ilike", "x")])
        if r.done:
            reveal_type(r.name)
        s = Order.search([("done", "=", True)])
        if s.partner_id:
            reveal_type(s.done)
"""

FIELD_READ_MODELS = """
from odoo import fields, models

class Order(models.Model):
    _inherit = "t.order"

    done = fields.Boolean()
    line_ids = fields.Many2many("t.order")
"""


def test_a_true_read_of_a_field_of_the_searched_records_proves_there_are_some(
    run_mypy: RunMypy,
) -> None:
    """``if found.partner_id:``: off no records a field reads its null value, false, so
    there are records, and the domain set ``name``. Not a read that is no field
    (``found.env``, ``found._name``), a field of the comodel (``found.partner_id.name``),
    ``is not None``, a false read, nor records bound again after it. ``.ids``, ``len()``
    and ``bool()`` of a field are true reads of it, a true Boolean field too; the value of
    a Boolean field keeps ``True``."""
    files = {
        "t_use": {
            "depends": ["t_base"],
            "tests/__init__.py": "from . import test_order\n",
            "models.py": FIELD_READ_MODELS,
            "tests/test_order.py": SEARCHED_FIELD_READ,
        }
    }
    result = run_mypy({"t_base": {"models.py": T_BASE}, **files}, check=["t_use"])

    lines = {
        line.split(":")[0]: line.partition('"')[2].rstrip('"')
        for line in result.messages("t_use/tests/test_order.py")
    }
    assert lines == {
        "9": "str",  # ``if a.partner_id:``
        "12": "str",  # ``self.assertTrue(b.day)``
        "15": "str | Literal[False]",  # ``env`` is no field
        "19": "str",  # ``partner = d.partner_id``, then ``if partner:``
        "23": "str",  # ``if not e.partner_id: return``
        "26": "str | Literal[False]",  # a field of the comodel: not followed
        "29": "str | Literal[False]",  # ``is not None`` proves nothing of records
        "32": "str | Literal[False]",  # ``_name`` is no field
        "35": "str | Literal[False]",  # a false read
        "39": "str | Literal[False]",  # ``m`` bound again after the test
        "42": "str",  # ``.ids`` of a field's records
        "45": "str",  # ``len()`` of a field's records
        "48": "str",  # ``bool()`` of a field
        "51": "str",  # a true Boolean field
        "54": "bool",  # a Boolean field reads ``bool``, no union: kept as it is
    }, lines


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
        if late:
            reveal_type(late.day)
        late = late.filtered(lambda order: order.partner_id).sorted("day")
        for order in late:
            reveal_type(order.day)
        coded = self.search([("name", "=like", f"{prefix}%")])
        if coded:
            reveal_type(coded.name)
        named = self.search([("name", "<", "m")])  # Odoo 19: "" < "m" takes the empty names
        if named:
            reveal_type(named.name)
        either = self.search([("day", "<", flag)])  # a Datetime field, whatever the value
        if either:
            reveal_type(either.day)
        wide = self.search([("name", "like", prefix)])  # "" matches every record
        if wide:
            reveal_type(wide.name)
        grown = self.search([("day", "<", start)])
        grown = self.search([])
        if grown:
            reveal_type(grown.day)
        texts = self.search([("day", ">=", fields.Datetime.to_string(start))])
        if texts:
            reveal_type(texts.day)  # a Datetime field: NULL is not greater
        exact = self.search([("name", "=like", prefix)])  # Odoo 19: "" is ``= False``
        if exact:
            reveal_type(exact.name)
        joined = self.search([("name", "=ilike", prefix + "%")])
        if joined:
            reveal_type(joined.name)
        arriving = self.search([("arrive", "<", start)])  # its search= method decides
        if arriving:
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
