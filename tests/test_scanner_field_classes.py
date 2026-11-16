"""Unit tests of which calls in a class body are fields: the classes the callee derives from."""

from __future__ import annotations

from pathlib import Path

from test_registry_mro import BASE
from test_registry_semantics import _build, _get

from odoo_typing.scanner import Contribution, FieldClass, FieldInfo

FIELD_CLASSES = """
from odoo import fields
from odoo.fields import Integer

class BigInteger(Integer):
    column_type = ("int8", "int8")

class Bigger(BigInteger):
    pass

class Packed(fields.Json):
    def convert_to_cache(self, value, record, validate=True):
        return value

class Typed(fields.Char):
    def __init__(self, kind=None, **kwargs):
        super().__init__(**kwargs)

class Partner(fields.Many2one[object]):
    def convert_to_record(self, value, record):
        return value

class Helper:
    pass

class Mixin(Helper):
    pass
"""

MODELS = """
import odoo
from functools import partial
from odoo import fields as odoo_fields, models
from odoo.fields import Char, Date
from . import f
from .f import BigInteger, Bigger, Helper, Packed, Partner, Typed

Alias = BigInteger

class Order(models.Model):
    _name = "t.order"

    plain = odoo_fields.Char()
    imported = Char(required=True)
    dotted = odoo.fields.Boolean()
    big = BigInteger(readonly=True)
    bigger = Bigger()
    by_module = f.BigInteger()
    aliased = Alias()
    packed = Packed()
    typed = Typed("char", required=True)
    partner_id = Partner("res.partner")
    helper = Helper()
    today = Date.today()
    bound = partial(print)
    limit = int("3")
"""


def _scan(tmp_path: Path, files: dict[str, str]) -> Contribution:
    registry = _build(tmp_path, base=BASE, addons={"t_core": ([], files)})
    return _get(registry, "t_core.m.Order")


def _own(tmp_path: Path) -> dict[str, FieldInfo]:
    files = {"__init__.py": "from . import m\n", "f.py": FIELD_CLASSES, "m.py": MODELS}
    return {info.name: info for info in _scan(tmp_path, files).fields}


def test_scan_fields_classes_of_odoo_fields_however_they_are_named(tmp_path: Path) -> None:
    fields = _own(tmp_path)

    assert fields["plain"] == FieldInfo("plain", "Char")
    assert fields["imported"] == FieldInfo("imported", "Char", required=True)
    assert fields["dotted"] == FieldInfo("dotted", "Boolean")


def test_scan_fields_subclass_of_the_addon_is_the_odoo_class_it_derives_from(
    tmp_path: Path,
) -> None:
    fields = _own(tmp_path)

    assert fields["big"] == FieldInfo("big", "Integer", readonly=True)
    assert fields["bigger"] == FieldInfo("bigger", "Integer")
    assert fields["by_module"] == FieldInfo("by_module", "Integer")
    assert fields["aliased"] == FieldInfo("aliased", "Integer")


def test_scan_fields_subclass_that_converts_values_keeps_its_own_name(tmp_path: Path) -> None:
    fields = _own(tmp_path)

    assert fields["packed"] == FieldInfo("packed", "Packed")
    # A relational class keeps its comodel: paths through the field are still known.
    assert fields["partner_id"] == FieldInfo("partner_id", "Many2one", comodel="res.partner")


def test_scan_fields_subclass_with_init_positional_arguments_are_not_read(tmp_path: Path) -> None:
    fields = _own(tmp_path)

    assert fields["typed"] == FieldInfo("typed", "Char", required=True)


def test_scan_fields_other_calls_are_no_fields(tmp_path: Path) -> None:
    fields = _own(tmp_path)

    assert {"helper", "today", "bound", "limit"}.isdisjoint(fields)


def test_scan_fields_unbound_fields_name_is_odoo_fields(tmp_path: Path) -> None:
    source = 'class Order(models.Model):\n    _name = "t.order"\n    name = fields.Char()\n'

    order = _scan(tmp_path, {"__init__.py": "from . import m\n", "m.py": source})

    assert order.fields == (FieldInfo("name", "Char"),)


def test_scan_fields_rebound_name_is_not_the_class_it_was(tmp_path: Path) -> None:
    source = """
from odoo import models
from odoo.fields import Char

Char = dict

class Order(models.Model):
    _name = "t.order"
    name = Char()
"""

    order = _scan(tmp_path, {"__init__.py": "from . import m\n", "m.py": source})

    assert order.fields == ()


