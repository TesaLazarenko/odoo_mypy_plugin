"""End-to-end tests of domains (roadmap stage 3.4)."""

from __future__ import annotations

import pytest
from conftest import RunMypy

T_BASE = """
from odoo import fields, models

class Order(models.Model):
    _name = "t.order"

    name = fields.Char()
    state = fields.Selection([("draft", "Draft"), ("done", "Done")])
    date = fields.Date()
    partner_id = fields.Many2one("res.partner")
    line_ids = fields.One2many("t.line", "order_id")
    ref = fields.Reference(selection=[("res.partner", "Partner")])
    props = fields.Properties(definition="partner_id.name")

class Line(models.Model):
    _name = "t.line"

    order_id = fields.Many2one("t.order")
    label = fields.Text()

class Mixin(models.AbstractModel):
    _name = "t.mixin"

    def find(self):
        return self.search([("anything", "=", 1)])
"""


def _messages(run_mypy: RunMypy, source: str) -> list[str]:
    result = run_mypy(
        {
            "t_base": {"models.py": T_BASE},
            "t_use": {"depends": ["t_base"], "models.py": source},
        },
        check=["t_use"],
    )
    # Odoo 19+ types a domain list ``list[str | tuple[str, str, Any]]``: mypy reports
    # ``(1, "=", 1)`` and a variable path itself (``list-item``).
    return [line for line in result.messages("t_use/models.py") if "[list-item]" not in line]


SEARCH = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self, value: int) -> None:
        self.search([("name", "=", "x"), ("partner_id.city", "ilike", "k"), (1, "=", 1)])
        self.search(["|", ("nmae", "=", "x"), "&", ("line_ids.label", "!=", False)])
        self.search_count([("name.size", "=", 1), ("date.month_number", "=", 3)])
        self.env["t.line"].search([("order_id.date.year_numbr", "=", 2024)])
        self.search([("state", "=", "cancel"), ("state", "in", ["draft", "dun"])])
        self.search([("name", "equals", "x"), ("name", "IN", []), ("name", "=?", "x")])
        self.search([("line_ids", "any", [("labl", "=", "x")])])
        self.search([("ref.name", "=", "x"), ("props.color", "=", "red"), ("id", "in", [])])
        self.search([(value, "=", 1), ("name", value, 1), "^"])
        self._read_group(domain=[("stat", "=", "draft")], groupby=["state"])
        self.filtered_domain([("name", "!=", False)])
"""


def test_search_domains_checked(run_mypy: RunMypy) -> None:
    assert _messages(run_mypy, SEARCH) == [
        '9: error: Odoo model "t.order" has no field "nmae" in domain; did you mean "name"?  '
        "[odoo-domain]",
        '10: error: Field "name" of Odoo model "t.order" in domain is not relational  '
        "[odoo-domain]",
        '11: error: "year_numbr" of date field "date" of Odoo model "t.order" in domain is not '
        'a date part; did you mean "year_number"?  [odoo-domain]',
        '12: error: Value "cancel" is not in the selection of field "state" of Odoo model '
        '"t.order" ("draft", "done")  [odoo-domain]',
        '12: error: Value "dun" is not in the selection of field "state" of Odoo model '
        '"t.order" ("draft", "done")  [odoo-domain]',
        '13: error: Unknown domain operator "equals"  [odoo-domain]',
        '14: error: Odoo model "t.line" has no field "labl" in domain; did you mean "label"?  '
        "[odoo-domain]",
        '16: error: Unknown domain operator "^" (not "&", "|", "!")  [odoo-domain]',
        '17: error: Odoo model "t.order" has no field "stat" in domain; did you mean "state"?  '
        "[odoo-domain]",
    ]


FIELD_DOMAINS = """
from odoo import fields, models

class Line(models.Model):
    _inherit = "t.line"

    draft_id = fields.Many2one("t.order", domain=[("state", "=", "draft")])
    wrong_id = fields.Many2one("t.order", domain=[("stat", "=", "draft")])
    text_id = fields.Many2one(
        "t.order", domain="[('partner_id', '=', partner_id), ('nme', '=', parent.name)]"
    )
    parent_id = fields.Many2one("t.order", domain="[('parent.x', '=', 1), ('name', 'eq', 1)]")
    other_ids = fields.One2many("t.line", "order_id", domain=[("lable", "!=", False)])
    partner_id = fields.Many2one("res.partner")
