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
    """,
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
