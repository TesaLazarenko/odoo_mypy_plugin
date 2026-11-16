"""Classes whose ``__new__`` builds an object of another class.

Odoo 20's ``frozendict`` is "not a real class": its ``__new__`` returns a
``MappingProxyType``. An annotation with it (``Environment.context: frozendict``) then
names a class without a single method of a mapping, so every ``env.context.get(...)``,
``env.context["key"]`` and ``dict(env.context)`` is an error, in Odoo's own code and in
every addon.

What such a class's values can do is what the built class can: when a class with no base
of its own has a ``__new__`` whose every ``return`` calls another class, the checker
takes that class for its base (:func:`inherit_built_class`, with the class's MRO). The
type arguments are passed on when both classes take the same number of them. A final
class (``MappingProxyType`` is one) becomes a base only in a module whose errors mypy does
not show, like Odoo's own: mypy reports a final base at the class.

The ``return`` statements are read from the source file: mypy drops the bodies of the
functions of modules whose errors it does not show (Odoo's own).
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path
from typing import Final

from mypy.nodes import ClassDef, FuncDef, MypyFile, Statement, TypeInfo
from mypy.plugin import ClassDefContext
from mypy.types import AnyType, CallableType, Instance, Type, TypeOfAny

from odoo_typing.classes import install_mro
from odoo_typing.hooks.models import errors_hidden
from odoo_typing.scanner import SourceError, parse_source

__all__ = ["inherit_built_class"]

NEW: Final = "__new__"
OBJECT: Final = "builtins.object"
OWN_MRO: Final = 2  # the class and ``object``


def inherit_built_class(ctx: ClassDefContext) -> None:
    """Give a class that builds another class's objects that class as its base."""
    info = ctx.cls.info
    if len(info.mro) != OWN_MRO or info.mro[1].fullname != OBJECT:
        return
    name = _built_name(ctx.cls, ctx.api.modules[ctx.api.cur_mod_id])
    if name is None or name == ctx.cls.name:
        return
    symbol = ctx.api.lookup_qualified(name, ctx.cls, suppress_errors=True)
    built = symbol.node if symbol is not None else None
    if not isinstance(built, TypeInfo) or built is info or not built.mro or info in built.mro:
        if symbol is not None and not ctx.api.final_iteration and not isinstance(built, TypeInfo):
            ctx.api.defer()  # an import mypy has not bound yet
        return
    if built.is_final and not errors_hidden(ctx.api):
        return  # mypy would report the base at the class, in a module whose errors it shows
    variables = info.defn.type_vars
    arguments: list[Type] = (
        list(variables)
        if len(variables) == len(built.defn.type_vars)
        else [AnyType(TypeOfAny.special_form) for _ in built.defn.type_vars]
    )
    install_mro(info, [info, *built.mro], [Instance(built, arguments)], partial=False)


def _built_name(defn: ClassDef, tree: MypyFile) -> str | None:
    """The name every ``return`` of a class's ``__new__`` calls (``return Other(...)``).

    Only for a ``__new__`` of a source file that declares no return type: a stub has no
    bodies, and a declared type is what mypy goes by.
    """
    if tree.is_stub or not any(_is_untyped_new(item) for item in defn.defs.body):
        return None
    names = _built_names(tree.path).get((defn.name, defn.line), frozenset())
    return next(iter(names)) if len(names) == 1 else None


def _is_untyped_new(statement: Statement) -> bool:
    if not isinstance(statement, FuncDef) or statement.name != NEW:
        return False
    declared = statement.unanalyzed_type
    return not isinstance(declared, CallableType) or isinstance(declared.ret_type, AnyType)


@lru_cache(maxsize=8)
def _built_names(path: str) -> dict[tuple[str, int], frozenset[str | None]]:
    """What the ``__new__`` of each class of a file returns, by class name and line."""
    try:
        tree = parse_source(Path(path))
    except SourceError:
        return {}
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    return {
        (node.name, node.lineno): frozenset(_called(item.value) for item in _returns(function))
        for node in classes
        for function in node.body
        if isinstance(function, ast.FunctionDef) and function.name == NEW
    }


def _returns(function: ast.FunctionDef) -> list[ast.Return]:
    """The ``return`` statements of a function itself, not of the functions inside it."""
    found: list[ast.Return] = []
    pending: list[ast.AST] = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Return):
            found.append(node)
        elif not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            pending.extend(ast.iter_child_nodes(node))
    return found


def _called(value: ast.expr | None) -> str | None:
    """The plain name a returned expression calls; ``None`` for anything else."""
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        return value.func.id
    return None
