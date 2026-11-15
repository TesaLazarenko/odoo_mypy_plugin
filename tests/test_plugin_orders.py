"""End-to-end tests of orders, of class attributes and field arguments that name fields."""

from __future__ import annotations

from conftest import RunMypy

from odoo_typing.references import order_fields

T_BASE = """
from odoo import fields, models

class Order(models.Model):
    _name = "t.order"

    name = fields.Char()
    date = fields.Date()
    sequence = fields.Integer()
    active = fields.Boolean()
    partner_id = fields.Many2one("res.partner")
    currency_id = fields.Many2one("res.currency")
    line_ids = fields.One2many("t.line", "order_id")

class Line(models.Model):
    _name = "t.line"

    order_id = fields.Many2one("t.order")

class Mixin(models.AbstractModel):
    _name = "t.mixin"
    _order = "sequence, id"
    _rec_name = "label"

    def first(self):
        return self.search([], order="sequence desc", limit=1)
"""


def _messages(run_mypy: RunMypy, source: str) -> list[str]:
    result = run_mypy(
        {
            "t_base": {"models.py": T_BASE},
            "t_use": {"depends": ["t_base"], "models.py": source},
        },
        check=["t_base", "t_use"],
    )
    assert result.messages("t_base/models.py") == []
    return result.messages("t_use/models.py")


def test_order_fields_reads_odoo_orders() -> None:
    assert order_fields("date desc, partner_id.name, id") == ["date", "partner_id", "id"]
    assert order_fields(' "name" ASC NULLS LAST,sequence:sum') == ["name", "sequence"]
    assert order_fields("date desc,") is None
    assert order_fields("date descending") is None
    assert order_fields("lower(name)") is None


SEARCH_ORDER = """
from typing import Final
from odoo import models

BY_DATE: Final = "dat desc"

class Order(models.Model):
    _inherit = "t.order"

    def find(self, order):
        self.search([], order="date desc, partner_id.name nulls last, id")
        self.search([], order="dat desc")
        self.search([], 0, None, "sequense")
        self.search_read([], ["name"], order="name, nme")
        self.search_fetch([], ["name"], order="date descending")
        self._search([], order=BY_DATE)
        self.search([], order=order)
        self.search([], order="")
        if "priority" in self._fields:
            self.search([], order="priority")
        return self.env["res.partner"].search([], order="name, zipp")
"""


def test_search_order_names_fields_of_the_model(run_mypy: RunMypy) -> None:
    assert _messages(run_mypy, SEARCH_ORDER) == [
        '12: error: Odoo model "t.order" has no field "dat" in search(order=...); did you mean '
        '"date"?  [odoo-field-ref]',
        '13: error: Odoo model "t.order" has no field "sequense" in search(order=...); did you '
        'mean "sequence"?  [odoo-field-ref]',
        '14: error: Odoo model "t.order" has no field "nme" in search_read(order=...); did you '
        'mean "name"?  [odoo-field-ref]',
        '15: error: search_fetch(order="date descending") is not an order: Odoo raises  '
        "[odoo-field-ref]",
        '16: error: Odoo model "t.order" has no field "dat" in _search(order=...); did you mean '
        '"date"?  [odoo-field-ref]',
        '21: error: Odoo model "res.partner" has no field "zipp" in search(order=...); did you '
        'mean "zip"?  [odoo-field-ref]',
    ]


META = """
from odoo import fields, models

class Order(models.Model):
    _inherit = "t.order"
    _order = "date desc, sequense, later_name"
    _rec_name = "nam"
    _rec_names_search = ["name", "partner_id.email", "reff"]
    _active_name = "active"
    _fold_name = "folded"

    later_name = fields.Char()

class Line(models.Model):
    _inherit = "t.line"
    _order = "order_id,"
    _parent_name = "order_id"

class Fresh(models.Model):
    _name = "t.fresh"
    _inherit = ["t.mixin"]
    _order = "sequence, labl"

    sequence = fields.Integer()
    label = fields.Char()

class Other(models.AbstractModel):
    _name = "t.other"
    _rec_name = "anything"
"""


def test_class_attributes_name_fields_of_the_model(run_mypy: RunMypy) -> None:
    assert _messages(run_mypy, META) == [
        '6: error: Odoo model "t.order" has no field "sequense" in _order; did you mean '
        '"sequence"?  [odoo-field-ref]',
        '7: error: Odoo model "t.order" has no field "nam" in _rec_name; did you mean "name"?  '
        "[odoo-field-ref]",
        '8: error: Odoo model "t.order" has no field "reff" in _rec_names_search  [odoo-field-ref]',
        '10: error: Odoo model "t.order" has no field "folded" in _fold_name  [odoo-field-ref]',
        '16: error: _order = "order_id," is not an order: Odoo raises when it searches  '
        "[odoo-field-ref]",
        '22: error: Odoo model "t.fresh" has no field "labl" in _order; did you mean "label"?  '
        "[odoo-field-ref]",
    ]


