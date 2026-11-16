"""Records where a parameter takes an abstract class Odoo registers ``BaseModel`` with.

Odoo's models module ends with ``collections.abc.Set.register(BaseModel)`` and
``collections.abc.Sequence.register(BaseModel)``: at run time records are a ``Sequence``
and a ``Set`` (``isinstance(records, Sequence)``), so ``random.sample(products, 2)``
works. mypy does not follow ``register``: records were no ``Sequence[T]``, and every such
call was an ``arg-type`` error. :func:`registered_classes` reads which classes the
module registers ``BaseModel`` with; a parameter of one of them takes records whose
items it takes: ``Sequence[T]`` with ``T`` the records (iterating records gives
records; ``random.sample(products, 2)`` is ``list[ProductProduct]``), ``Sequence[X]``
records of a class of ``X``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from mypy.expandtype import expand_type
from mypy.lookup import lookup_fully_qualified
from mypy.nodes import Decorator, FuncDef, MypyFile, OverloadedFuncDef, TypeInfo, Var
from mypy.plugin import FunctionSigContext, MethodSigContext
from mypy.subtypes import is_subtype
from mypy.types import (
    CallableType,
    FunctionLike,
    Instance,
    ProperType,
    Type,
    TypeVarType,
    get_proper_type,
)

# isort: split
# After mypy.types: these modules import each other at import time.
from mypy.checker import TypeChecker

from odoo_typing.scanner import SourceError, parse_source

__all__ = ["has_registered_parameter", "records_signature", "registered_classes"]

REGISTER: Final = "register"


def registered_classes(
    module: MypyFile, base_model: str, modules: dict[str, MypyFile]
) -> frozenset[str]:
    """The classes Odoo's models module registers ``BaseModel`` with, by full name.

    Read from the module's source (a cached tree has no statements): its top-level
    ``<class>.register(BaseModel)`` calls, the class named as the module imports it
    (``collections.abc.Sequence`` is ``typing.Sequence``).
    """
    try:
        tree = parse_source(Path(module.path))
    except SourceError:
        return frozenset()
    base_name = base_model.rpartition(".")[2]
    found: set[str] = set()
    for statement in tree.body:
        call = statement.value if isinstance(statement, ast.Expr) else None
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        argument = call.args[0] if len(call.args) == 1 else None
        if call.func.attr != REGISTER or not isinstance(argument, ast.Name):
            continue
        dotted = _dotted(call.func.value)
        if argument.id != base_name or dotted is None:
            continue
        symbol = lookup_fully_qualified(dotted, modules, raise_on_missing=False)
        if symbol is not None and isinstance(symbol.node, TypeInfo):
            found.add(symbol.node.fullname)
    return frozenset(found)


def _dotted(node: ast.expr) -> str | None:
    """``collections.abc.Sequence`` of an ``Attribute`` / ``Name`` chain."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id, *reversed(parts)])


def has_registered_parameter(node: object, registered: Iterable[str]) -> bool:
    """Whether a function or a callable variable has a parameter of a registered class."""
    names = frozenset(registered)
    if isinstance(node, Decorator):
        node = node.func
    types: list[Type | None] = []
    if isinstance(node, (FuncDef, Var)):
        types.append(node.type)
    elif isinstance(node, OverloadedFuncDef):
        for item in node.items:
            types.append(item.func.type if isinstance(item, Decorator) else item.type)
    for typ in types:
        proper = get_proper_type(typ) if typ is not None else None
        if isinstance(proper, CallableType) and any(
            _registered(get_proper_type(argument), names) is not None
            for argument in proper.arg_types
        ):
            return True
    return False


def _registered(typ: ProperType, names: frozenset[str]) -> ProperType | None:
    """The item type of ``Sequence[X]`` for a registered class, if the type is one."""
    if not isinstance(typ, Instance) or typ.type.fullname not in names or len(typ.args) != 1:
        return None
    return get_proper_type(typ.args[0])


def records_signature(
    registered: frozenset[str], base_model: str, ctx: FunctionSigContext | MethodSigContext
) -> FunctionLike:
    """A parameter ``Sequence[T]`` given records takes them, with ``T`` the records."""
    signature = ctx.default_signature
    checker = ctx.api
    if not isinstance(signature, CallableType) or not isinstance(checker, TypeChecker):
        return signature
    for position, declared in enumerate(signature.arg_types):
        item = _registered(get_proper_type(declared), registered)
        given = ctx.args[position] if position < len(ctx.args) else []
        if item is None or len(given) != 1:
            continue
        with checker.msg.filter_errors(), checker.local_type_map:
            found = get_proper_type(checker.get_expression_type(given[0]))
        if not isinstance(found, Instance) or not found.type.has_base(base_model):
            continue
        if isinstance(item, TypeVarType):
            expanded = get_proper_type(expand_type(signature, {item.id: found}))
            if not isinstance(expanded, CallableType):
                continue
            variables = [other for other in expanded.variables if other.id != item.id]
            signature = expanded.copy_modified(variables=variables)
        elif not is_subtype(found, item):
            continue
        arguments = list(signature.arg_types)
        arguments[position] = found
        signature = signature.copy_modified(arg_types=arguments)
    return signature
