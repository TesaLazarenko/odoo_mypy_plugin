"""Unit tests of which calls in a class body are fields: the classes the callee derives from."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pytest_mock import MockerFixture
from test_registry_mro import BASE
from test_registry_semantics import _build, _get

from odoo_typing.records import Record
from odoo_typing.registry import sources
from odoo_typing.registry.sources import SourceClasses
from odoo_typing.scanner import Contribution, FieldClass, FieldInfo

FIELD_CLASSES = """
from functools import partial
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

class Lines(fields.One2many):
    pass

class State(fields.Selection):
    pass

class Helper:
    pass

class Mixin(Helper):
    pass

def money(**kwargs):
    return fields.Float(digits=(16, 2), **kwargs)

def partner(**kwargs):
    return fields.Many2one("res.partner", required=True, **kwargs)

def big(flag=False, **kwargs):
    if flag:
        return BigInteger(**kwargs)
    return Bigger(**kwargs)

def text(value):
    return str(value)

Amount = partial(fields.Monetary, currency_field="currency_id")
"""

MODELS = """
import odoo
from functools import partial
from odoo import fields as odoo_fields, models
from odoo.fields import Char, Date
from . import f
from .f import Amount, BigInteger, Bigger, Helper, Packed, Partner, Typed, big, money, text
from .f import partner

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
    price = money(required=True)
    owner_id = partner()
    seller_id = partner(readonly=True, required=False)
    count = big()
    amount = Amount("Amount")
    label = text("x")
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


def test_scan_fields_factory_makes_fields_of_the_class_it_returns(tmp_path: Path) -> None:
    fields = _own(tmp_path)

    assert fields["price"] == FieldInfo("price", "Float", required=True)
    assert fields["count"] == FieldInfo("count", "Integer")
    assert fields["amount"] == FieldInfo("amount", "Monetary")  # functools.partial
    # What the factory passes itself is not read: these attributes are unknown, not unset.
    assert fields["owner_id"] == FieldInfo(
        "owner_id", "Many2one", dynamic=frozenset({"comodel", "required"})
    )
    assert fields["seller_id"] == FieldInfo(
        "seller_id", "Many2one", readonly=True, required=False, dynamic=frozenset({"comodel"})
    )


def test_scan_fields_other_calls_are_no_fields(tmp_path: Path) -> None:
    fields = _own(tmp_path)

    assert {"label", "helper", "today", "bound", "limit"}.isdisjoint(fields)


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
        FieldClass("Lines", ("odoo.fields.One2many",)),
        FieldClass("State", ("odoo.fields.Selection",)),
        FieldClass("money", ("odoo.fields.Float",), arguments=True, passes=("digits",)),
        FieldClass("partner", ("odoo.fields.Many2one",), arguments=True, passes=("*", "required")),
        FieldClass("big", ("odoo.fields.Integer",), arguments=True),
        FieldClass("Amount", ("odoo.fields.Monetary",), arguments=True, passes=("currency_field",)),
    )
    # A module exports the field classes it imports from its addon: a package's __init__.
    models = registry.module_scan("t_core.m")
    assert models is not None
    assert FieldClass("BigInteger", ("t_core.f.BigInteger",)) in models.classes
    assert all(item.name != "Helper" for item in models.classes)


OTHER_ADDON = """
from odoo import fields, models
from odoo.addons.t_core import BigInteger
from odoo.addons.t_core.f import Lines, Packed, Partner, State, Typed, Helper, money
from odoo.addons.t_core import f as core_fields
from odoo.addons.t_missing.fields import Gone

COMODEL = compute_comodel()

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
    line_ids = Lines("t.line", "order_id")
    state = State([("a", "A"), ("b", "B")])
    kind = State(_kinds)
    computed = Partner(COMODEL)
    price = money()
    mine = Mine()

    def _kinds(self):
        return []
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
    assert fields["price"] == FieldInfo("price", "Float")  # a factory of another addon
    assert fields["name"] == FieldInfo("name", "Char")


def test_link_fields_positional_arguments_are_those_of_the_odoo_class(tmp_path: Path) -> None:
    fields = _linked(tmp_path)

    # The scan of one addon does not know the class of another: the positional arguments
    # are kept, and read once the registry knows which Odoo class takes them.
    assert fields["partner_id"] == FieldInfo("partner_id", "Many2one", comodel="res.partner")
    assert fields["partner_keyword_id"] == FieldInfo(
        "partner_keyword_id", "Many2one", comodel="res.partner"
    )
    assert fields["line_ids"] == FieldInfo(
        "line_ids", "One2many", comodel="t.line", inverse_name="order_id"
    )
    assert fields["state"] == FieldInfo("state", "Selection", selection=("a", "b"))
    assert fields["kind"] == FieldInfo("kind", "Selection", selection_method="_kinds")
    assert fields["computed"] == FieldInfo("computed", "Many2one", dynamic=frozenset({"comodel"}))
    # A class with an ``__init__`` of its own takes other arguments.
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


THIRD = """
from odoo import models
from odoo.addons.t_other.m import BigInteger, Session, Char

class Third(models.Model):
    _name = "t.third"

    big = BigInteger()
    name = Char()
    session = Session()
