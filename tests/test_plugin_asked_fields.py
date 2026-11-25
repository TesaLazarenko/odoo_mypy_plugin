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

    def asked_through_get(self, other: "Thing") -> None:
        field = self._fields.get("extra")
        if field and field.type == "char":
            thing = self.sudo()
            thing.extra
            thing.other
            other.sudo().extra
            other.sudo().other
            other.extra
            self.extra
        else:
            self.sudo().extra
        self.with_context(a=1).extra
        if "extra" not in other._fields:
            return
        other.with_user(1).extra

    def answered_no(self, other: "Thing") -> None:
        if "extra" not in self:
            self.extra
        if "extra" in other._fields:
            other.extra
        else:
            other.extra
            self.name
        for thing in self:
            if "extra" in thing:
                continue
            thing.extra
        if "extra" in other:
            return
        other.extra

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
        # ``field = self._fields.get("extra")``, tested: ``self.sudo()`` has the field there
        '32: error: "Thing" has no attribute "other"  [attr-defined]',
        '33: error: "Thing" has no attribute "extra"  [attr-defined]',  # other was not asked
        '34: error: "Thing" has no attribute "other"  [attr-defined]',
        '35: error: "Thing" has no attribute "extra"  [attr-defined]',
        '36: error: "Thing" has no attribute "extra"  [attr-defined]',  # self: no new type
        '38: error: "Thing" has no attribute "extra"  [attr-defined]',  # the else branch
        '39: error: "Thing" has no attribute "extra"  [attr-defined]',  # after the test
        # where the answer is no: the body of ``not in``, the ``else`` branch, after
        # ``continue`` and after ``return``
        '46: error: "Thing" has no attribute "extra"  [attr-defined]',
        '50: error: "Thing" has no attribute "extra"  [attr-defined]',
        '55: error: "Thing" has no attribute "extra"  [attr-defined]',
        '58: error: "Thing" has no attribute "extra"  [attr-defined]',
        '61: error: "Thing" has no attribute "extra"  [attr-defined]',
        '66: error: "Thing" has no attribute "extra"  [attr-defined]',
        '69: error: "Thing" has no attribute "missing"  [attr-defined]',
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


GUARDED_BASE = {
    "models.py": """
    from odoo import fields, models

    class Thing(models.Model):
        _name = "t.thing"

        name = fields.Char()
    """,
}
GUARDED_EXTRA = {
    "depends": ["t_base"],
    "models.py": """
    from odoo import fields, models

    class Thing(models.Model):
        _inherit = "t.thing"

        extra_field = fields.Char()

        def extra_method(self) -> None:
            pass
    """,
}
GUARDED_USE = """
from odoo import models

class Thing(models.Model):
    _inherit = "t.thing"

    def named_in_an_if(self) -> None:
        if self.env["ir.module.module"]._get("t_extra").state == "installed":
            self.env["t.thing"].extra_method()

    def ended_before(self) -> None:
        if self.env["ir.module.module"]._get("t_extra").state != "installed":
            return
        self.env["t.thing"].extra_method()

    def by_a_name(self) -> None:
        module = self.env["ir.module.module"].search([("name", "=", "t_extra")])
        if module:
            self.env["t.thing"].extra_method()

    def by_hasattr(self) -> None:
        thing = self.env["t.thing"]
        if hasattr(thing, "extra_field"):
            thing.extra_method()

    def not_guarded(self) -> None:
        self.env["t.thing"].extra_method()
"""
GUARDED_TEST = """
from odoo.tests import TransactionCase

class TestThing(TransactionCase):
    def test_skipped(self) -> None:
        if self.env["ir.module.module"]._get("t_extra").state != "installed":
            self.skipTest("t_extra is not installed")
        self.env["t.thing"].extra_method()

    def test_of_an_addon_off_the_path(self) -> None:
        if "enterprise_only" not in self.env["ir.module.module"]._installed():
            self.skipTest("enterprise_only is not installed")
        self.env["t.thing"].anything_it_adds()

    def test_not_about_modules(self) -> None:
        if self.env["t.thing"].name == "t_extra":
            self.env["t.thing"].extra_method()


class TestFlagged(TransactionCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.extra_installed = "t_extra" in cls.env["ir.module.module"]._installed()
        if cls.extra_installed:
            cls.env["t.thing"].extra_method()

    def test_flagged(self) -> None:
        if self.extra_installed:
            self.env["t.thing"].extra_method()
"""


