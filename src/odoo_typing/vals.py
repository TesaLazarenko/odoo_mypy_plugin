"""``vals`` of ``create()`` / ``write()``: the keys and values of literal dicts.

The signatures stay permissive (``create`` takes a dict or a list of dicts, see
:func:`odoo_typing.methods.create_signature`): a ``vals = {...}; vals.update(...)``
variable is ``dict[str, Any]``, which a ``TypedDict`` parameter would reject. Instead,
the method hook looks at the argument expression: a dict display (or a list of them for
``create``) has each literal key checked against the model's fields
(:meth:`odoo_typing.registry.Registry.fields`, the view of the checking addon) and each
value against the field's class (:data:`VALUE_TYPES`). So are ``dict(k=v)`` and a variable
built before the call (:mod:`odoo_typing.local_values`): its literal value and the keys
added in place. Keys of ``**spread`` and non-literal keys are not checked.

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
from mypy.messages import format_type
from mypy.nodes import (
    ARG_POS,
    CallExpr,
    Context,
    DictExpr,
    Expression,
    IntExpr,
    ListExpr,
    MemberExpr,
    NameExpr,
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

from odoo_typing.local_values import MAX_DEPTH, Change, LocalValues
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
    "ContextDefault",
    "ValsChecker",
    "ValsTarget",
    "context_defaults",
    "stored",
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
# Context keys that give a default to a field of the created records.
DEFAULT_PREFIX: Final = "default_"
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


def vals_dicts(
    argument: Expression, many: bool, local: LocalValues | None = None, depth: int = 0
) -> Iterator[DictExpr]:
    """The dict displays of a ``vals`` argument (``many``: ``create``, also a list).

    Also ``dict(k=v, ...)``, and, with the function's :class:`LocalValues`, a variable built
    before the call: its literal value and what was added to it in place
    (``vals["k"] = v``, ``vals.update(...)``, ``vals_list.append({...})``...).
    """
    if isinstance(argument, DictExpr):
        yield argument
    elif isinstance(argument, CallExpr) and _is_dict_call(argument):
        for value, kind in zip(argument.args, argument.arg_kinds, strict=True):
            if kind == ARG_POS:  # dict(other, k=v): the keys of ``other`` too
                yield from vals_dicts(value, False, local, depth + 1)
        yield from _keyword_dicts(argument.args, argument.arg_names)
    elif many and isinstance(argument, (ListExpr, TupleExpr)):
        for item in argument.items:
            yield from vals_dicts(item, False, local, depth + 1)
    elif isinstance(argument, NameExpr) and local is not None and depth < MAX_DEPTH:
        built = local.built(argument, argument.line)
        if built.value is not None:
            yield from vals_dicts(built.value, many, local, depth + 1)
        for change in built.changes:
            yield from _changed_dicts(change, many, local, depth + 1)


def _changed_dicts(
    change: Change, many: bool, local: LocalValues, depth: int
) -> Iterator[DictExpr]:
    """The dicts an in-place change adds to a vals variable (a dict, or a list of them)."""
    args = change.args
    if change.method in {"__setitem__", "setdefault"}:  # vals["k"] = v, setdefault("k", v)
        if len(args) == 2 and isinstance(args[0], StrExpr):
            yield _synthetic_dict([(args[0], args[1])])
    elif change.method in {"update", "__ior__"}:
        for value, name in zip(args, change.arg_names, strict=True):
            if name is None:
                yield from vals_dicts(value, False, local, depth)
        yield from _keyword_dicts(args, change.arg_names)
    elif many and change.method == "append" and len(args) == 1:
        yield from vals_dicts(args[0], False, local, depth)
    elif many and change.method == "insert" and len(args) == 2:
        yield from vals_dicts(args[1], False, local, depth)
    elif many and change.method in {"extend", "__iadd__"} and len(args) == 1:
        yield from vals_dicts(args[0], True, local, depth)


def _keyword_dicts(args: Sequence[Expression], names: Sequence[str | None]) -> Iterator[DictExpr]:
    """``dict(k=v)`` / ``vals.update(k=v)``: the keyword arguments as one dict."""
    items: list[tuple[StrExpr, Expression]] = []
    for value, name in zip(args, names, strict=True):
        if name is not None:
            key = StrExpr(name)
            key.set_line(value)
            items.append((key, value))
    if items:
        yield _synthetic_dict(items)


def _synthetic_dict(items: Sequence[tuple[StrExpr, Expression]]) -> DictExpr:
    """A dict display of keys and values found elsewhere (messages point at the keys)."""
    display = DictExpr(list(items))
    display.set_line(items[0][0])
    return display


def _is_dict_call(call: CallExpr) -> bool:
    return isinstance(call.callee, RefExpr) and call.callee.fullname == "builtins.dict"


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
        guarded: Whether a field name is guarded on a line of the checked file
            (``if "f" in self._fields:``, :func:`odoo_typing.guards.field_guard`).
    """

    checker: TypeChecker
    base_model: str
    new_id: str
    model_of: Callable[[TypeInfo], str | None]
    concrete: Callable[[str], bool]
    target_for: Callable[[str], ValsTarget]
    guarded: Callable[[str, int], bool]

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

    def check_defaults(self, target: ValsTarget, defaults: Sequence[ContextDefault]) -> None:
        """Report the ``default_<field>`` keys of a context that the created model lacks."""
        for default in defaults:
            info = target.fields.get(default.field)
            if info is None:
                # Odoo ignores a default of a field the model lacks, and code sets
                # defaults for fields of optional addons on purpose: only a near miss
                # of an existing field (a typo) is reported.
                if get_close_matches(default.field, list(target.fields), n=1, cutoff=0.8):
                    where = f" ({default.key} in with_context)"
                    self._unknown_key(target, default.node, default.field, where)
                continue
            value_type = self.checker.lookup_type_or_none(default.value)
            if value_type is not None:
                self.check_value(target.model, info, default.value, value_type)

    def _unknown_key(
        self, target: ValsTarget, node: Context, name: str | None = None, where: str = ""
    ) -> None:
        name = name if name is not None else node.value if isinstance(node, StrExpr) else ""
        if not target.check_keys or not target.fields:
            return
        if any(fnmatchcase(name, pattern) for pattern in target.ignore_keys):
            return
        if self.guarded(name, node.line):
            return
        message = f'Odoo model "{target.model}" has no field "{name}"{where}'
        matches = get_close_matches(name, list(target.fields), n=1, cutoff=0.6)
        if matches:
            message += f'; did you mean "{matches[0]}"?'
        self.checker.fail(message, node, code=ODOO_VALS_KEY)

    def check_value(self, model: str, info: FieldInfo, value: Expression, value_type: Type) -> None:
        """Report a value (of type ``value_type``) that a field of a model does not take."""
        accepted = self._accepted(info.field_type)
        if accepted is None or (info.field_type in BINARY_CLASSES and not stored(info)):
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