"""


def test_link_fields_class_another_addon_only_passes_on(tmp_path: Path) -> None:
    core = {
        "__init__.py": "from . import m\nfrom .f import BigInteger\n",
        "f.py": FIELD_CLASSES,
        "m.py": MODELS,
    }
    passing = "from odoo.addons.t_core import BigInteger\nfrom odoo.fields import Char\n"
    other = {"__init__.py": "from . import m\n", "m.py": passing + "from requests import Session\n"}
    third = {"__init__.py": "from . import m\n", "m.py": THIRD}
    registry = _build(
        tmp_path,
        base=BASE,
        addons={
            "t_core": ([], core),
            "t_other": (["t_core"], other),
            "t_third": (["t_other"], third),
        },
    )

    fields = {info.name: info for info in _get(registry, "t_third.m.Third").fields}

    assert fields == {"big": FieldInfo("big", "Integer"), "name": FieldInfo("name", "Char")}


ODOO_FIELDS = """
from odoo.orm.fields import Field
from odoo.orm.fields_numeric import Integer, Float as Float
from odoo.orm.domains import Domain

class Command:
    pass
"""
ORM_FIELDS = "import typing\n\nclass Field(typing.Generic[T]):\n    pass\n"
ORM_NUMERIC = """
from .fields import Field

class Number(Field[int]):
    pass

class Integer(Number):
    pass

class Float(Field[float]):
    pass
"""
ORM_USE = """
from odoo import fields, models
from odoo.fields import Command, Domain
from odoo.orm.fields_numeric import Integer, Number
from t_library.fields import Money, Other

class Big(Integer):
    pass

class Order(models.Model):
    _name = "t.order"

    count = Integer()
    big = Big()
    number = Number()
    price = Money()
    other = Other()
    rule = Domain("a", "=", 1)
    command = Command()
    spelled = fields.Domain()
    serial = fields.Serialized()
"""


def test_link_fields_classes_of_odoo_modules_and_libraries_are_read_from_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "site" / "t_library"
    library.mkdir(parents=True)
    (library / "__init__.py").write_text("")
    (library / "fields.py").write_text(
        "from odoo import fields\n\nclass Money(fields.Float):\n    pass\n\n"
        "class Other:\n    pass\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    files = {"__init__.py": "from . import m\n", "m.py": ORM_USE}
    registry = _build(tmp_path, base=BASE, addons={"t_core": ([], files)})
    odoo = tmp_path / "odoo"
    (odoo / "orm").mkdir()
    (odoo / "fields.py").write_text(ODOO_FIELDS)
    (odoo / "orm" / "fields.py").write_text(ORM_FIELDS)
    (odoo / "orm" / "fields_numeric.py").write_text(ORM_NUMERIC)
    (odoo / "orm" / "domains.py").write_text("class Domain:\n    pass\n")

    fields = {info.name: info for info in _get(registry, "t_core.m.Order").fields}

    assert fields == {
        # Odoo's class, however it is reached: the name odoo.fields has it under.
        "count": FieldInfo("count", "Integer"),
        "big": FieldInfo("big", "Integer"),
        # A class of Odoo's that odoo.fields does not have: a field of an unknown class.
        "number": FieldInfo("number", "Field"),
        "price": FieldInfo("price", "Float"),  # a library's subclass
        # A name of odoo.fields that is no field class is no field; one it does not
        # have at all (an addon adds it at run time) is taken for one.
        "serial": FieldInfo("serial", "Serialized"),
    }


class _Keeper:
    """Keeps records in memory, as the scan store does between runs."""

    def __init__(self) -> None:
        self.records: dict[str, Record] = {}

    def load_record(self, key: str) -> Record | None:
        return self.records.get(key)

    def save_record(self, key: str, record: Record) -> None:
        self.records[key] = record


def test_source_classes_are_kept_until_the_file_changes(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    odoo = tmp_path / "odoo"
    (odoo / "orm").mkdir(parents=True)
    (odoo / "fields.py").write_text(ODOO_FIELDS)
    (odoo / "orm" / "fields.py").write_text(ORM_FIELDS)
    (odoo / "orm" / "fields_numeric.py").write_text(ORM_NUMERIC)
    keeper = _Keeper()
    first = SourceClasses(odoo, lambda module: None, keeper)
    integer = first.describe("odoo.orm.fields_numeric.Integer")
    first.save()
    parse = mocker.spy(sources, "parse_source")

    second = SourceClasses(odoo, lambda module: None, keeper)
    kept = second.describe("odoo.orm.fields_numeric.Integer")

    assert kept == integer == FieldClass("Integer", ("odoo.fields.Integer",))
    assert parse.call_count == 0
    assert not second.inputs_changed()
    numeric = odoo / "orm" / "fields_numeric.py"
    numeric.write_text(ORM_NUMERIC.replace("class Integer(Number)", "class Integer"))
    os.utime(numeric, ns=(1, 1))  # another stamp, whatever the clock's resolution

    assert second.inputs_changed()  # a long-lived registry starts over
    third = SourceClasses(odoo, lambda module: None, keeper)

    assert third.describe("odoo.orm.fields_numeric.Integer") is None
    assert [call.args[0] for call in parse.call_args_list] == [numeric]
