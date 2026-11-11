"""``vals`` of ``create()`` / ``write()``: the keys and values of literal dicts.

The signatures stay permissive (``create`` takes a dict or a list of dicts, see
:func:`odoo_typing.methods.create_signature`): a ``vals = {...}; vals.update(...)``
variable is ``dict[str, Any]``, which a ``TypedDict`` parameter would reject. Instead,
the method hook looks at the argument expression: a dict display (or a list of them for
``create``) has each literal key checked against the model's fields
(:meth:`odoo_typing.registry.Registry.fields`, the view of the checking addon) and each
value against the field's class (:data:`VALUE_TYPES`). Keys of ``**spread`` and
non-literal keys are not checked, nor are dicts built any other way.

What a field accepts follows Odoo's ``convert_to_cache``: ``False`` and ``None`` clear
almost every field. A plain ``bool`` value is taken where ``False`` is: Odoo code
writes ``x = cond and value`` and ``min(..., default=False)``, which mypy types
``bool``. ``Boolean`` takes any value (``bool(value)``), and a computed ``Binary`` that
is not stored often holds other data (widget dicts), so neither is checked.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from difflib import get_close_matches
from fnmatch import fnmatchcase
from types import MappingProxyType
from typing import Final

from mypy.errorcodes import ErrorCode
from mypy.lookup import lookup_fully_qualified
from mypy.messages import format_type, pretty_seq
from mypy.nodes import (
    CallExpr,
    DictExpr,
    Expression,
    IntExpr,
    ListExpr,
    MemberExpr,
    RefExpr,
    StrExpr,
    TupleExpr,
    TypeInfo,
)
from mypy.subtypes import is_subtype
from mypy.types import (
    AnyType,
    Instance,
    LiteralType,
    NoneType,
    ProperType,
    Type,
    TypeOfAny,
    UnionType,
    get_proper_type,
)

# isort: split
# After mypy.types: mypy.checker and mypy.types import each other at import time.
from mypy.checker import TypeChecker

from odoo_typing.scanner import FieldInfo

__all__ = [
    "FALSE",
    "NEW_ID",
    "NONE",
    "ODOO_VALS",
    "ODOO_VALS_KEY",
    "ODOO_VALS_TYPE",
    "RECORDS",
    "VALUE_TYPES",
    "ValsChecker",
    "ValsTarget",
    "vals_dicts",
]

ODOO_VALS = ErrorCode("odoo-vals", "Check the vals of create() and write()", "Odoo")
ODOO_VALS_KEY = ErrorCode(
    "odoo-vals-key", "Check that the keys of vals are fields", "Odoo", sub_code_of=ODOO_VALS
)
ODOO_VALS_TYPE = ErrorCode(
    "odoo-vals-type", "Check the types of the values in vals", "Odoo", sub_code_of=ODOO_VALS
)

FALSE: Final = "Literal[False]"
NONE: Final = "None"
RECORDS: Final = "records"  # a recordset (of the field's comodel, checked by model name)
NEW_ID: Final = "NewId"  # an id of a new record: ``record.id`` is ``int | NewId``
_TEXT = ("builtins.str", FALSE, NONE)
_INTEGER = ("builtins.int", NEW_ID, FALSE, NONE)
_FLOAT = ("builtins.float", FALSE, NONE)  # mypy promotes int to float
_DATE = ("datetime.date", "builtins.str", FALSE, NONE)  # datetime is a date
_BINARY = ("builtins.bytes", "builtins.str", FALSE, NONE)
_X2MANY = ("builtins.list", "builtins.tuple", RECORDS, FALSE, NONE)  # commands or ids
# Field class to the types of the values it takes; a class not listed (Json,
# Properties, a custom field class) is not checked.
VALUE_TYPES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "Char": _TEXT,
        "Text": _TEXT,
        "Html": _TEXT,
        "Selection": _TEXT,  # literal keys are checked against the selection
        "Integer": _INTEGER,
        "Many2oneReference": _INTEGER,
        "Float": _FLOAT,
        "Monetary": _FLOAT,
        "Date": _DATE,
        "Datetime": _DATE,
        "Binary": _BINARY,
        "Image": _BINARY,
        "Many2one": ("builtins.int", NEW_ID, RECORDS, "builtins.tuple", FALSE, NONE),
        "One2many": _X2MANY,
        "Many2many": _X2MANY,
        "Reference": ("builtins.str", RECORDS, FALSE, NONE),  # "model,id"
    }
)
RELATIONAL_CLASSES: Final = frozenset({"Many2one", "One2many", "Many2many"})
BINARY_CLASSES: Final = frozenset({"Binary", "Image"})
SHOWN_KEYS: Final = 8  # selection keys listed in a message
X2MANY_CLASSES: Final = frozenset({"One2many", "Many2many"})
# x2many commands that carry the vals of a comodel record: ``Command`` method and the
# position of its dict argument; ``(0, 0, vals)`` / ``(1, id, vals)`` tuples likewise.
COMMAND_VALS: Final = {"create": 0, "update": 1}
TUPLE_COMMANDS: Final = frozenset({0, 1})


@dataclass(frozen=True, slots=True)
class ValsTarget:
    """The model whose fields the ``vals`` of a call are.

    Attributes:
        model: Model name.
        fields: Its fields as the checking addon sees them.
        check_keys: Whether keys that are not fields are errors (not for abstract
            models: their methods run on models with more fields).
        ignore_keys: Glob patterns of keys never reported as unknown.
    """

    model: str
    fields: Mapping[str, FieldInfo]
    check_keys: bool
    ignore_keys: tuple[str, ...]


def vals_dicts(argument: Expression, many: bool) -> Iterator[DictExpr]:
    """The dict displays of a ``vals`` argument (``many``: ``create``, also a list)."""
    if isinstance(argument, DictExpr):
        yield argument
    elif many and isinstance(argument, ListExpr):
        yield from (item for item in argument.items if isinstance(item, DictExpr))


@dataclass(frozen=True, slots=True)
class ValsChecker:
    """Report the keys and values of ``vals`` dicts that do not fit the model's fields.

    Attributes:
        checker: The type checker of the module being checked.
        base_model: Full name of ``BaseModel`` (the type of recordset values).
        new_id: Full name of ``NewId``.
        model_of: The model of a model class, ``None`` if it is not one.
        concrete: Whether a model is not abstract (in the checking addon's view).
        target_for: The vals target of a model (for the vals of x2many commands).
    """

    checker: TypeChecker
    base_model: str
    new_id: str
    model_of: Callable[[TypeInfo], str | None]
    concrete: Callable[[str], bool]
    target_for: Callable[[str], ValsTarget]

    def check(self, target: ValsTarget, vals: DictExpr) -> None:
        """Report the problems of one dict display."""
        for key, value in vals.items:
            if not isinstance(key, StrExpr):
                continue  # ``**spread`` or a computed key
            info = target.fields.get(key.value)
            if info is None:
                self._unknown_key(target, key)
                continue
            value_type = self.checker.lookup_type_or_none(value)
            if value_type is not None:
                self.check_value(target.model, info, value, value_type)

    def _unknown_key(self, target: ValsTarget, key: StrExpr) -> None:
        if not target.check_keys or not target.fields:
            return
        if any(fnmatchcase(key.value, pattern) for pattern in target.ignore_keys):
            return
        message = f'Odoo model "{target.model}" has no field "{key.value}"'
        matches = get_close_matches(key.value, list(target.fields), n=3, cutoff=0.6)
        if matches:
            message += f"; did you mean {pretty_seq(matches, 'or')}?"
        self.checker.fail(message, key, code=ODOO_VALS_KEY)

    def check_value(self, model: str, info: FieldInfo, value: Expression, value_type: Type) -> None:
        """Report a value (of type ``value_type``) that a field of a model does not take."""
        accepted = self._accepted(info.field_type)
        if accepted is None or (info.field_type in BINARY_CLASSES and not _stored(info)):
            return
        # Every listed class takes False: also take ``bool`` (see the module docstring).
        widened = UnionType.make_union([accepted, self.checker.named_type("builtins.bool")])
        if not is_subtype(value_type, widened):
            options = self.checker.options
            self.checker.fail(
                f'Incompatible type for field "{info.name}" of Odoo model "{model}" '
                f"(expression has type {format_type(value_type, options)}, "
                f"{info.field_type} field takes {format_type(accepted, options)})",
                value,
                code=ODOO_VALS_TYPE,
            )
            return
        items = _union_items(value_type)
        if info.field_type == "Selection":
            self._check_selection_keys(model, info, items, value)
        if info.field_type in RELATIONAL_CLASSES and info.comodel and "comodel" not in info.dynamic:
            self._check_comodel(model, info, items, value)
            if info.field_type in X2MANY_CLASSES and isinstance(value, ListExpr):
                target = self.target_for(info.comodel)
                for vals in _command_vals(value):
                    self.check(target, vals)

    def _check_selection_keys(
        self, model: str, info: FieldInfo, items: Sequence[ProperType], value: Expression
    ) -> None:
        selection = info.selection
        if selection is None or "selection" in info.dynamic:
            return
        for key in _literal_strings(items):
            if key and key not in selection:  # Odoo stores a falsy value as False
                shown = ", ".join(f'"{item}"' for item in selection[:SHOWN_KEYS])
                more = ", ..." if len(selection) > SHOWN_KEYS else ""
                self.checker.fail(
                    f'Value "{key}" is not in the selection of field "{info.name}" of '
                    f'Odoo model "{model}" ({shown}{more})',
                    value,
                    code=ODOO_VALS_TYPE,
                )

    def _check_comodel(
        self, model: str, info: FieldInfo, items: Sequence[ProperType], value: Expression
    ) -> None:
        for item in items:
            other = self.model_of(item.type) if isinstance(item, Instance) else None
            if other is None or other == info.comodel or not self.concrete(other):
                continue
            self.checker.fail(
                f'Incompatible records for field "{info.name}" of Odoo model "{model}" '
                f'(expression has "{other}" records, {info.field_type} field takes '
                f'"{info.comodel}" records)',
                value,
                code=ODOO_VALS_TYPE,
            )

    def _accepted(self, field_type: str) -> Type | None:
        """The type of the values a field class takes; ``None``: not checked."""
        names = VALUE_TYPES.get(field_type)
        if names is None:
            return None
        items: list[Type] = []
        for name in names:
            item = self._named(name)
            if item is None:
                return None  # e.g. ``datetime`` not loaded: do not guess
            items.append(item)
        return UnionType.make_union(items)

    def _named(self, name: str) -> Type | None:
        if name == NONE:
            return NoneType()
        if name == FALSE:
            return LiteralType(False, self.checker.named_type("builtins.bool"))
        full = {RECORDS: self.base_model, NEW_ID: self.new_id}.get(name, name)
        node = lookup_fully_qualified(full, self.checker.modules)
        info = node.node if node is not None else None
        if not isinstance(info, TypeInfo):
            return None
        args: list[Type] = [AnyType(TypeOfAny.special_form)] * len(info.defn.type_vars)
        return Instance(info, args)


def _union_items(typ: Type) -> list[ProperType]:
    proper = get_proper_type(typ)
    if isinstance(proper, UnionType):
        return [get_proper_type(item) for item in proper.items]
    return [proper]


def _literal_strings(items: Sequence[ProperType]) -> Iterator[str]:
    """The string literals a value's type is (``"draft"``, ``Literal["a", "b"]``)."""
    for item in items:
        literal = item.last_known_value if isinstance(item, Instance) else item
        if isinstance(literal, LiteralType) and isinstance(literal.value, str):
            yield literal.value


def _command_vals(commands: ListExpr) -> Iterator[DictExpr]:
    """The comodel vals of an x2many value: ``Command.create({...})``, ``(0, 0, {...})``."""
    for item in commands.items:
        if isinstance(item, CallExpr):
            position = COMMAND_VALS.get(_command_name(item.callee) or "")
            if position is not None and len(item.args) > position:
                argument = item.args[position]
                if isinstance(argument, DictExpr):
                    yield argument
        elif isinstance(item, TupleExpr) and len(item.items) == 3:
            code, _, vals = item.items
            command = code.value if isinstance(code, IntExpr) else None
            if command in TUPLE_COMMANDS and isinstance(vals, DictExpr):
                yield vals


def _command_name(callee: Expression) -> str | None:
    """The method of Odoo's ``Command`` a callee is (``Command.create``), if it is one."""
    if (
        isinstance(callee, MemberExpr)
        and isinstance(callee.expr, RefExpr)
        and callee.expr.fullname.endswith(".Command")
    ):
        return callee.name
    return None


def _stored(info: FieldInfo) -> bool:
    """Whether a field is stored: computed and related fields are not, by default."""
    if info.store is not None:
        return info.store
    return info.compute is None and info.related is None and not info.dynamic & {"compute"}
