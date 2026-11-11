"""Unit tests of what the scan records about each field definition and method (no mypy run)."""

from __future__ import annotations

from pathlib import Path

from test_registry_mro import BASE, _files
from test_registry_semantics import _build, _get

from odoo_typing.scanner import Contribution, FieldInfo

SOURCE = """
from odoo import _, fields, models
from .consts import IMPORTED

STATES = [("draft", "Draft"), ("done", _("Done"))]
COMPUTE = "_compute_total"


class Order(models.Model):
    _name = "t.order"
    _inherits = {"t.parent": "parent_id"}

    partner_id = fields.Many2one("res.partner", "Partner", ondelete="cascade")
    line_ids = fields.One2many("t.line", "order_id", string="Lines")
    tag_ids = fields.Many2many(comodel_name="t.tag")
    state = fields.Selection(STATES, required=True, readonly=False)
    kind = fields.Selection(selection="_selection_kind")
    level = fields.Selection(_selection_level)
    mode = fields.Selection(lambda self: [])
    total = fields.Float(compute=_compute_total, inverse="_inverse_total", store=True)
    other = fields.Integer(compute=COMPUTE, search=lambda self, op, value: [])
    name = fields.Char(related="partner_id.name", readonly=IMPORTED)
    note = fields.Text(IMPORTED)
    note = fields.Html("Note")
    parent_id = fields.Many2one("t.parent", delegate=True)

    def _compute_total(self):
        pass

    @staticmethod
    def _selection_level():
        return []

    async def _inverse_total(self):
        pass


class OrderExtension(models.Model):
    _inherit = "t.order"

    state = fields.Selection(selection_add=[("sent", "Sent"), ("cancel",)])
    kind = fields.Selection(selection_add=IMPORTED)
"""


def _scan(tmp_path: Path, fullname: str) -> Contribution:
    registry = _build(tmp_path, base=BASE, addons={"t_core": ([], _files(SOURCE))})
    return _get(registry, fullname)


def _field(contribution: Contribution, name: str) -> FieldInfo:
    return next(info for info in contribution.fields if info.name == name)


def test_scan_fields_relational_arguments_positional_and_keyword(tmp_path: Path) -> None:
    order = _scan(tmp_path, "t_core.m.Order")

    assert _field(order, "partner_id") == FieldInfo("partner_id", "Many2one", comodel="res.partner")
    assert _field(order, "line_ids") == FieldInfo(
        "line_ids", "One2many", comodel="t.line", inverse_name="order_id"
    )
    assert _field(order, "tag_ids").comodel == "t.tag"
    assert _field(order, "parent_id").delegate is True


def test_scan_fields_selection_list_constant_gives_keys(tmp_path: Path) -> None:
    state = _field(_scan(tmp_path, "t_core.m.Order"), "state")

    assert state.selection == ("draft", "done")
    assert (state.required, state.readonly) == (True, False)


def test_scan_fields_selection_method_by_name_or_function(tmp_path: Path) -> None:
    order = _scan(tmp_path, "t_core.m.Order")

    assert _field(order, "kind").selection_method == "_selection_kind"
    assert _field(order, "level").selection_method == "_selection_level"
    assert _field(order, "mode") == FieldInfo("mode", "Selection", dynamic=frozenset({"selection"}))


def test_scan_fields_methods_by_function_or_constant_name(tmp_path: Path) -> None:
    order = _scan(tmp_path, "t_core.m.Order")

    total = _field(order, "total")
    other = _field(order, "other")
    assert (total.compute, total.inverse, total.store) == ("_compute_total", "_inverse_total", True)
    assert (other.compute, other.search, other.dynamic) == ("_compute_total", None, {"search"})


def test_scan_fields_values_from_imports_are_dynamic(tmp_path: Path) -> None:
    name = _field(_scan(tmp_path, "t_core.m.Order"), "name")

    assert name == FieldInfo(
        "name", "Char", related="partner_id.name", dynamic=frozenset({"readonly"})
    )


def test_scan_fields_name_assigned_twice_keeps_last_definition(tmp_path: Path) -> None:
    order = _scan(tmp_path, "t_core.m.Order")

    assert [info for info in order.fields if info.name == "note"] == [FieldInfo("note", "Html")]
    assert order.field_names[-2:] == ("note", "parent_id")


def test_scan_fields_selection_add_keys_and_dynamic(tmp_path: Path) -> None:
    extension = _scan(tmp_path, "t_core.m.OrderExtension")

    assert _field(extension, "state").selection_add == ("sent", "cancel")
    assert _field(extension, "kind") == FieldInfo(
        "kind", "Selection", dynamic=frozenset({"selection_add"})
    )


def test_scan_methods_and_inherits_pairs(tmp_path: Path) -> None:
    order = _scan(tmp_path, "t_core.m.Order")

    assert order.methods == ("_compute_total", "_selection_level", "_inverse_total")
    assert order.inherits == (("t.parent", "parent_id"),)


def test_scan_methods_class_without_methods_has_none(tmp_path: Path) -> None:
    assert _scan(tmp_path, "t_core.m.OrderExtension").methods == ()


ALIASES = """
from odoo import fields, models
from .consts import STATES as BASE_STATES
from . import consts


class Thing(models.Model):
    _name = "t.thing"

    state = fields.Selection(BASE_STATES + [("extra", "Extra")])
    level = fields.Selection(consts.LEVELS)

    def __private(self):
        pass

    def _str_id(self):
        pass

    _str_to_char = _str_to_text = _str_id
    _check_company_domain = models.check_company_domain_parent_of
    _CONSTANT = consts.LEVELS
"""
CONSTS = 'STATES = [("draft", "Draft")]\nLEVELS = [("low", "Low")]\n'


def _thing(tmp_path: Path) -> Contribution:
    files = {
        "__init__.py": "from . import m\n",
        "m.py": ALIASES,
        "consts.py": CONSTS,
    }
    registry = _build(tmp_path, base=BASE, addons={"t_core": ([], files)})
    return _get(registry, "t_core.m.Thing")


def test_scan_methods_mangled_aliased_and_assigned_functions(tmp_path: Path) -> None:
    thing = _thing(tmp_path)

    assert thing.methods == (
        "_Thing__private",
        "_str_id",
        "_str_to_char",
        "_str_to_text",
        "_check_company_domain",
    )


def test_scan_fields_constants_imported_from_the_addon_and_concatenated(tmp_path: Path) -> None:
    thing = _thing(tmp_path)

    assert _field(thing, "state").selection == ("draft", "extra")
    assert _field(thing, "level").dynamic == {"selection"}  # attribute of a module
