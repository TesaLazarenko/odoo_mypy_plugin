"""Lambdas the ORM calls with records: of recordset methods and of field arguments.

Odoo 17 and 18 declare no type for the function of ``filtered`` / ``mapped`` /
``sorted`` / ``grouped`` and take a field's ``default`` / ``domain`` / ``selection`` /
``compute`` as ``**kwargs``: the lambda's parameter was ``Any`` and nothing in its body
was checked. The plugin gives it the records Odoo calls it with.
"""

from __future__ import annotations

import pytest
from conftest import RunMypy

MODELS = """
from odoo import api, fields, models

class Order(models.Model):
    _name = "t.order"

    name = fields.Char(default=lambda self: self.env.company.name)
    state = fields.Selection([("draft", "Draft"), ("done", "Done")])
    line_ids = fields.One2many("t.line", "order_id")


class Line(models.Model):
    _name = "t.line"

    order_id = fields.Many2one("t.order", default=lambda self: self.env["t.order"].search([]))
    qty = fields.Float()
    note = fields.Char(default=lambda self: self.nmae)
    other_id = fields.Many2one("t.order", domain=lambda self: [("id", "!=", self.order_idd.id)])
    kind = fields.Selection(selection=lambda self: [("a", self.order_id.nmae)])
    total = fields.Float(compute=lambda self: self.qtty)
    stamp = fields.Datetime(default=fields.Datetime.now)

    def use(self) -> None:
        reveal_type(self.filtered(lambda line: line.qty > 0))
        self.filtered(lambda line: line.qtty)
        self.filtered(lambda line: line.order_id.state == "dnoe")
        reveal_type(self.mapped(lambda line: line.qty))
        reveal_type(self.mapped(lambda line: line.order_id))
        reveal_type(self.mapped("qty"))
        self.sorted(key=lambda line: line.qyt)
        self.sorted("qty")
        reveal_type(self.grouped(lambda line: line.order_id))
        self.mapped(lambda line: line.order_id.nmae)
        name = "qty"
        self.mapped(name)

    @api.onchange("qty")
    def _onchange_qty(self) -> None:
        for line in self:
            line.id.origin
"""
EXPECTED = [
    '17: error: "Line" has no attribute "nmae"  [attr-defined]',
    '18: error: "Line" has no attribute "order_idd"; maybe "order_id"?  [attr-defined]',
    '19: error: "Order" has no attribute "nmae"  [attr-defined]',
    '20: error: "Line" has no attribute "qtty"; maybe "qty"?  [attr-defined]',
    '24: note: Revealed type is "t_mod.models.Line"',
    '25: error: "Line" has no attribute "qtty"; maybe "qty"?  [attr-defined]',
    '26: error: Value "dnoe" is not in the selection of field "state" of Odoo model "t.order"'
    ' ("draft", "done")  [odoo-selection]',
    '27: note: Revealed type is "list[float]"',
    '28: note: Revealed type is "t_mod.models.Order"',
    '29: note: Revealed type is "list[float]"',
    '30: error: "Line" has no attribute "qyt"  [attr-defined]',
    '32: note: Revealed type is "dict[t_mod.models.Order, t_mod.models.Line]"',
    '33: error: "Order" has no attribute "nmae"  [attr-defined]',
    # ``line.id.origin`` (line 41): the id of a new record, which has ``origin``.
]


def test_lambdas_take_the_records_odoo_calls_them_with(run_mypy: RunMypy) -> None:
    result = run_mypy({"t_mod": {"depends": ["base"], "models.py": MODELS}})

    assert result.messages("t_mod/models.py") == EXPECTED


UNKNOWN = """
from odoo import models

class Thing(models.Model):
    _name = "t.thing"

    def use(self) -> None:
        if "iot.device" in self.env:
            printers = self.env["iot.device"].search([])
            printers.mapped(lambda printer: printer.name)
"""


@pytest.mark.odoo(17, 18)
def test_records_of_a_model_the_addon_cannot_see_keep_an_untyped_lambda(
    run_mypy: RunMypy,
) -> None:
    result = run_mypy({"t_mod": {"depends": ["base"], "models.py": UNKNOWN}})

    assert result.messages("t_mod/models.py") == []


# A default that reads a field declared further down, or of a class of the same import
# cycle: mypy cannot tell its type in the class body yet. That is no error of the code.
LATER = """
from odoo import fields, models

class Wizard(models.Model):
    _name = "t.wizard"

    new_name = fields.Char(default=lambda self: self.linked_id.name)
    linked_id = fields.Many2one("res.partner")
    wrong = fields.Char(default=lambda self: self.linked_id.nmae)
"""


def test_a_default_reading_a_later_field_is_no_error(run_mypy: RunMypy) -> None:
    result = run_mypy({"t_mod": {"depends": ["base"], "models.py": LATER}})

    # Odoo 17 and 18 name base's partner class ``Partner``, 19+ ``ResPartner``.
    messages = [
        message.replace('"ResPartner"', '"Partner"')
        for message in result.messages("t_mod/models.py")
    ]
    assert messages == ['9: error: "Partner" has no attribute "nmae"  [attr-defined]']


def test_a_changed_file_reads_its_id_origin_again(run_mypy: RunMypy) -> None:
    """The index of ``x.id.origin`` is by position: an edited file is read again."""
    addons = {"t_mod": {"depends": ["base"], "models.py": MODELS}}
    run_mypy(addons, fresh_cache=True)
    changed = MODELS.replace("line.id.origin", "line.id.origin\n            line.id.origin")

    result = run_mypy({"t_mod": {"depends": ["base"], "models.py": changed}}, fresh_cache=True)

    assert result.messages("t_mod/models.py") == EXPECTED
