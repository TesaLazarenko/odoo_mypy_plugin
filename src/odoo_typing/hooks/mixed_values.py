"""Dicts and lists whose value type mypy took from their first values, not from the code.

mypy types ``values = {"partner": partner, "amount": 0.0, "lines": []}`` by joining the
values: ``dict[str, object]`` when they have nothing in common, a protocol they all
satisfy (``Container[Any]`` for records, a set and a list), a union when one of them is
one (``record.id`` is ``int | NewId``, an optional field ``str | Literal[False]``).
Every later use of a value is then an error (``"object" has no attribute "append"``,
``Unsupported operand types for + ("object" and "float")``), though the code is right:
the dict holds what was put under each key. While records were ``Any`` such a dict was
``dict[str, Any]``; the records the plugin types made it a join.

mypy calls no hook for a dict or list display, so the hooks are those of the reads
(``values["amount"]``, ``.get()``, ``.pop()``, ``.setdefault()``, ``.items()``,
``.values()``, ``rows[0]``, iteration) and of the writes (``values[key] = x``,
``.update()``, ``.setdefault()``). A read gives ``Any`` for a joined type:

* ``object`` or a protocol of the standard library (``Container``, ``Collection``,
  ``Sized``...) in a container that has no annotation: nobody declared it;
* the values of a local variable whose only value is a dict display of values of
  different types.

A **record dict** is a local variable built only by dict displays with string keys
(``context = {"default_move_type": "out_invoice"}``, also ``{}``) that the function then
changes under string keys (``context.update({"default_partner_id": partner.id})``,
``vals["name"] = name``): a record of named values, not a mapping of one type. mypy
takes its value type from the first display (or the first change of ``{}``), and every
change with another type was an error. Its values read as ``Any`` and its writes take
any value.

A write into a container without an annotation whose values are ``int`` takes a
``float`` too: ``totals = defaultdict(lambda: 0)``, then ``totals[key] += line.balance``.

A declared ``dict[str, object]`` (a parameter, an annotated variable) and a dict of one
type that the function does not change stay as declared.
"""

from __future__ import annotations

from mypy.erasetype import remove_instance_last_known_values
from mypy.nodes import (
    CallExpr,
    DictExpr,
    Expression,
    IndexExpr,
    ListExpr,
    MemberExpr,
    NameExpr,
    StrExpr,
    Var,
)
from mypy.plugin import MethodContext, MethodSigContext
from mypy.subtypes import is_same_type
from mypy.types import (
    AnyType,
    CallableType,
    FunctionLike,
    Instance,
    ProperType,
    TupleType,
    Type,
    TypeOfAny,
    TypeType,
    UnionType,
    get_proper_type,
)

from odoo_typing.classes import receiver_type
from odoo_typing.hooks.models import Hooks
from odoo_typing.hooks.nodes import type_checker
from odoo_typing.local_values import Change

__all__ = ["MIXED_ITERATIONS", "MIXED_READS", "MIXED_WRITES", "MixedValueHooks"]

DICT = "builtins.dict"
LIST = "builtins.list"
OBJECT = "builtins.object"
# mypy names a method's hook after the receiver's class: the dicts and lists of builtins
# and collections.
CONTAINERS = (DICT, LIST, "collections.defaultdict", "collections.OrderedDict")
# The methods that give the values of a dict or a list.
MIXED_READS = frozenset(
    f"{container}.{method}"
    for container in CONTAINERS
    for method in ("__getitem__", "get", "pop", "setdefault", "items", "values")
)
MIXED_ITERATIONS = frozenset(f"{container}.__iter__" for container in CONTAINERS)
MIXED_WRITES = frozenset(
    f"{container}.{method}"
    for container in CONTAINERS
    for method in ("__setitem__", "update", "setdefault")
)
INT = "builtins.int"
FLOAT = "builtins.float"
# The modules of the protocols mypy joins unrelated values into (``Container[Any]``).
STANDARD = frozenset({"builtins", "typing", "typing_extensions", "collections.abc"})


