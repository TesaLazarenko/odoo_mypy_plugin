"""End-to-end tests of the names in field definitions (roadmap stage 3.1, 3.2)."""

from __future__ import annotations

from conftest import RunMypy

T_BASE = """
from odoo import fields, models

class Order(models.Model):
    _name = "t.order"

    name = fields.Char()
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


RELATED = """
from odoo import fields, models

class Line(models.Model):
    _inherit = "t.line"

    order_name = fields.Char(related="order_id.name")
    country = fields.Char(related="order_id.partner_id.country_id.code")
    city = fields.Char(related="order_id.partner_id.citty")
    deep = fields.Char(related="order_id.name.x")
    by_ref = fields.Char(related="order_id.ref.name")
    later = fields.Char(related="order_id.later_name")
    wrong = fields.Text(related="order_id.name")
    image = fields.Image(related="order_id.partner_id.image_1920")

class Order(models.Model):
    _inherit = "t.order"

    later_name = fields.Char()
"""


def test_related_paths_checked_through_comodels(run_mypy: RunMypy) -> None:
    assert _messages(run_mypy, RELATED) == [
        '9: error: Odoo model "res.partner" has no field "citty" in '
        'related="order_id.partner_id.citty"; did you mean "city"?  [odoo-field-ref]',
        '10: error: Field "name" of Odoo model "t.order" in related="order_id.name.x" is not '
        "relational  [odoo-field-ref]",
        '13: error: Related field is a Text, but "order_id.name" is a Char field of Odoo '
        'model "t.order"  [odoo-field-ref]',
    ]


RELATED_COMODEL = """
from odoo import fields, models

class Line(models.Model):
    _inherit = "t.line"

    partner_id = fields.Many2one(related="order_id.partner_id")
    sibling_ids = fields.One2many(related="order_id.line_ids")

    def check(self) -> None:
        reveal_type(self.partner_id)
        reveal_type(self.sibling_ids.label)
"""


def test_related_relational_field_gets_comodel_of_path(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, RELATED_COMODEL)

    assert messages[0].startswith('11: note: Revealed type is "odoo.addons.base.models.res_partner')
    assert messages[1:] == ['12: note: Revealed type is "str | Literal[False]"']


METHODS = """
from odoo import api, fields, models

def module_function(records):
    return []

class Order(models.Model):
    _inherit = "t.order"

    total = fields.Float(compute="_compute_total", inverse="_inverse_totl", store=True)
    other = fields.Integer(compute="_compute_later", search="_search_other")
    kind = fields.Selection("_selection_kind")
    level = fields.Selection(selection="_selection_levl")
    display = fields.Char(compute="_compute_display_name")
    by_function = fields.Selection(module_function)

    def _compute_total(self) -> None:
        pass

    def _inverse_total(self) -> None:
        pass

    def _search_other(self, operator, value):
        return []

    @api.model
    def _selection_kind(self):
        return []

class Later(models.Model):
    _inherit = "t.order"

    def _compute_later(self) -> None:
        pass
"""


def test_method_names_must_exist_in_view(run_mypy: RunMypy) -> None:
    assert [line.split(";")[0] for line in _messages(run_mypy, METHODS)] == [
        '10: error: Odoo model "t.order" has no method "_inverse_totl" (inverse="_inverse_totl")',
        '13: error: Odoo model "t.order" has no method "_selection_levl" '
        '(selection="_selection_levl")',
    ]


MIXIN = """
from odoo import fields, models

class Mixin(models.AbstractModel):
    _name = "t.mixin"

    partner_name = fields.Char(related="partner_id.name")
    total = fields.Float(compute="_compute_total")
"""


def test_abstract_model_references_not_checked(run_mypy: RunMypy) -> None:
    assert _messages(run_mypy, MIXIN) == []
