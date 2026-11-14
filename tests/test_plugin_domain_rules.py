"""End-to-end tests of the domain term rules of each Odoo version (roadmap stage 8, 3.7).

Each rule gives an error where Odoo raises or silently does something else, and none on
the nearest correct term (the terms without a mark); the rules that only Odoo's SQL
search enforces (``odoo-domain-search``) give none in ``filtered_domain()``.
"""

from __future__ import annotations

from conftest import RunMypy

RULES = """\
from odoo import fields, models


class Order(models.Model):
    _name = "t.order"

    name = fields.Char()
    color = fields.Integer()
    amount = fields.Float()
    active = fields.Boolean()
    date = fields.Date()
    partner_id = fields.Many2one("res.partner")
    line_ids = fields.One2many("t.line", "order_id")
    props = fields.Properties(definition="partner_id.name")
    datas = fields.Binary()
    raw = fields.Binary(attachment=False)
    total = fields.Float(compute="_compute_total")
    shown = fields.Float(compute="_compute_total", search="_search_shown")
    partner_name = fields.Char(related="partner_id.name")
    total_rel = fields.Float(related="line_ids.order_id.total")

    def _compute_total(self) -> None:
        pass

    def _search_shown(self, operator, value):
        return []

    def check(self, partner: "odoo.model.res_partner") -> None:
        self.search([
            ("name", "IN", ["a"]),  # O1
            ("name", "in", ["a"]),
            ("name", "<>", "a"),  # O1-20
            ("date.month_number", "=", 3),  # P1-17
            ("date.month", "=", 3),  # P1-month
            ("date.tz", "=", 1),  # P1-tz
            ("date.day_of_week", "=?", 3),  # P2
            ("props.color", "=", "red"),
            ("props.color.x", "=", 1),  # P3-path
            ("props.c", "child_of", 1),  # P3-operator
            ("datas.size", ">", 10),  # P4
            ("datas", "=", False),
            ("datas", "=", "x"),  # F2-equal
            ("datas", "like", "x"),  # F2-like
            ("raw", "like", "x"),  # F2-raw
            ("datas", "in", [False]),
            ("datas", "in", ["x"]),  # F2-in
            ("name", "any", [("a", "=", 1)]),  # T1
            ("id", "any", [("name", "=", 1)]),  # T1-id
            ("name", "child_of", 1),  # T2
            ("id", "child_of", 1),
            ("partner_id", "child_of", 1),
            ("line_ids", "<", 5),  # T3
            ("partner_id", "<", 5),
            ("partner_id", "<", "M"),  # T4
            ("active", "<", True),  # T5
            ("active", "like", "x"),  # T5-like
            ("active", "like", ""),
            ("active", "=", True),
            ("name", "=like", 5),  # T6
            ("name", "=like", "x%"),
            ("name", "=like", ""),
            ("color", "=", "red"),  # V1
            ("color", "=", "5"),
            ("color", "in", ["x", "1"]),  # V1-in
            ("amount", "=", "1.5"),
            ("color", "in", 5),  # V2
            ("name", "in", "abc"),  # V2-string
            ("color", "in", [1]),
            ("active", "in", True),
            ("partner_id", "in", 5),
            ("line_ids", "in", 5),
            ("id", "in", 5),  # V2-id
            ("partner_id", "=", partner),  # V3
            ("partner_id", "=", partner.id),
            ("line_ids", "in", partner),
            ("id", "in", partner),  # V3-id
            ("date", "=", 5),  # V4
            ("date", "=", "2024-01-01"),
            ("date", "like", 5),
            ("total", "=", 1),  # F1
            ("shown", "=", 1),
            ("partner_name", "=", "x"),
            ("total_rel", "=", 1),  # F1-related
            ("partner_id", "access", "read"),  # O2
            ("name", "access", "read"),  # O2-field
            ("id", "access", "eat"),  # O2-value
            ("line_ids", "any!", [("label", "=", 1)]),  # O3
            ("line_ids", "any", [("label", "=", 1)]),
            ("display_name", "ilike", "x"),  # display_name
        ])
        self.filtered_domain([
            ("total", "=", 1),
            ("color", "=", "red"),
            ("line_ids", "<", 5),
            ("partner_id", "<", "M"),  # filtered-T4
            ("id", "any", [("name", "=", 1)]),
            ("name", "in", "abc"),
            ("datas", "like", "x"),
            ("partner_id", "=", partner),
        ])


class Line(models.Model):
    _name = "t.line"

    order_id = fields.Many2one("t.order")
    label = fields.Text()
"""

ALL = (17, 18, 19, 20)
OLD = (17, 18)  # filtered_domain runs Python code of its own; ``in`` takes only lists
NEW = (19, 20)  # filtered_domain runs the optimized domain
SEARCH = "[odoo-domain-search]"
DOMAIN = "[odoo-domain]"
BINARY = 'Binary field "{}" takes only existence checks in a search ("=" / "!=" with False)'
UNSEARCHABLE = 'Field "{}" cannot be searched (not stored, no search method): {{consequence}}'
DATE_17 = 'Field "date" in domain is not relational (Odoo 17 domains have no date parts)'
T4_OLD = 'Operator "<" with a string on the Many2one field "partner_id": Odoo {v} searches the '
T4_NEW = 'Operator "<" with a string on the Many2one field "partner_id": Odoo {v} raises'