def test_scan_classes_field_classes_of_a_module_with_flattened_bases(tmp_path: Path) -> None:
    files = {"__init__.py": "from . import m\n", "f.py": FIELD_CLASSES, "m.py": MODELS}
    registry = _build(tmp_path, base=BASE, addons={"t_core": ([], files)})

    scan = registry.module_scan("t_core.f")

    assert scan is not None
    assert scan.classes == (
        FieldClass("BigInteger", ("odoo.fields.Integer",)),
        FieldClass("Bigger", ("odoo.fields.Integer",)),
        FieldClass("Packed", ("odoo.fields.Json",), converts=True),
        FieldClass("Typed", ("odoo.fields.Char",), arguments=True),
        FieldClass("Partner", ("odoo.fields.Many2one",), converts=True),
    )
    # A module exports the field classes it imports from its addon: a package's __init__.
    models = registry.module_scan("t_core.m")
    assert models is not None
    assert FieldClass("BigInteger", ("t_core.f.BigInteger",)) in models.classes
    assert all(item.name != "Helper" for item in models.classes)


OTHER_ADDON = """
from odoo import fields, models
from odoo.addons.t_core import BigInteger
from odoo.addons.t_core.f import Packed, Partner, Typed, Helper
from odoo.addons.t_core import f as core_fields
from odoo.addons.t_missing.fields import Gone

class Mine(Typed):
    pass

class Line(models.Model):
    _name = "t.line"

    big = BigInteger()
    by_module = core_fields.Bigger(required=True)
    packed = Packed()
    typed = Typed("char")
    partner_id = Partner("res.partner")
    partner_keyword_id = Partner(comodel_name="res.partner")
    mine = Mine()
    helper = Helper()
    gone = Gone()
    name = fields.Char()
"""


def _linked(tmp_path: Path) -> dict[str, FieldInfo]:
    core = {
        "__init__.py": "from . import m\nfrom .f import BigInteger\n",
        "f.py": FIELD_CLASSES,
        "m.py": MODELS,
    }
    other = {"__init__.py": "from . import m\n", "m.py": OTHER_ADDON}
    registry = _build(
        tmp_path, base=BASE, addons={"t_core": ([], core), "t_other": (["t_core"], other)}
    )
    return {info.name: info for info in _get(registry, "t_other.m.Line").fields}


def test_link_fields_class_of_another_addon_is_the_odoo_class_it_derives_from(
    tmp_path: Path,
) -> None:
    fields = _linked(tmp_path)

    assert fields["big"] == FieldInfo("big", "Integer")  # through the package's __init__
    assert fields["by_module"] == FieldInfo("by_module", "Integer", required=True)
    assert fields["packed"] == FieldInfo("packed", "Packed")
    assert fields["mine"] == FieldInfo("mine", "Char")
    assert fields["name"] == FieldInfo("name", "Char")


def test_link_fields_positional_arguments_of_another_addons_class_are_unknown(
    tmp_path: Path,
) -> None:
    fields = _linked(tmp_path)

    # The scan of one addon does not know the class: what its positional arguments may
    # give is marked as not evaluated, a keyword argument is read.
    assert fields["partner_id"] == FieldInfo(
        "partner_id", "Many2one", dynamic=frozenset({"comodel"})
    )
    assert fields["partner_keyword_id"] == FieldInfo(
        "partner_keyword_id", "Many2one", comodel="res.partner"
    )
    assert fields["typed"] == FieldInfo("typed", "Char")


def test_link_fields_name_that_is_no_field_class_is_no_field(tmp_path: Path) -> None:
    fields = _linked(tmp_path)

    assert {"helper", "gone"}.isdisjoint(fields)


def test_link_fields_classes_that_derive_from_each_other_are_no_fields(tmp_path: Path) -> None:
    first = """
from odoo import models
from odoo.addons.t_b.m import Second

class First(Second):
    pass

class Order(models.Model):
    _name = "t.order"
    value = First()
"""
    second = "from odoo.addons.t_a.m import First\n\nclass Second(First):\n    pass\n"
    files = {"__init__.py": "from . import m\n"}
    registry = _build(
        tmp_path,
        base=BASE,
        addons={"t_a": ([], {**files, "m.py": first}), "t_b": ([], {**files, "m.py": second})},
    )

    assert _get(registry, "t_a.m.Order").fields == ()