"""


def test_field_domains_checked_against_comodel(run_mypy: RunMypy) -> None:
    messages = [line.split(";")[0].split("  [")[0] for line in _messages(run_mypy, FIELD_DOMAINS)]

    assert messages == [
        '8: error: Odoo model "t.order" has no field "stat" in domain',
        '10: error: Odoo model "t.order" has no field "nme" in domain',
        '12: error: Unknown domain operator "eq"',
        '13: error: Odoo model "t.line" has no field "lable" in domain',
    ]


def test_abstract_model_domains_not_checked(run_mypy: RunMypy) -> None:
    result = run_mypy({"t_base": {"models.py": T_BASE}}, check=["t_base"])

    assert result.messages("t_base/models.py") == []


DOMAIN_CLASS = """
from odoo import models
from odoo.fields import Domain

class Order(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        self.search(Domain([("nmae", "=", "x")]))
        self.search(Domain("stat", "=", "draft"))
        self.search(Domain("name", "=", "x") & Domain([("name", "!=", False)]))
"""


def test_domain_class_checked(run_mypy: RunMypy, odoo_version: int) -> None:
    if odoo_version < 19:
        pytest.skip("Domain is new in Odoo 19")

    messages = [line.split(";")[0] for line in _messages(run_mypy, DOMAIN_CLASS)]

    assert messages == [
        '9: error: Odoo model "t.order" has no field "nmae" in domain',
        '10: error: Odoo model "t.order" has no field "stat" in domain',
    ]


BUILT_DOMAINS = """
from typing import Any
from odoo import models
from odoo.osv import expression

class Order(models.Model):
    _inherit = "t.order"

    def check(self, extra: list[Any]) -> None:
        domain: list[Any] = [("nmae", "=", "x")]
        domain += [("stat", "=", "draft")]
        domain.append(("dat", "=", False))
        domain.extend(extra)
        self.search(domain)
        self.search([("name", "=", "x")] + [("lable", "=", 1)])
        self.search(expression.AND([[("prtner_id", "=", 1)], domain]))
        domain = [("name", "=", "y")]
        self.search(domain)
        both = expression.OR([domain, [("lines", "=", 1)]])
        self.search_count(both)
        self.search(extra)
"""


def test_domains_built_in_a_variable_checked(run_mypy: RunMypy, odoo_version: int) -> None:
    if odoo_version > 19:
        pytest.skip("odoo.osv.expression is gone in Odoo 20")

    messages = _domain_errors(_messages(run_mypy, BUILT_DOMAINS))

    assert messages == [
        '10: error: Odoo model "t.order" has no field "nmae" in domain',
        '11: error: Odoo model "t.order" has no field "stat" in domain',
        '12: error: Odoo model "t.order" has no field "dat" in domain',
        '15: error: Odoo model "t.order" has no field "lable" in domain',
        '16: error: Odoo model "t.order" has no field "prtner_id" in domain',
        # 18: rebound, the earlier terms are not its terms
        '19: error: Odoo model "t.order" has no field "lines" in domain',
    ]


DOMAIN_OBJECTS = """
from odoo import models
from odoo.fields import Domain

class Order(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        domain = Domain("name", "=", "x")
        domain &= Domain("stat", "=", "draft")
        domain |= Domain([("dat", "=", False)])
        self.search(domain)
        self.search(~Domain("nmae", "=", 1) | Domain.AND([[("lable", "=", 1)]]))
        self.search(Domain.OR([domain, Domain("prtner_id", "=", 1)]))
"""


def test_domain_objects_combined_checked(run_mypy: RunMypy, odoo_version: int) -> None:
    if odoo_version < 19:
        pytest.skip("Domain is new in Odoo 19")

    messages = _domain_errors(_messages(run_mypy, DOMAIN_OBJECTS))

    assert messages == [
        '10: error: Odoo model "t.order" has no field "stat" in domain',
        '11: error: Odoo model "t.order" has no field "dat" in domain',
        '13: error: Odoo model "t.order" has no field "nmae" in domain',
        '13: error: Odoo model "t.order" has no field "lable" in domain',
        '14: error: Odoo model "t.order" has no field "prtner_id" in domain',
    ]


def _domain_errors(messages: list[str]) -> list[str]:
    """The ``odoo-domain`` errors, without the code and the suggestion."""
    return [
        line.split(";")[0].removesuffix("  [odoo-domain]")
        for line in messages
        if "[odoo-domain]" in line
    ]
