"""Recordset methods: keep the model type through Odoo's decorators and overrides.

Odoo 18 declares ``sudo``, ``with_context``, ``filtered``, ``search``, ``copy``... as
``-> Self``, but wraps many of them in decorators without annotations
(``api.private``, ``api.returns``), so mypy types the decorated methods as ``Any``.
Addons then override ``create``, ``copy``, ``search``... without annotations, which
also gives ``Any``. These hooks restore the declared signatures and the receiver type.
"""

from __future__ import annotations

from typing import Final

from mypy.lookup import lookup_fully_qualified
from mypy.nodes import Decorator, FuncDef, TypeInfo
from mypy.plugin import ClassDefContext, FunctionContext, MethodContext, MethodSigContext
from mypy.types import (
    AnyType,
    CallableType,
    FunctionLike,
    Instance,
    Type,
    TypeOfAny,
    TypeVarId,
    TypeVarType,
    UnionType,
    get_proper_type,
)
from mypy.typevars import fill_typevars

# isort: split
# After mypy.types: mypy.checker and mypy.types import each other at import time.
from mypy.checker import TypeChecker

__all__ = [
    "SELF_METHODS",
    "annotate_self_overrides",
    "create_signature",
    "identity_decorator",
    "identity_factory",
    "is_create",
    "is_self_method",
    "keep_receiver",
]

# BaseModel methods declared ``-> Self``: an unannotated override returns the receiver.
SELF_METHODS: Final = frozenset(
    {
        "browse",
        "copy",
        "create",
        "ensure_one",
        "exists",
        "filtered",
        "filtered_domain",
        "search",
        "sorted",
        "sudo",
        "with_company",
        "with_context",
        "with_env",
        "with_prefetch",
        "with_user",
    }
)


def identity_decorator(ctx: FunctionContext) -> Type:
    """``@api.private`` and the like: the decorated method keeps its own signature."""
    if len(ctx.arg_types) == 1 and len(ctx.arg_types[0]) == 1:
        return ctx.arg_types[0][0]
    return ctx.default_return_type


def identity_factory(template: str, ctx: FunctionContext) -> Type:
    """``@api.returns("self")``: a decorator that keeps the method's own signature.

    ``template`` is the full name of a typed ``def [T] (T) -> T`` (``api.model``).
    """
    if not isinstance(ctx.api, TypeChecker):
        return ctx.default_return_type
    node = lookup_fully_qualified(template, ctx.api.modules)
    func = node.node if node is not None else None
    if isinstance(func, FuncDef) and isinstance(func.type, CallableType):
        return func.type
    return ctx.default_return_type


def is_self_method(fullname: str) -> bool:
    """Whether a method's full name (``<receiver class>.<name>``) is a Self method."""
    return fullname.rpartition(".")[2] in SELF_METHODS


def is_create(fullname: str) -> bool:
    """Whether a method's full name is ``<receiver class>.create``."""
    return fullname.rpartition(".")[2] == "create"


def create_signature(base_model: str, ctx: MethodSigContext) -> FunctionLike:
    """``create(vals_list: list[X])`` also accepts one ``X``, as ``model_create_multi`` does.

    ``BaseModel.create`` is declared with a list, but its decorator wraps a single dict
    into a list at runtime, and most code passes one dict.
    """
    signature = ctx.default_signature
    receiver = get_proper_type(ctx.type)
    if (
        not isinstance(signature, CallableType)
        or not isinstance(receiver, Instance)
        or not receiver.type.has_base(base_model)
        or not signature.arg_types
    ):
        return signature
    vals = get_proper_type(signature.arg_types[0])
    if not isinstance(vals, Instance) or vals.type.fullname != "builtins.list":
        return signature
    accepted = UnionType.make_union([vals.args[0], vals])
    return signature.copy_modified(arg_types=[accepted, *signature.arg_types[1:]])


def keep_receiver(base_model: str, ctx: MethodContext) -> Type:
    """Return the receiver from an unannotated override of a ``-> Self`` method.

    Only when the method's return type is the implicit ``Any`` of a def without
    annotations, and the receiver is a model; ``-> Any`` written out is kept.
    """
    default = get_proper_type(ctx.default_return_type)
    receiver = get_proper_type(ctx.type)
    if (
        isinstance(default, AnyType)
        and default.type_of_any == TypeOfAny.unannotated
        and isinstance(receiver, Instance)
        and receiver.type.has_base(base_model)
    ):
        return receiver
    return ctx.default_return_type


def annotate_self_overrides(ctx: ClassDefContext) -> None:
    """Type unannotated overrides of ``-> Self`` methods as ``(self: Self, ...) -> Self``.

    ``super().create(vals_list)`` in an override returns what the next class's
    ``create`` returns; when that is another unannotated override, mypy says ``Any``.
    mypy calls no plugin hook for ``super()`` calls (the callee has no full name), so
    the overrides themselves get the type the ``BaseModel`` method declares: their
    arguments stay ``Any`` as in any unannotated def. Runs after the class body.
    """
    info = ctx.cls.info
    for statement in ctx.cls.defs.body:
        func = statement.func if isinstance(statement, Decorator) else statement
        if not isinstance(func, FuncDef) or func.type is not None or func.name not in SELF_METHODS:
            continue
        if func.is_static or func.is_class or not func.arg_names:
            continue
        func.type = _self_signature(func, info, ctx.api.named_type("builtins.function"))


def _self_signature(func: FuncDef, info: TypeInfo, fallback: Instance) -> CallableType:
    if info.self_type is None:
        # As mypy's own ``SemanticAnalyzer.setup_self_type`` (id 0 marks a self type).
        info.self_type = TypeVarType(
            "Self",
            f"{info.fullname}.Self",
            id=TypeVarId(0),
            values=[],
            upper_bound=fill_typevars(info),
            default=AnyType(TypeOfAny.from_omitted_generics),
        )
    self_type = info.self_type
    others = [AnyType(TypeOfAny.unannotated)] * (len(func.arg_names) - 1)
    return CallableType(
        [self_type, *others],
        func.arg_kinds,
        func.arg_names,
        self_type,
        fallback,
        name=func.name,
        variables=[self_type],
        line=func.line,
        column=func.column,
        definition=func,
    )
