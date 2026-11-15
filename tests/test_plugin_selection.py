"""End-to-end tests of selection keys in comparisons, command ids and ``ondelete=``."""

from __future__ import annotations

from conftest import RunMypy

T_BASE = """
from odoo import fields, models

class Order(models.Model):
    _name = "t.order"

    name = fields.Char()
    state = fields.Selection([("draft", "Draft"), ("done", "Done")])
    kind = fields.Selection(selection="_kinds")
    partner_id = fields.Many2one("res.partner")
    line_ids = fields.One2many("t.line", "order_id")
    tag_ids = fields.Many2many("t.tag")

    def _kinds(self):
        return [("a", "A")]

class Line(models.Model):
    _name = "t.line"

    order_id = fields.Many2one("t.order")

class Tag(models.Model):
    _name = "t.tag"

    name = fields.Char()

class Mixin(models.AbstractModel):
    _name = "t.mixin"
"""


T_EXTRA = """
from odoo import fields, models

class Order(models.Model):
    _inherit = "t.order"

    state = fields.Selection(selection_add=[("extra", "Extra"), ("more", "More")])
"""


def _messages(run_mypy: RunMypy, source: str) -> list[str]:
    result = run_mypy(
        {
            "t_base": {"models.py": T_BASE},
            "t_extra": {"depends": ["t_base"], "models.py": T_EXTRA},
            "t_use": {"depends": ["t_base"], "models.py": source},
        },
        check=["t_base", "t_use"],
    )
    assert result.messages("t_base/models.py") == []
    return result.messages("t_use/models.py")


COMPARISONS = """
from odoo import fields, models

class Order(models.Model):
    _inherit = "t.order"

    state = fields.Selection(selection_add=[("sent", "Sent")])

    def check(self, other):
        for order in self:
            if order.state == "draft" or order.state in ("sent", "done"):
                pass
            if order.state == "drft":
                pass
            if "dne" != order.state:
                pass
            if order.state in ("draft", "snt") or order.state not in ["cancel"]:
                pass
            if order.state == other or order.state == "" or order.kind == "whatever":
                pass
            if order.name == "draftt" or order.partner_id.name in ("x", "y"):
                pass
            if order.state == "extra" or order.state in ("draft", "more"):
                pass  # keys of an addon that t_use does not depend on
        return self.state == "don"
"""


def test_selection_keys_in_comparisons(run_mypy: RunMypy) -> None:
    keys = '("draft", "done", "sent")  [odoo-selection]'
    start = "is not in the selection of field"
    assert _messages(run_mypy, COMPARISONS) == [
        f'13: error: Value "drft" {start} "state" of Odoo model "t.order" {keys}',
        f'15: error: Value "dne" {start} "state" of Odoo model "t.order" {keys}',
        f'17: error: Value "snt" {start} "state" of Odoo model "t.order" {keys}',
        f'17: error: Value "cancel" {start} "state" of Odoo model "t.order" {keys}',
        f'25: error: Value "don" {start} "state" of Odoo model "t.order" {keys}',
    ]


COMMANDS = """
from odoo import models
from odoo.fields import Command

class Order(models.Model):
    _inherit = "t.order"

    def link(self, tags, partner, line_id):
        lines = self.env["t.line"]
        self.write({"tag_ids": [Command.link(tags.id), Command.set(tags.ids), (4, line_id)]})
        self.write({"tag_ids": [Command.link(partner.id), (6, 0, lines.ids)]})
        self.write({"line_ids": [(4, lines.id), Command.unlink(self.id), (3, tags.id)]})
        self.write({"line_ids": [(7, 0, 0), (5,), Command.clear()]})
        self.write({"tag_ids": [Command.set(self.env["t.mixin"].ids)]})

    def typed(self):
        tags = self.env["t.tag"]
        partner = self.env["res.partner"]
        self.link(tags, partner, 1)
        self.write({"tag_ids": [Command.link(tags.id), Command.link(partner.id)]})
"""


def test_command_ids_are_of_the_comodel(run_mypy: RunMypy) -> None:
    start = "error: Incompatible records for field"
    # Odoo 17/18 also report ``Command.link(record.id)`` themselves (``int | NewId``).
    messages = [item for item in _messages(run_mypy, COMMANDS) if "odoo-vals-type" in item]
    assert messages == [
        f'11: {start} "tag_ids" of Odoo model "t.order" (command has the ids of "t.line" '
        'records, Many2many field takes "t.tag" records)  [odoo-vals-type]',
        f'12: {start} "line_ids" of Odoo model "t.order" (command has the id of "t.order" '
        'records, One2many field takes "t.line" records)  [odoo-vals-type]',
        '13: error: (7, ...) is not an x2many command of field "line_ids" of Odoo model '
        '"t.order" (commands are 0 to 6)  [odoo-vals-type]',
        f'20: {start} "tag_ids" of Odoo model "t.order" (command has the id of "res.partner" '
        'records, Many2many field takes "t.tag" records)  [odoo-vals-type]',
    ]


ONDELETE = """
from odoo import fields, models

class Line(models.Model):
    _inherit = "t.line"

    a_id = fields.Many2one("t.order", ondelete="cascade", required=True)
    b_id = fields.Many2one("t.order", ondelete="set null")
    c_id = fields.Many2one("t.order", required=True, ondelete="set null")
    d_id = fields.Many2one("t.order", ondelete="casacde")
    state = fields.Selection([("a", "A")], ondelete={"a": "set null"})
"""


def test_ondelete_of_many2one(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, ONDELETE)
    assert [message for message in messages if "odoo-field-ref" in message] == [
        '9: error: A required Many2one field cannot have ondelete="set null": Odoo raises  '
        "[odoo-field-ref]",
        '10: error: ondelete="casacde" is not a policy of a Many2one field; did you mean '
        '"cascade"?  [odoo-field-ref]',
    ]
