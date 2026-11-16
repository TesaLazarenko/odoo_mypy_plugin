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


def test_search_domains_checked(run_mypy: RunMypy, odoo_version: int) -> None:
    expected = [
        '9: error: Odoo model "t.order" has no field "nmae" in domain; did you mean "name"?  '
        "[odoo-domain]",
        '9: error: Domain operator "&" needs 2 terms, has 1  [odoo-domain]',
        '10: error: Field "name" of Odoo model "t.order" in domain is not relational  '
        "[odoo-domain]",
        '11: error: "year_numbr" of date field "date" of Odoo model "t.order" in domain is not '
        'a date part; did you mean "year_number"?  [odoo-domain]',
        '12: error: Value "cancel" is not in the selection of field "state" of Odoo model '
        '"t.order" ("draft", "done")  [odoo-domain]',
        '12: error: Value "dun" is not in the selection of field "state" of Odoo model '
        '"t.order" ("draft", "done")  [odoo-domain]',
        '13: error: Unknown domain operator "equals"  [odoo-domain]',
        '13: error: Unknown domain operator "IN"; did you mean "in"?  [odoo-domain]',
        '14: error: Odoo model "t.line" has no field "labl" in domain; did you mean "label"?  '
        "[odoo-domain]",
        '16: error: Unknown domain operator "^" (not "&", "|", "!")  [odoo-domain]',
        '17: error: Odoo model "t.order" has no field "stat" in domain; did you mean "state"?  '
        "[odoo-domain]",
    ]
    if odoo_version == 19:  # Odoo 19 lowers the operator (with a warning)
        expected.remove(
            '13: error: Unknown domain operator "IN"; did you mean "in"?  [odoo-domain]'
        )
    if odoo_version == 17:  # no date parts in Odoo 17 domains
        date = (
            'error: Field "date" of Odoo model "t.order" in domain is not relational (Odoo 17 '
            "domains have no date parts)  [odoo-domain]"
        )
        expected[3] = f"11: {date}"
        expected.insert(3, f"10: {date}")

    assert _messages(run_mypy, SEARCH) == expected


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


OTHER_SEARCH = """
from odoo import models

class Index:
    def search(self, items: list[object]) -> None:
        pass

    def name_search(self, args: list[object]) -> None:
        pass

class Order(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        Index().search(["a", None, ("x",)])
        Index().name_search(args=["|"])
"""


def test_search_methods_of_other_classes_are_not_domains(run_mypy: RunMypy) -> None:
    assert _messages(run_mypy, OTHER_SEARCH) == []


STRUCTURE = """
from odoo import models

class Mixin(models.AbstractModel):
    _inherit = "t.mixin"

    def check(self, items: list[tuple[str, str, int]], info: dict[str, int]) -> None:
        self.search([("name", "=")])
        self.search([(2, "=", 1), (1, "=", 1), (0, "=", 1), ("anything", "=", 1)])
        self.search(["!", "|", ("a", "=", 1)])
        self.search([items, info, 5, *items])
        self.search(["|"] + [("a", "=", 1), ("b", "=", 1)])
"""


def test_structure_is_checked_on_any_model(run_mypy: RunMypy) -> None:
    # A mixin's domains name fields of the models that use it: only their structure.
    assert _messages(run_mypy, STRUCTURE) == [
        "8: error: Domain term must be (field, operator, value), not 2 items  [odoo-domain]",
        "9: error: Domain term must start with a field name, not 2  [odoo-domain]",
        '10: error: Domain operator "|" needs 2 terms, has 1  [odoo-domain]',
        '11: error: Domain item of type "list[tuple[str, str, int]]" is not a term or an '
        "operator  [odoo-domain]",
        '11: error: Domain item of type "dict[str, int]" is not a term or an operator  '
        "[odoo-domain]",
        '11: error: Domain item of type "int" is not a term or an operator  [odoo-domain]',
    ]


GROWING_DOMAINS = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self, ids: list[int], flag: bool) -> None:
        domain = [("name", "=", "x")]
        domain.append(("partner_id", "in", ids))
        domain.insert(0, ("date", "!=", False))
        domain.extend([("state", "=", "draft"), ("line_ids", "in", ids)])
        domain += [("partner_id", "=", 1)]
        domain = domain + [("date", "=", False)]
        if flag:
            domain.insert(0, "|")
        domain.append(("nmae", "=", 1))
        self.search(domain)
        domain.append(5)
        pairs = [("a", "b")]
        pairs.append(("a", 1))
        triples = [(1, "b", "c")]
        triples.append((1, "b", 2))
"""


def test_domains_list_of_terms_takes_terms_with_other_values(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, GROWING_DOMAINS)

    # mypy infers list[tuple[str, str, str]] from the first term: a term with another
    # kind of value is still a term, anything else is not.
    assert [message.split(";")[0] for message in messages] == [
        '16: error: Odoo model "t.order" has no field "nmae" in domain',
        '18: error: Argument 1 to "append" of "list" has incompatible type "int"',
        '20: error: Argument 1 to "append" of "list" has incompatible type "tuple[str, int]"',
        '22: error: Argument 1 to "append" of "list" has incompatible type "tuple[int, str, int]"',
    ]


@pytest.mark.odoo(19, 20)
def test_list_of_commands_takes_plain_command_tuples(run_mypy: RunMypy) -> None:
    """``Command`` is an ``int`` enumeration on Odoo 19+: its tuples are no ``tuple[int, ...]``."""
    messages = _messages(
        run_mypy,
        """
        from odoo import Command, models

        class Order(models.Model):
            _inherit = "t.order"

            def check(self, ids: list[int]) -> None:
                commands = [Command.update(1, {"label": "x"})]
                commands.append((2, 5))
                commands.append((0, 0, {"label": "y"}))
                commands += [(5,), Command.set(ids)]
                commands.append(7)
                self.write({"line_ids": commands})
        """,
    )

    assert len(messages) == 1, messages
    assert messages[0].startswith(
        '12: error: Argument 1 to "append" of "list" has incompatible type "int"'
    )
