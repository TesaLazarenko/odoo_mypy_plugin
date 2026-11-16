"""A dict or a list of mixed values: the type mypy joins them into is not their type.

mypy types ``values = {"partner": partner, "amount": 0.0, "lines": []}`` by joining the
values: ``dict[str, object]`` when they have nothing in common, a union when one of them
is one (``record.id`` is ``int | NewId``, an optional field ``str | Literal[False]``).
Every later use of a value is then an error (``"object" has no attribute "append"``,
``Unsupported operand types for + ("object" and "float")``), though the code is right:
the dict holds what was put under each key.

While records were ``Any`` such a dict was ``dict[str, Any]``; the records the plugin
types made it ``object``. mypy calls no hook for a dict or list display, so the hooks are
those of the reads: ``values["amount"]``, ``.get()``, ``.pop()``, ``.setdefault()``,
``.items()``, ``.values()``, and for a list ``rows[0]``, ``.pop()`` and iteration. There a
value of a joined type is ``Any``, as it was. Two signs of a join:

* the value type is ``object`` and the variable has no annotation: nothing can be done
  with an ``object``, and nobody declared it;
* the variable is a local one whose only value is a dict display of values of
  different types.

``values[key] = x`` of such a local dict takes any value. A declared
``dict[str, object]`` (a parameter, an annotated variable) stays as declared.
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
MIXED_WRITES = frozenset(f"{container}.__setitem__" for container in CONTAINERS)


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
        """``values[key] = x`` of a local dict of mixed values takes any value."""
        signature = ctx.default_signature
        joined = self._joined(ctx.type, _receiver(ctx.context), ctx)
        if not joined or not isinstance(signature, CallableType):
            return signature
        arguments = [_unknown(get_proper_type(item), joined) for item in signature.arg_types]
        return signature.copy_modified(arg_types=arguments)

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
        if (
            _is_join(value)
            and isinstance(receiver, NameExpr)
            and self._of_mixed_display(receiver, ctx)
        ):
            return [value]
        # ``rows = [[record, [], True]]`` is a ``list[list[object]]``: the object inside.
        found = next(filter(None, map(_object_in, arguments)), None)
        return [found] if found is not None else []

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


def _is_join(typ: ProperType) -> bool:
    """Whether a type is what mypy joins unrelated values into: no class of its own."""
    if isinstance(typ, UnionType):
        return True
    return isinstance(typ, Instance) and (
        typ.type.fullname == OBJECT or typ.type.is_abstract or typ.type.is_protocol
    )


def _object_in(typ: ProperType) -> ProperType | None:
    """The ``object`` among a type and the arguments of its classes, unions and tuples."""
    if isinstance(typ, Instance):
        if typ.type.fullname == OBJECT:
            return typ
        items = list(typ.args)
    elif isinstance(typ, (UnionType, TupleType)):
        items = list(typ.items)
    else:
        return None
    return next(filter(None, (_object_in(get_proper_type(item)) for item in items)), None)


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