def test_records_on_lines_of_an_addon_outside_the_dependencies_are_any(
    run_mypy: RunMypy,
) -> None:
    """Records of a model that an addon outside the dependencies extends, where it runs."""
    result = run_mypy(
        {
            "t_base": GUARDED_BASE,
            "t_extra": GUARDED_EXTRA,
            "t_use": {
                "depends": ["t_base"],
                "models.py": GUARDED_USE,
                "tests/__init__.py": "from . import test_thing\n",
                "tests/test_thing.py": GUARDED_TEST,
            },
        },
        check=["t_use"],
    )

    assert result.messages("t_use/models.py") == [
        '27: error: "Thing" has no attribute "extra_method"  [attr-defined]',
    ]
    # The test of a name is no test about modules.
    assert result.messages("tests/test_thing.py") == [
        '17: error: "Thing" has no attribute "extra_method"  [attr-defined]',
    ]


ENSURED_TEAM = {
    "depends": ["t_base"],
    "models.py": """
    from odoo import fields, models

    class Team(models.Model):
        _name = "t.team"

        name = fields.Char()
    """,
    "data": {"data.xml": '<odoo><record id="team_one" model="t.team"/></odoo>'},
}
ENSURED_SALE = {
    "depends": ["t_team"],
    "models.py": """
    from odoo import fields, models

    class Thing(models.Model):
        _inherit = "t.thing"

        sale_field = fields.Char()

    class Order(models.Model):
        _name = "t.sale.order"

        name = fields.Char()

    class Twice(models.Model):
        _name = "t.twice"
    """,
    "data": {"data.xml": '<odoo><record id="order_one" model="t.sale.order"/></odoo>'},
}
ENSURED_TEST = """
from odoo.tests import TransactionCase

class TestThing(TransactionCase):
    def ensure_installed(self, module_name: str) -> None:
        pass

    def test_ensured(self) -> None:
        self.ensure_installed("t_sale")
        self.env["t.team"].search([])
        self.env.ref("t_team.team_one")
        self.env["t.thing"].create({"sale_field": "x"})

    def test_not_ensured(self) -> None:
        self.env["t.team"].search([])
        self.env.ref("t_team.team_one")
        self.env["t.thing"].create({"sale_field": "x"})
"""


def test_lines_after_ensure_installed_run_with_the_addon_and_its_dependencies(
    run_mypy: RunMypy,
) -> None:
    """``self.ensure_installed("t_sale")``: what ``t_sale`` sees (``t_team``'s too) is there."""
    result = run_mypy(
        {
            "t_base": GUARDED_BASE,
            "t_team": ENSURED_TEAM,
            "t_sale": ENSURED_SALE,
            "t_use": {
                "depends": ["t_base"],
                "tests/__init__.py": "from . import test_thing\n",
                "tests/test_thing.py": ENSURED_TEST,
            },
        },
        check=["t_use"],
    )

    messages = result.messages("tests/test_thing.py")
    assert [(line.split(":")[0], line.rpartition("[")[2]) for line in messages] == [
        ("15", "odoo-model-depends]"),
        ("16", "odoo-xmlid-depends]"),
        ("17", "odoo-vals-key]"),
    ], messages


TWICE = {
    "depends": ["t_base"],
    "models.py": """
    from odoo import models

    class Twice(models.Model):
        _name = "t.twice"
    """,
}
MODEL_GUARDED = """
from odoo import fields, models

class Thing(models.Model):
    _inherit = "t.thing"

    def use(self) -> None:
        if "t.sale.order" in self.env:
            self.env["t.team"].search([])
            self.env.ref("t_team.team_one")
            self.create({"sale_field": "x"})
        if "t.sale.order" not in self.env:
            return
        self.create({"sale_field": "x"})

    def unguarded(self) -> None:
        self.create({"sale_field": "x"})
        if "t.team" in self.env:
            self.create({"sale_field": "x"})
        if "t.twice" in self.env:
            self.create({"sale_field": "x"})

    guarded_ref = fields.Integer(
        default=lambda self: self.env.ref("t_team.team_one").id if "t.sale.order" in self.env else 0
    )
    unguarded_ref = fields.Integer(default=lambda self: self.env.ref("t_team.team_one").id)
"""
REF_GUARDED = """
from odoo.tests import TransactionCase

class TestThing(TransactionCase):
    def test_ref(self) -> None:
        if self.env.ref("t_sale.order_one", raise_if_not_found=False):
            self.env.ref("t_team.team_one")
            self.env["t.thing"].create({"sale_field": "x"})
        self.env["t.thing"].create({"sale_field": "x"})
"""