@dataclass(frozen=True, slots=True)
class ContextDefault:
    """A ``default_<field>`` entry of a context.

    Attributes:
        key: The context key (``default_partner_id``).
        field: The field it gives a default to (``partner_id``).
        value: The default value.
        node: Where a message about the key points.
    """

    key: str
    field: str
    value: Expression
    node: Context


def context_defaults(
    receiver: Expression, local: LocalValues | None = None
) -> list[ContextDefault]:
    """The ``default_<field>`` entries of ``records.with_context(...)``, if it is that.

    Keyword arguments and the keys of the positional dict count: a dict display,
    ``dict(...)``, or a variable built before (with the function's :class:`LocalValues`).
    """
    if not (
        isinstance(receiver, CallExpr)
        and isinstance(receiver.callee, MemberExpr)
        and receiver.callee.name == "with_context"
    ):
        return []
    found: list[ContextDefault] = []
    arguments = zip(receiver.arg_names, receiver.args, receiver.arg_kinds, strict=True)
    for name, value, kind in arguments:
        if name is not None and name.startswith(DEFAULT_PREFIX):
            found.append(ContextDefault(name, name.removeprefix(DEFAULT_PREFIX), value, value))
        elif name is None and kind == ARG_POS:
            for display in vals_dicts(value, False, local):
                for key, item in display.items:
                    if isinstance(key, StrExpr) and key.value.startswith(DEFAULT_PREFIX):
                        field = key.value.removeprefix(DEFAULT_PREFIX)
                        found.append(ContextDefault(key.value, field, item, key))
    return found


def stored(info: FieldInfo) -> bool:
    """Whether a field is stored: computed and related fields are not, by default."""
    if info.store is not None:
        return info.store
    return info.compute is None and info.related is None and not info.dynamic & {"compute"}