FIELD_ARGUMENTS = """
from odoo import fields, models

class Order(models.Model):
    _inherit = "t.order"

    amount = fields.Monetary(currency_field="currency_id")
    total = fields.Monetary("Total", "curency_id")
    tax = fields.Monetary(currency_field="company_currency")
    item_ids = fields.One2many("t.line", "order_id")
    wrong_ids = fields.One2many("t.line", inverse_name="ordr_id")
    partner_ids = fields.One2many("res.partner", "parent_idd")
    unknown_ids = fields.One2many(inverse_name="whatever")
    stage = fields.Char(group_expand="_expand_stage")
    state = fields.Char(group_expand="_expand_state")

    def _expand_stage(self, values, domain):
        return values
"""


def test_field_arguments_name_fields_and_methods(run_mypy: RunMypy) -> None:
    assert _messages(run_mypy, FIELD_ARGUMENTS) == [
        '8: error: Odoo model "t.order" has no field "curency_id" (currency_field="curency_id"); '
        'did you mean "currency_id"?  [odoo-field-ref]',
        '9: error: Odoo model "t.order" has no field "company_currency" '
        '(currency_field="company_currency")  [odoo-field-ref]',
        '11: error: Odoo model "t.line" has no field "ordr_id" (inverse_name="ordr_id"); did '
        'you mean "order_id"?  [odoo-field-ref]',
        '12: error: Odoo model "res.partner" has no field "parent_idd" '
        '(inverse_name="parent_idd"); did you mean "parent_id"?  [odoo-field-ref]',
        '15: error: Odoo model "t.order" has no method "_expand_state" '
        '(group_expand="_expand_state"); did you mean "_expand_stage"?  [odoo-field-ref]',
    ]


FIELD_LISTS = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def preload(self, names):
        self.fetch(["name", "dat"])
        self.search_fetch([], ["name", "partner"], order="id")
        self.fetch(names)
        self.fetch(["name", *names])
        self.env["t.mixin"].fetch(["whatever"])
        if "priority" in self._fields:
            self.fetch(["priority"])
"""


def test_field_name_lists_of_fetch(run_mypy: RunMypy) -> None:
    assert _messages(run_mypy, FIELD_LISTS) == [
        '8: error: Odoo model "t.order" has no field "dat" in fetch(); did you mean "date"?  '
        "[odoo-field-ref]",
        '9: error: Odoo model "t.order" has no field "partner" in search_fetch(); did you mean '
        '"partner_id"?  [odoo-field-ref]',
    ]


READ_GROUP = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def totals(self, groupby):
        self._read_group([], ["partner_id", "date:month"], ["sequence:sum", "__count"])
        self._read_group([], ["partnr_id"], ["sequence:summ"])
        self._read_group([], groupby=["date:mnth"], aggregates=["sequense:max", "sequence"])
        self._read_group([], ["date month"], ["id:recordset"])
        self._read_group([], groupby, ["name:array_agg"])
        self._read_group([], ["partner_id.country_id", "name:whatever"])
        self.env["t.mixin"]._read_group([], ["anything"], ["other:sum"])
"""


def test_read_group_specifications(run_mypy: RunMypy) -> None:
    assert _messages(run_mypy, READ_GROUP) == [
        '9: error: Odoo model "t.order" has no field "partnr_id" in _read_group(groupby=...); '
        'did you mean "partner_id"?  [odoo-field-ref]',
        '9: error: "summ" is not an aggregate function; did you mean "sum"?  [odoo-field-ref]',
        '10: error: "mnth" is not a granularity of date field "date"; did you mean "month"?  '
        "[odoo-field-ref]",
        '10: error: Odoo model "t.order" has no field "sequense" in _read_group(aggregates=...); '
        'did you mean "sequence"?  [odoo-field-ref]',
        '10: error: Aggregate "sequence" has no function ("sequence:sum")  [odoo-field-ref]',
        '11: error: "date month" in _read_group(groupby=...) is not a specification  '
        "[odoo-field-ref]",
    ]


XML_IDS = """
from odoo import fields, models

class Order(models.Model):
    _inherit = "t.order"

    secret = fields.Char(groups="base.group_system,base.group_sytem")
    plain = fields.Char(groups="base.group_user")

    def allowed(self, name):
        user = self.env.user
        action = self.env["ir.actions.act_window"]._for_xml_id("base.action_partner_form")
        self.env["ir.actions.actions"]._for_xml_id("base.action_partner_frm")
        self.env["ir.actions.actions"]._for_xml_id("base.group_user")
        user.has_group(name)
        if user.has_group("website.group_website_designer"):
            return self.env["ir.actions.actions"]._for_xml_id("website.action_website")
        return user.has_group("base.group_user") or user.has_group("base.group_usr")
"""


def test_xml_ids_of_groups_and_actions(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, XML_IDS)
    assert [message.partition(";")[0] for message in messages] == [
        '7: error: Unknown XML id "base.group_sytem"',
        '13: error: Unknown XML id "base.action_partner_frm"',
        '14: error: XML id "base.group_user" is a record of Odoo model "res.groups", not an '
        "action  [odoo-xmlid]",
        '17: error: XML id "website.action_website" is created by addon "website", which is not '
        'in the dependencies of "t_use"  [odoo-xmlid-depends]',
        '18: error: Unknown XML id "base.group_usr"',
    ]
