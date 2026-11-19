"""Calls of Odoo functions that handle their empty arguments (odoo_typing.falsy_guards)."""

from __future__ import annotations

from collections.abc import Callable

from mypy.nodes import ARG_POS, ARG_STAR, Expression
from mypy.plugin import (
    CheckerPluginInterface,
    FunctionContext,
    FunctionSigContext,
    MethodContext,
    MethodSigContext,
)
from mypy.types import (
    AnyType,
    CallableType,
    FunctionLike,
    Instance,
    LiteralType,
    NoneType,
    Overloaded,
    Type,
    TypeOfAny,
    UnionType,
    get_proper_type,
)

# isort: split
# After mypy.types: these modules import each other at import time.
from mypy.typeops import make_simplified_union

from odoo_typing.falsy_guards import Guard
from odoo_typing.hooks.nodes import type_checker

__all__ = [
    "callable_first_signature",
    "decorator_function_signature",
    "guarded_result",
    "guarded_signature",
]


def guarded_signature(
    guard: Guard, base_model: str, ctx: FunctionSigContext | MethodSigContext
) -> FunctionLike:
    """A parameter the function returns early for when empty takes ``False`` and ``None``.

    ``format_date(env, line.date_maturity)``: Odoo declares ``value: datetime | date |
    str`` and starts with ``if not value: return ''``; ``str2bool(config.value)`` starts
    with ``if type(s) is bool: return s``. ``*args`` the function may take a function in
    place of (:func:`callable_first_signature`) take one.
    """
    signature = ctx.default_signature
    if not isinstance(signature, CallableType):
        return signature
    checker = type_checker(ctx.api)
    boolean = checker.named_type("builtins.bool")
    empty: list[Type] = [LiteralType(False, boolean), NoneType()]
    arg_types = [
        UnionType.make_union([typ, *empty])
        if name in guard.falsy
        else UnionType.make_union([typ, boolean])
        if name in guard.booleans
        else typ
        for name, typ in zip(signature.arg_names, signature.arg_types, strict=True)
    ]
    guarded = signature.copy_modified(arg_types=arg_types)
    if guard.callable_first:
        return callable_first_signature(guarded, base_model, ctx)
    return guarded


def callable_first_signature(
    signature: CallableType, base_model: str, ctx: FunctionSigContext | MethodSigContext
) -> CallableType:
    """``@api.constrains(lambda self: self._fields_to_check())`` on Odoo 17 and 18.

    Odoo declares ``*args: str`` and takes a function in their place
    (``if args and callable(args[0]): args = args[0]``), as Odoo 19 declares; the
    ``*args`` take a function then too.
    """
    if ARG_STAR not in signature.arg_kinds:
        return signature
    index = signature.arg_kinds.index(ARG_STAR)
    declared = get_proper_type(signature.arg_types[index])
    if isinstance(declared, AnyType):
        return signature
    function = CallableType(
        [_decorated_records(ctx, base_model) or AnyType(TypeOfAny.special_form)],
        [ARG_POS],
        [None],
        ctx.api.named_generic_type("builtins.object", []),
        ctx.api.named_generic_type("builtins.function", []),
    )
    arg_types = list(signature.arg_types)
    arg_types[index] = UnionType.make_union([signature.arg_types[index], function])
    return signature.copy_modified(arg_types=arg_types)


def decorator_function_signature(
    base_model: str, ctx: FunctionSigContext | MethodSigContext
) -> FunctionLike:
    """``@api.depends(lambda self: ...)`` in a model class: the function takes its records.

    Odoo 19+ declares ``func: Callable[[BaseModel], Collection[str]]``, records of no
    model, whose every field and method is an unknown attribute; Odoo calls the function
    with the records of the model whose method it decorates.
    """
    signature = ctx.default_signature
    records = _decorated_records(ctx, base_model)
    if records is None:
        return signature
    items = signature.items if isinstance(signature, Overloaded) else [signature]
    found = [_taking_records(item, records, base_model) for item in items]
    if all(new is old for new, old in zip(found, items, strict=True)):
        return signature
    return Overloaded(found) if isinstance(signature, Overloaded) else found[0]


def _taking_records(item: CallableType, records: Instance, base_model: str) -> CallableType:
    """A signature whose ``Callable[[BaseModel], ...]`` parameters take the records."""
    arg_types = []
    for arg in item.arg_types:
        function = get_proper_type(arg)
        first = (
            get_proper_type(function.arg_types[0])
            if isinstance(function, CallableType) and function.arg_types
            else None
        )
        if (
            isinstance(function, CallableType)
            and isinstance(first, Instance)
            and first.type.fullname == base_model
        ):
            arg = function.copy_modified(arg_types=[records, *function.arg_types[1:]])
        arg_types.append(arg)
    if all(new is old for new, old in zip(arg_types, item.arg_types, strict=True)):
        return item
    return item.copy_modified(arg_types=arg_types)


def _decorated_records(
    ctx: FunctionSigContext | MethodSigContext, base_model: str
) -> Instance | None:
    """The records of the model class whose body the call is in, if it is one."""
    info = type_checker(ctx.api).scope.active_class()
    return Instance(info, []) if info is not None and info.has_base(base_model) else None


def guarded_result(
    guard: Guard,
    proven: Callable[[Expression, CheckerPluginInterface], bool],
    ctx: FunctionContext | MethodContext,
) -> Type:
    """No ``None`` / ``False`` from a function that gives it only for an empty argument.

    ``fields.Date.to_date(value)`` (also ``from_string``) returns ``None`` only from
    ``if not value: return None``: for an argument that cannot be false, or a read of the
    context the function has found true (``if self.env.context.get("k"):``, then
    ``from_string(self.env.context["k"])``), the result is a ``date``.
    """
    for index, name in enumerate(ctx.callee_arg_names):
        if name not in guard.none_only_for or index >= len(ctx.args):
            continue
        given = list(zip(ctx.args[index], ctx.arg_types[index], strict=True))
        if given and all(
            not get_proper_type(typ).can_be_false or proven(node, ctx.api) for node, typ in given
        ):
            return _without_empty(ctx.default_return_type)
    return ctx.default_return_type


def _without_empty(found: Type) -> Type:
    proper = get_proper_type(found)
    if not isinstance(proper, UnionType):
        return found
    kept = [
        item
        for item in proper.items
        if not isinstance(get_proper_type(item), NoneType)
        and not (
            isinstance(literal := get_proper_type(item), LiteralType) and literal.value is False
        )
    ]
    return make_simplified_union(kept) if kept else found