class MixedValueHooks(Hooks):
    """The values of the dicts and lists whose type mypy joined from mixed values."""

    def read(self, ctx: MethodContext) -> Type:
        """A value read from a container of mixed values is ``Any``, not the join."""
        return self._read(_receiver(ctx.context), ctx)

    def iterate(self, ctx: MethodContext) -> Type:
        """``for row in rows``: mypy asks for ``__iter__`` at the iterated expression itself."""
        return self._read(ctx.context if isinstance(ctx.context, Expression) else None, ctx)

    def _read(self, receiver: Expression | None, ctx: MethodContext) -> Type:
        joined = self._joined(ctx.type, receiver, ctx)
        if not joined:
            return ctx.default_return_type
        return _unknown(get_proper_type(ctx.default_return_type), joined)

    def write(self, ctx: MethodSigContext) -> FunctionLike:
        """``values[key] = x``, ``.update()``, ``.setdefault()`` of a container of joined values.

        A local dict of mixed values or a record dict takes any value; a container
        without an annotation whose values are ``int`` takes a ``float`` too.
        """
        signature = ctx.default_signature
        receiver = _receiver(ctx.context)
        if not isinstance(signature, CallableType):
            return signature
        joined = self._joined(ctx.type, receiver, ctx)
        if joined:
            arguments = [_unknown(get_proper_type(item), joined) for item in signature.arg_types]
            return signature.copy_modified(arguments)
        container = get_proper_type(ctx.type)
        if (
            isinstance(container, Instance)
            and container.args
            and _is_class(get_proper_type(container.args[-1]), INT)
            and receiver is not None
            and self._is_inferred(receiver, ctx)
            and signature.arg_types
            and _is_class(get_proper_type(signature.arg_types[-1]), INT)
        ):
            number = type_checker(ctx.api).named_type(FLOAT)
            return signature.copy_modified([*signature.arg_types[:-1], number])
        return signature

    def _joined(
        self, typ: Type, receiver: Expression | None, ctx: MethodContext | MethodSigContext
    ) -> list[ProperType]:
        """The type arguments of a dict or a list that are joins of mixed values."""
        container = get_proper_type(typ)
        if not isinstance(container, Instance) or receiver is None:
            return []
        if not (container.type.has_base(DICT) or container.type.has_base(LIST)):
            return []
        arguments = [get_proper_type(item) for item in container.args]
        if not arguments or not self._is_inferred(receiver, ctx):
            return []
        value = arguments[-1]
        if isinstance(receiver, NameExpr) and container.type.has_base(DICT):
            if self._is_record_dict(receiver, ctx):
                return [value]
            if _is_join(value) and self._of_mixed_display(receiver, ctx):
                return [value]
        # ``rows = [[record, [], True]]`` is a ``list[list[object]]``: the object inside.
        found = next(filter(None, map(_join_in, arguments)), None)
        return [found] if found is not None else []

    def _is_record_dict(self, name: NameExpr, ctx: MethodContext | MethodSigContext) -> bool:
        """Whether a local dict is built by string-keyed displays and changed under string keys."""
        local = self._local_values(ctx.api)
        if local is None:
            return False
        bindings = local.bindings(name)
        if not bindings or not all(map(_is_record_display, bindings)):
            return False
        return any(map(_is_named_change, local.changes(name)))

    def _is_inferred(self, expression: Expression, ctx: MethodContext | MethodSigContext) -> bool:
        """Whether the variable a container is read from has no annotation.

        ``rows`` for ``rows[0]["x"]`` and for ``rows.items()``.
        """
        while True:
            if isinstance(expression, IndexExpr):
                expression = expression.base
            elif isinstance(expression, CallExpr) and isinstance(expression.callee, MemberExpr):
                expression = expression.callee.expr  # ``data.items()``, ``rows.get(key)``
            else:
                break
        if isinstance(expression, (ListExpr, DictExpr)):
            return True  # ``for values in [first, second]:`` the display is read in place
        if isinstance(expression, NameExpr):
            return isinstance(expression.node, Var) and expression.node.is_inferred
        if not isinstance(expression, MemberExpr):
            return False
        owner = receiver_type(type_checker(ctx.api).lookup_type_or_none(expression.expr))
        if isinstance(owner, TypeType):
            owner = get_proper_type(owner.item)
        if isinstance(owner, CallableType) and owner.is_type_obj():
            owner = get_proper_type(owner.ret_type)
        symbol = owner.type.get(expression.name) if isinstance(owner, Instance) else None
        return symbol is not None and isinstance(symbol.node, Var) and symbol.node.is_inferred

    def _of_mixed_display(self, name: NameExpr, ctx: MethodContext | MethodSigContext) -> bool:
        """Whether a local variable's only value is a dict display of differently typed values."""
        local = self._local_values(ctx.api)
        bindings = local.bindings(name) if local is not None else []
        if len(bindings) != 1 or not isinstance(bindings[0], DictExpr):
            return False
        checker = type_checker(ctx.api)
        types = [checker.lookup_type_or_none(value) for _, value in bindings[0].items]
        known = [remove_instance_last_known_values(item) for item in types if item is not None]
        if len(known) != len(types) or len(known) < 2:
            return False
        return any(not is_same_type(item, known[0]) for item in known[1:])