def test_a_guard_of_a_model_one_addon_defines_runs_with_that_addon(run_mypy: RunMypy) -> None:
    """``if "t.sale.order" in self.env:``: only ``t_sale`` defines it, so it is installed,
    with what it and its dependencies (``t_team``) have; ``env.ref("t_sale.x",
    raise_if_not_found=False)`` in a test too. Not a model that ``t_team`` defines (it
    adds no ``sale_field``), nor one two addons define (which one is installed is not
    known), nor outside the guard."""
    result = run_mypy(
        {
            "t_base": GUARDED_BASE,
            "t_team": ENSURED_TEAM,
            "t_sale": ENSURED_SALE,
            "t_twice": TWICE,
            "t_use": {
                "depends": ["t_base"],
                "models.py": MODEL_GUARDED,
                "tests/__init__.py": "from . import test_thing\n",
                "tests/test_thing.py": REF_GUARDED,
            },
        },
        check=["t_use"],
    )

    messages = result.messages("t_use/models.py")
    assert [(line.split(":")[0], line.rpartition("[")[2]) for line in messages] == [
        ("17", "odoo-vals-key]"),
        ("19", "odoo-vals-key]"),
        ("21", "odoo-vals-key]"),
        ("26", "odoo-xmlid-depends]"),
    ], messages
    messages = result.messages("tests/test_thing.py")
    assert [(line.split(":")[0], line.rpartition("[")[2]) for line in messages] == [
        ("9", "odoo-vals-key]"),
    ], messages


AUTO_PAYMENT = {
    "depends": ["t_base"],
    "models.py": """
    from odoo import fields, models

    class Provider(models.Model):
        _name = "t.provider"

        name = fields.Char()
    """,
    "data": {"data.xml": '<odoo><record id="provider_one" model="t.provider"/></odoo>'},
}
AUTO_BRIDGE = {
    "depends": ["t_base", "t_payment"],
    "auto_install": ["t_base"],
    "models.py": """
    from odoo import fields, models

    class Thing(models.Model):
        _inherit = "t.thing"

        provider_id = fields.Many2one("t.provider")
    """,
}
AUTO_USE = """
from odoo import models

class Thing(models.Model):
    _inherit = "t.thing"

    def use(self) -> None:
        self.env["t.provider"].search([])
        self.env.ref("t_payment.provider_one")
        self.create({"provider_id": 1, "providr_id": 1})
        self.env["t.team"].search([])
"""


def test_addons_odoo_installs_with_the_closure_are_there(run_mypy: RunMypy) -> None:
    """``auto_install: ["t_base"]``: Odoo installs ``t_bridge`` (and ``t_payment``) with it."""
    result = run_mypy(
        {
            "t_base": GUARDED_BASE,
            "t_payment": AUTO_PAYMENT,
            "t_bridge": AUTO_BRIDGE,
            "t_team": ENSURED_TEAM,
            "t_use": {"depends": ["t_base"], "models.py": AUTO_USE},
        },
        check=["t_use"],
    )

    messages = result.messages("t_use/models.py")
    assert [(line.split(":")[0], line.rpartition("[")[2]) for line in messages] == [
        ("10", "odoo-vals-key]"),
        ("11", "odoo-model-depends]"),
    ], messages


VIEWS_KEPT = """
from odoo import models

class Thing(models.Model):
    _inherit = "t.thing"

    def views(self) -> None:
        field = self._fields.get("extra")
        if field:
            thread = self.sudo()
            thread.extra
            thread._compute_display_name()
            self.sudo().extra
        other = self.browse()
        if field:
            other = self.sudo()
            other.extra
        other.extra
        if field:
            later = self.sudo()
        later.extra
        if field:
            for each in self.sudo():
                each.extra
        each.extra
        if field:
            self.cached = self.sudo()

    def through(self) -> None:
        field = self._fields.get("extra")
        alias = self.browse()
        if field:
            thread = self.sudo()
            alias = thread.filtered("name")
        alias.extra
        kept = []
        if field:
            kept.append(self.sudo())
        for item in kept:
            item.extra
        last = self.browse()
        if field:
            [(last := record) for record in self.sudo()]
        last.extra
        joined = self.browse()
        if field:
            thread = self.sudo()
            joined = thread.union(self)
        joined.extra

    def denied(self) -> None:
        if "extra" not in self:
            y = "é"; self.extra
            y = "é"; self.other
        if "extra" not in self:
            z = "é"; print(self.extra, self.extra)
            z = "é"; print(self.other, self.other)
"""


