"""Unit tests of what the scan records about each field definition and method (no mypy run)."""

from __future__ import annotations

from pathlib import Path

from conftest import without_given
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
    """A field of a class, without the list of its arguments (``_given`` reads that)."""
    return without_given(next(info for info in contribution.fields if info.name == name))


def _given(contribution: Contribution, name: str) -> tuple[str, ...]:
    return next(info for info in contribution.fields if info.name == name).given


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

    notes = [without_given(info) for info in order.fields if info.name == "note"]
    assert notes == [FieldInfo("note", "Html")]
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


KINDS = """
from odoo import models


class Mixin(models.AbstractModel):
    _name = "t.mixin"


class Wizard(models.TransientModel):
    _name = "t.wizard"


class Explicit(models.AbstractModel):
    _name = "t.explicit"
    _abstract = False


class Custom(SomeBase):
    _name = "t.custom"
"""


def test_scan_abstract_from_base_class_or_attribute(tmp_path: Path) -> None:
    registry = _build(tmp_path, base=BASE, addons={"t_core": ([], _files(KINDS))})

    kinds = [_get(registry, f"t_core.m.{name}").abstract for name in ("Mixin", "Wizard")]
    others = [_get(registry, f"t_core.m.{name}").abstract for name in ("Explicit", "Custom")]
    assert (kinds, others) == ([True, False], [False, None])


def test_scan_fields_compute_sql_is_passed_and_attachment_is_a_flag(tmp_path: Path) -> None:
    source = (
        "from odoo import fields, models\n\nclass Doc(models.Model):\n"
        '    _name = "t.doc"\n'
        '    total = fields.Float(compute="_c", compute_sql=lambda self, alias: None)\n'
        "    datas = fields.Binary(attachment=False)\n"
        "    plain = fields.Char()\n"
    )
    registry = _build(tmp_path, base=BASE, addons={"t_core": ([], _files(source))})
    doc = _get(registry, "t_core.m.Doc")

    assert _field(doc, "total").compute_sql is True
    assert _field(doc, "total").dynamic == frozenset()  # passed, its value does not matter
    assert _field(doc, "datas").attachment is False
    assert _field(doc, "plain") == FieldInfo("plain", "Char")


GIVEN = """
from odoo import fields, models
from .consts import IMPORTED

GROUPS = "base.group_user"
OPTIONS = {"size": 5}


class Journal(models.Model):
    _name = "t.journal"

    code = fields.Char("Code", size=5, groups=GROUPS + ",base.group_system", copy=False)
    name = fields.Char(string="", size=0, help=IMPORTED, digits=0.5, index=True)
    spread = fields.Char(size=5, **OPTIONS)
    starred = fields.Char(*["Label"], size=5)
    partner_id = fields.Many2one("res.partner", "Partner", ondelete="cascade")
    extra = fields.Many2one("res.partner", "Partner", "more")
    plain = fields.Char()
"""


def test_scan_fields_arguments_given_with_the_type_of_a_true_literal(tmp_path: Path) -> None:
    """``size=int`` for ``size=5``; the bare name for any other value; ``*`` for unnamed ones."""
    registry = _build(tmp_path, base=BASE, addons={"t_core": ([], _files(GIVEN))})
    journal = _get(registry, "t_core.m.Journal")

    assert _given(journal, "code") == ("copy", "groups=str", "size=int", "string=str")
    assert _given(journal, "name") == ("digits=float", "help", "index", "size", "string")
    assert _given(journal, "spread") == ("*",)
    assert _given(journal, "starred") == ("*",)
    assert _given(journal, "partner_id") == ("comodel_name=str", "ondelete=str", "string=str")
    assert _given(journal, "extra") == ("*",)  # more positional arguments than the class has
    assert _given(journal, "plain") == ()