def _receiver(context: object) -> Expression | None:
    """The container of ``container[key]`` or ``container.method(...)``."""
    if isinstance(context, IndexExpr):
        return context.base
    if isinstance(context, CallExpr) and isinstance(context.callee, MemberExpr):
        return context.callee.expr
    return None


def _is_record_display(value: Expression | None) -> bool:
    """``{"name": ..., "amount": ...}`` or ``{}``: a dict display with string keys only."""
    return isinstance(value, DictExpr) and all(isinstance(key, StrExpr) for key, _ in value.items)


def _is_named_change(change: Change) -> bool:
    """A change under a string key: ``x["name"] = v``, ``x.setdefault("name", v)``.

    Also ``x.update({"name": v})`` and ``x.update(name=v)``.
    """
    if change.method in ("__setitem__", "setdefault"):
        return bool(change.args) and isinstance(change.args[0], StrExpr)
    if change.method != "update":
        return False
    if any(name is not None for name in change.arg_names):
        return True
    return len(change.args) == 1 and _is_record_display(change.args[0])


def _is_class(typ: ProperType, fullname: str) -> bool:
    return isinstance(typ, Instance) and typ.type.fullname == fullname


def _is_join(typ: ProperType) -> bool:
    """Whether a type is what mypy joins unrelated values into: no class of its own."""
    if isinstance(typ, UnionType):
        return True
    return isinstance(typ, Instance) and (
        typ.type.fullname == OBJECT or typ.type.is_abstract or typ.type.is_protocol
    )


def _is_joined_protocol(typ: ProperType) -> bool:
    """``object``, or a protocol of the standard library (``Container[Any]``, ``Sized``)."""
    if not isinstance(typ, Instance):
        return False
    if typ.type.fullname == OBJECT:
        return True
    return typ.type.is_protocol and typ.type.module_name in STANDARD


def _join_in(typ: ProperType) -> ProperType | None:
    """The join among a type and the arguments of its classes, unions and tuples."""
    if isinstance(typ, Instance):
        if _is_joined_protocol(typ):
            return typ
        items = list(typ.args)
    elif isinstance(typ, (UnionType, TupleType)):
        items = list(typ.items)
    else:
        return None
    return next(filter(None, (_join_in(get_proper_type(item)) for item in items)), None)


def _unknown(typ: ProperType, joined: list[ProperType]) -> Type:
    """A type with every occurrence of the joined types as ``Any``."""
    if any(typ == item for item in joined):
        return AnyType(TypeOfAny.special_form)
    if isinstance(typ, UnionType):
        return UnionType.make_union(
            [_unknown(get_proper_type(item), joined) for item in typ.items], typ.line, typ.column
        )
    if isinstance(typ, TupleType):
        items = [_unknown(get_proper_type(item), joined) for item in typ.items]
        return typ.copy_modified(items=items)
    if isinstance(typ, Instance) and typ.args:
        arguments = [_unknown(get_proper_type(item), joined) for item in typ.args]
        return typ.copy_modified(args=arguments)
    return typ
