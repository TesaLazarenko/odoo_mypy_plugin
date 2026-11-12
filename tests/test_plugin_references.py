"""End-to-end tests of the names in field definitions and decorators (stage 3.1-3.3)."""

from __future__ import annotations

from conftest import RunMypy

from odoo_typing.references import PathResult, decorator_problem, resolve_path
from odoo_typing.scanner import FieldInfo

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

    assert messages[0].startswith('11: note: Revealed type is "odoo.addons.base.')
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


DECORATORS = """
from odoo import api, fields, models

class Line(models.Model):
    _inherit = "t.line"

    total = fields.Float(compute="_compute_total")

    @api.depends("label", "order_id.partner_id.city", "order_id.ref.name", "create_uid.name")
    def _compute_total(self) -> None:
        pass

    @api.depends("order_id.nmae")
    def _compute_a(self) -> None:
        pass

    @api.depends("label.size")
    def _compute_b(self) -> None:
        pass

    @api.constrains("label", "order_id.name")
    def _check_label(self) -> None:
        pass

    @api.onchange("lable", "display_name")
    def _onchange_label(self) -> None:
        reveal_type(self.label)

@api.depends("anything")
def module_function(records):
    return records
"""


def test_decorator_paths_checked(run_mypy: RunMypy) -> None:
    assert _messages(run_mypy, DECORATORS) == [
        '13: error: Odoo model "t.order" has no field "nmae" in '
        '@api.depends("order_id.nmae"); did you mean "name"?  [odoo-field-ref]',
        '17: error: Field "label" of Odoo model "t.line" in @api.depends("label.size") is not '
        "relational  [odoo-field-ref]",
        '21: error: @api.constrains("order_id.name") takes field names, not paths: Odoo '
        "ignores it  [odoo-field-ref]",
        '25: error: Odoo model "t.line" has no field "lable" in @api.onchange("lable"); '
        'did you mean "label"?  [odoo-field-ref]',
        '27: note: Revealed type is "str | Literal[False]"',
    ]


MIXIN_DECORATORS = """
from odoo import api, models

class Mixin(models.AbstractModel):
    _name = "t.mixin"

    @api.depends("partner_id.name")
    def _compute_partner_name(self) -> None:
        pass
"""


def test_abstract_model_decorators_not_checked(run_mypy: RunMypy) -> None:
    assert _messages(run_mypy, MIXIN_DECORATORS) == []


def test_resolve_path_stops_at_properties() -> None:
    fields = {"props": FieldInfo("props", "Properties")}

    assert resolve_path("props.key", "t.order", lambda model: fields, "") == PathResult()
    assert decorator_problem("depends", "props.key", "t.order", lambda model: fields) is None