# (versions, mark of the line, message without the model "t.order", code)
EXPECTED: list[tuple[tuple[int, ...], str, str, str]] = [
    ((17, 18, 20), "O1", 'Unknown domain operator "IN"; did you mean "in"?', DOMAIN),
    ((20,), "O1-20", 'Unknown domain operator "<>"; did you mean "!="?', DOMAIN),
    ((17,), "P1-17", DATE_17, DOMAIN),
    ((17,), "P1-month", DATE_17, DOMAIN),
    ((18, 19), "P1-month", '"month" of date field "date" in domain is not a date part', DOMAIN),
    ((17,), "P1-tz", DATE_17, DOMAIN),
    ((18,), "P1-tz", '"tz" of date field "date" in domain is not a date part', DOMAIN),
    ((17,), "P2", DATE_17, DOMAIN),
    (
        (18,),
        "P2",
        'Operator "=?" cannot compare a date part ("date.day_of_week") in Odoo 18',
        DOMAIN,
    ),
    (OLD, "P3-path", 'Property path "props.color.x" in domain must be "props.<property>"', DOMAIN),
    (ALL, "P3-operator", 'Operator "child_of" cannot compare a property ("props.c")', DOMAIN),
    ((17, 18, 19), "P4", 'Field "datas" in domain is not relational', DOMAIN),
    (ALL, "F2-equal", BINARY.format("datas"), SEARCH),
    (ALL, "F2-like", BINARY.format("datas"), SEARCH),
    ((20,), "F2-raw", BINARY.format("raw"), SEARCH),
    (ALL, "F2-in", BINARY.format("datas"), SEARCH),
    (ALL, "T1", 'Operator "any" needs a relational field, not the Char field "name"', DOMAIN),
    (
        OLD,
        "T1-id",
        'Operator "any" on "id" fails in an Odoo {v} search (it takes a relational field)',
        SEARCH,
    ),
    (ALL, "T2", 'Operator "child_of" needs a relational field, not the Char field "name"', DOMAIN),
    (ALL, "T3", 'Operator "<" cannot compare the One2many field "line_ids" in a search', SEARCH),
    (OLD, "T4", T4_OLD + "names instead", SEARCH),
    (NEW, "T4", T4_NEW, DOMAIN),
    (NEW, "T5", 'Operator "<" cannot compare the Boolean field "active"', DOMAIN),
    (NEW, "T5-like", 'Operator "like" cannot compare the Boolean field "active"', DOMAIN),
    (NEW, "T6", 'Operator "=like" needs a string pattern, not 5', DOMAIN),
    (
        ALL,
        "V1",
        'Value "red" of the Integer field "color" is not a number: the search raises',
        SEARCH,
    ),
    (
        ALL,
        "V1-in",
        'Value "x" of the Integer field "color" is not a number: the search raises',
        SEARCH,
    ),
    (OLD, "V2", 'Operator "in" needs a list, not 5 (field "color"): Odoo {v} raises', SEARCH),
    (
        OLD,
        "V2-string",
        'Operator "in" needs a list, not "abc" (field "name"): Odoo {v} raises',
        SEARCH,
    ),
    (OLD, "V2-id", 'Operator "in" needs a list, not 5 (field "id"): Odoo {v} raises', SEARCH),
    (
        OLD,
        "V3",
        'Records as the value of field "partner_id": Odoo {v} searches take ids (.id, .ids)',
        SEARCH,
    ),
    (
        OLD,
        "V3-id",
        'Records as the value of field "id": Odoo {v} searches take ids (.id, .ids)',
        SEARCH,
    ),
    (NEW, "V4", 'Value 5 of the Date field "date" is not a date', DOMAIN),
    (ALL, "F1", UNSEARCHABLE.format("total"), SEARCH),
    (ALL, "F1-related", UNSEARCHABLE.format("total_rel"), SEARCH),
    ((17, 18, 19), "O2", 'Unknown domain operator "access"', DOMAIN),
    ((17, 18, 19), "O2-field", 'Unknown domain operator "access"', DOMAIN),
    ((17, 18, 19), "O2-value", 'Unknown domain operator "access"', DOMAIN),
    (
        (20,),
        "O2-field",
        'Operator "access" works only on Many2one fields and "id", not on the Char field "name"',
        DOMAIN,
    ),
    (
        (20,),
        "O2-value",
        'Operator "access" takes one of "read", "write", "create", "unlink", not "eat"',
        DOMAIN,
    ),
    (OLD, "O3", 'Unknown domain operator "any!"; did you mean "any"?', DOMAIN),
    (
        NEW,
        "O3",
        'Operator "any!" is not allowed in a list domain (Odoo {v} raises); use '
        'Domain("line_ids", "any!", ...)',
        DOMAIN,
    ),
    ((17,), "display_name", UNSEARCHABLE.format("display_name"), SEARCH),
    (NEW, "filtered-T4", T4_NEW, DOMAIN),  # filtered_domain(): only the rules that fail there
]


def test_term_rules_of_each_version(run_mypy: RunMypy, odoo_version: int) -> None:
    result = run_mypy({"t_rules": {"models.py": RULES}})
    lines = {
        line.rpartition("# ")[2]: number
        for number, line in enumerate(RULES.splitlines(), start=1)
        if "  # " in line
    }
    consequence = (
        f"Odoo {odoo_version} ignores the condition and the search matches every record"
        if odoo_version < 19
        else f"Odoo {odoo_version} raises"
    )
    expected = sorted(
        {
            f"{lines[mark]}: error: "
            f"{message.format(v=odoo_version, consequence=consequence)}  {code}"
            for versions, mark, message, code in EXPECTED
            if odoo_version in versions
        }
    )
    found = sorted(
        line.replace(' of Odoo model "t.order"', "")
        for line in result.messages("t_rules/models.py")
    )

    assert found == expected