def test_a_view_is_given_the_field_only_where_its_value_goes_nowhere(
    run_mypy: RunMypy, odoo_version: int
) -> None:
    """A field given to ``self.sudo()`` goes with the value: the view, or the name it is
    bound to, may only be read through an attribute, on the lines the guard covers."""
    result = run_mypy(
        {"t_base": T_BASE, "t_use": {"depends": ["t_base"], "models.py": VIEWS_KEPT}},
        check=["t_use"],
        flags=["--show-column-numbers"],
    )

    messages = [message.split(": error: ")[0] for message in result.messages("t_use/models.py")]
    lines = [message.split(":")[0] for message in messages]
    # After the block (18, 21, 25). Inside it too where the variable is read after it
    # (17, 24): the type cannot hold the field in one place and not in the other. An
    # alias, a list, a walrus and a method declared ``-> Self`` (``union``) carry no
    # field out of it (35, 40, 44, 49). Odoo 17 leaves ``union`` without annotations:
    # it gives ``Any``, so ``joined`` is ``Any`` there.
    union = ["49"] if odoo_version >= 18 else []
    assert lines[: 8 + len(union)] == ["17", "18", "21", "24", "25", "35", "40", "44", *union]
    # mypy's columns, also after a character of more than one byte: the read reported
    # here (53; 56, the first of two) where mypy reports its own error on such a line
    assert lines[8 + len(union) :] == ["53", "54", "56", "57"]
    columns = [message.split(":")[1] for message in messages[8 + len(union) :]]
    assert columns[0] == columns[1]
    assert columns[2] == columns[3]


SKIPPED_TESTS = """
from odoo.tests import TransactionCase

class TestThing(TransactionCase):
    def test_has_it(self) -> None:
        thing = self.env["t.thing"]
        if "extra" in thing:
            self.skipTest("extra is there")
        thing.extra

    def test_lacks_it(self) -> None:
        thing = self.env["t.thing"]
        if "extra" not in thing:
            self.skipTest("extra is missing")
        thing.extra

    def test_returns(self, helper: "Helper") -> None:
        thing = self.env["t.thing"]
        if "extra" in thing:
            helper.skipTest("extra is there")
        thing.extra

    def test_unannotated(self):
        thing = self.env["t.thing"]
        if "extra" in thing:
            self.skipTest("extra is there")
        thing.extra

class Helper:
    def skipTest(self, reason: str) -> None:
        print(reason)
"""

SKIPPED_KEPT = """
from odoo.tests.common import TransactionCase

class Helper:
    def skipTest(self, reason: str) -> None:
        print(reason)

class TestThing(TransactionCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.thing = cls.env["t.thing"].create({"name": "x"})

    def test_has_it(self) -> None:
        if "extra" in self.thing:
            self.skipTest("extra is there")
        self.thing.extra

    def test_lacks_it(self) -> None:
        if "extra" not in self.thing:
            self.skipTest("extra is missing")
        self.thing.extra

    def test_returns(self, helper: Helper, count: int) -> None:
        if "extra" in self.thing:
            helper.skipTest("extra is there {}".format(count))
        self.thing.extra

    def test_unknown(self, helper, flag: bool) -> None:
        if "extra" in self.thing:
            helper.skipTest("extra is there")
        self.thing.extra

    @classmethod
    def skip_class(cls) -> None:
        if "extra" in cls.thing:
            cls.skipTest(cls, "extra is there")
        cls.thing.extra

    def test_first(self, helper: Helper, flag: bool) -> None:
        if "extra" in self.thing:
            helper.skipTest("x" if flag else self.skipTest("y"))
        self.thing.extra

    def test_two_statements(self, helper: Helper, flag: bool) -> None:
        if "extra" in self.thing:
            flag and self.skipTest("a"); helper.skipTest("b")
        self.thing.extra

    @classmethod
    def prepare(cls) -> None:
        print(cls)

    @classmethod
    def prepared(cls) -> None:
        if "extra" not in cls.thing:
            cls.prepare()
            cls.thing.extra

    @classmethod
    def skip_unannotated(cls):
        if "extra" in cls.thing:
            cls.skipTest(cls, "extra is there")
        cls.thing.extra

def untyped(function):
    return function

class TestOther(TestThing):
    @classmethod
    @untyped
    def setUpClass(cls):
        super().setUpClass()
        if "extra" in cls.thing:
            cls.skipTest(cls, "extra is there")
        cls.thing.extra
"""


