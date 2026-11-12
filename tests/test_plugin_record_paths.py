"""End-to-end tests of field paths in ``mapped`` / ``filtered`` / ``sorted`` / ``grouped``."""

from __future__ import annotations

from conftest import RunMypy

T_BASE = """
from odoo import fields, models

class Order(models.Model):
    _name = "t.order"

    name = fields.Char()
    amount = fields.Float()
    partner_id = fields.Many2one("res.partner")
    line_ids = fields.One2many("t.line", "order_id")
    ref = fields.Reference(selection=[("res.partner", "Partner")])

class Line(models.Model):
    _name = "t.line"

    order_id = fields.Many2one("t.order")
    label = fields.Text()
"""


def _messages(run_mypy: RunMypy, source: str) -> list[str]:
    result = run_mypy(
        {
            "t_base": {"models.py": T_BASE},
            "t_use": {"depends": ["t_base"], "models.py": source},
        },
        check=["t_use"],
    )
    return result.messages("t_use/models.py")


MAPPED = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        reveal_type(self.mapped("partner_id.name"))
        reveal_type(self.mapped("amount"))
        reveal_type(self.mapped("line_ids.label"))
        reveal_type(self.mapped("line_ids").order_id.name)
        reveal_type(self.grouped("partner_id"))
        self.mapped("partner_id.nmae")
        self.filtered("line_ids.lable")
        self.grouped("partner_id.name")
        self.mapped(lambda order: order.name)
"""


def test_mapped_types_and_paths(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, MAPPED)

    assert messages[:3] == [
        '8: note: Revealed type is "list[str | Literal[False]]"',
        '9: note: Revealed type is "list[float]"',
        '10: note: Revealed type is "list[str | Literal[False]]"',
    ]
    assert messages[3] == '11: note: Revealed type is "str | Literal[False]"'
    assert messages[4].startswith('12: note: Revealed type is "dict[odoo.addons.base')
    assert [line.split(";")[0] for line in messages[5:]] == [
        '13: error: Odoo model "res.partner" has no field "nmae" in mapped("partner_id.nmae")',
        '14: error: Odoo model "t.line" has no field "lable" in filtered("line_ids.lable")',
        '15: error: grouped("partner_id.name") takes a field name  [odoo-field-ref]',
    ]


SORTED = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        reveal_type(self.sorted("name"))
        self.sorted("nmae")
"""


def test_sorted_field_checked(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, SORTED)

    assert messages[0].startswith('8: note: Revealed type is "t_use.models.Order')
    assert messages[1].split(";")[0] == (
        '9: error: Odoo model "t.order" has no field "nmae" in sorted("nmae")'
    )


SORTED_ORDER = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        self.sorted("partner_id.name desc, amount, id")
        self.sorted("name desc, amont")
"""


def test_sorted_order_checked_on_19(run_mypy: RunMypy, odoo_version: int) -> None:
    messages = [line.split(";")[0] for line in _messages(run_mypy, SORTED_ORDER)]

    if odoo_version < 19:
        assert messages == [
            '8: error: sorted("partner_id.name desc, amount, id") takes a field name  '
            "[odoo-field-ref]",
            '9: error: sorted("name desc, amont") takes a field name  [odoo-field-ref]',
        ]
    else:
        assert messages == [
            '9: error: Odoo model "t.order" has no field "amont" in sorted("name desc, amont")'
        ]


CONSTANT_PATHS = """
from typing import Final, Literal
from odoo import models

PARTNER_NAME: Final = "partner_id.name"
TYPO: Final = "partner_id.nmae"
NAME_TYPO: Final = "nmae"

class Order(models.Model):
    _inherit = "t.order"

    def check(self, path: Literal["amount"], text: str) -> None:
        reveal_type(self.mapped(PARTNER_NAME))
        reveal_type(self.mapped(path))
        reveal_type(self.grouped(path))
        self.filtered(TYPO)
        self.sorted(NAME_TYPO)
        self.mapped(text)  # not known before run time: not checked
"""


def test_final_and_literal_paths_checked_like_literals(run_mypy: RunMypy) -> None:
    messages = [line.split(";")[0] for line in _messages(run_mypy, CONSTANT_PATHS)]

    assert messages[:2] == [
        '13: note: Revealed type is "list[str | Literal[False]]"',
        '14: note: Revealed type is "list[float]"',
    ]
    assert messages[2].startswith('15: note: Revealed type is "dict[float, t_use.models.Order')
    assert messages[3:] == [
        '16: error: Odoo model "res.partner" has no field "nmae" in filtered("partner_id.nmae")',
        '17: error: Odoo model "t.order" has no field "nmae" in sorted("nmae")',
    ]