def test_a_read_past_a_test_skipped_when_the_field_is_there_is_reported(
    run_mypy: RunMypy,
) -> None:
    """``self.skipTest(...)`` ends the branch as ``return`` does: past ``if "f" in
    records: self.skipTest(...)`` the records have no field ``f`` (9, and 27 in a method
    without annotations, as Odoo writes tests); past ``not in`` they do.

    A method of that name declared to return ends nothing: past it the records may not
    have the field, and mypy reports the read as it does for any field the model lacks
    (21; also before this rule).
    """
    result = run_mypy(
        {"t_base": T_BASE, "t_use": {"depends": ["t_base"], "models.py": SKIPPED_TESTS}},
        check=["t_use"],
    )

    assert [message.split(":")[0] for message in result.messages("t_use/models.py")] == [
        "9",
        "21",
        "27",
    ]


def test_records_a_test_case_keeps_lack_the_field_past_a_skip_where_it_is_there(
    run_mypy: RunMypy,
) -> None:
    """Records a test case keeps take the field on every line after the question, so
    mypy reports none of these reads by itself: the ones past ``self.skipTest(...)`` (17)
    and ``cls.skipTest(cls, ...)`` (38, as Odoo's ``setUpClass`` writes it) are the
    plugin's. Past a ``skipTest`` declared to return, with a call on its line whose
    callee is not known (``str.format`` of a literal), nothing is (27); nor past one of
    a receiver not known (32), one whose argument skips (43: the statement's call
    decides), or one sharing its line with another statement (48). ``cls.prepare()``,
    declared to return, does not stop the reads where the answer is no (58). A class
    method without annotations, as Odoo writes ``setUpClass``, knows ``cls`` too (64), also
    under a decorator without annotations (76).
    """
    result = run_mypy(
        {
            "t_base": T_BASE,
            "t_extra": T_EXTRA,
            "t_use": {
                "depends": ["t_base"],
                "tests/__init__.py": "from . import test_thing\n",
                "tests/test_thing.py": SKIPPED_KEPT,
            },
        },
        check=["t_use"],
    )

    messages = result.messages("t_use/tests/test_thing.py")
    lines = [message.split(": error: ")[0] for message in messages]
    assert lines == ["17", "37", "38", "58", "63", "64", "75", "76"]
    # mypy's own, of ``cls.skipTest(cls, ...)``
    assert all(messages[at].endswith("[arg-type]") for at in (1, 4, 6))
    missing = '"Thing" has no attribute "extra"  [attr-defined]'
    assert [messages[at].split(": error: ")[1] for at in (0, 2, 3, 5, 7)] == [missing] * 5


INSIDE_OTHER_TYPES = """
from typing import NoReturn, Self

from odoo import models

class Thing(models.Model):
    _inherit = "t.thing"

    def by_name(self) -> dict[str, Self]:
        return {}

    def maybe(self) -> Self | None:
        return self or None

    def stop(self) -> NoReturn:
        raise ValueError(self)

    def held(self) -> None:
        field = self._fields.get("extra")
        if field:
            groups = self.sudo().by_name()
            found = self.sudo().maybe()
        for records in groups.values():
            records.extra
        if found:
            found.extra

    def grouped_out(self) -> None:
        field = self._fields.get("extra")
        if field:
            groups = self.sudo().grouped("name")
        for records in groups.values():
            records.extra

    def ended(self) -> None:
        if "extra" not in self:
            self.stop()
            self.extra
        if "extra" not in self:
            print(self)
            self.extra
"""


def test_a_view_passed_on_inside_another_type_takes_no_field_out(
    run_mypy: RunMypy, odoo_version: int
) -> None:
    """``-> dict[str, Self]``, ``-> Self | None``, Odoo 19's ``grouped() -> dict[Any, Self]``.

    And the reads after a call declared ``NoReturn`` are not reached: none is reported.
    """
    result = run_mypy(
        {"t_base": T_BASE, "t_use": {"depends": ["t_base"], "models.py": INSIDE_OTHER_TYPES}},
        check=["t_use"],
    )

    lines = [message.split(":")[0] for message in result.messages("t_use/models.py")]
    grouped = ["33"] if odoo_version >= 19 else []
    assert lines == ["24", "26", *grouped, "41"]
