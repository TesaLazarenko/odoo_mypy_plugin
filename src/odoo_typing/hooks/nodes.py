"""What the hooks look up in mypy: classes in its modules, arguments of the checked call."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from mypy.lookup import lookup_fully_qualified
from mypy.nodes import (
    ARG_POS,
    CallExpr,
    Expression,
    ListExpr,
    MypyFile,
    NameExpr,
    PlaceholderNode,
    StrExpr,
    TupleExpr,
    TypeInfo,
)
from mypy.plugin import CheckerPluginInterface, ClassDefContext, MethodContext, MethodSigContext
from mypy.types import Instance, LiteralType, ProperType, Type, get_proper_type

# isort: split
# After mypy.types: these modules import each other at import time.
from mypy.checker import TypeChecker
from mypy.semanal import SemanticAnalyzer

from odoo_typing.scanner import POSITIONAL_PARAMETERS, Contribution

__all__ = [
    "Found",
    "Incomplete",
    "call_argument",
    "class_infos",
    "field_arguments",
    "field_sources",
    "imported_view_class",
    "incomplete_modules",
    "is_false",
    "literal_string",
    "literal_strings_argument",
    "lookup_class",
    "record_type",
    "type_checker",
]

Incomplete = Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class Found:
    """Classes looked up in mypy's modules.

    Attributes:
        infos: The classes found, in the order asked.
        pending: Some class's module is part of this run but the class is not
            analyzed yet.
        missing: Some class was not found (pending, or its module is not loaded).
    """

    infos: list[TypeInfo]
    pending: bool
    missing: bool


def class_infos(
    contributions: Iterable[Contribution],
    modules: dict[str, MypyFile],
    incomplete: Incomplete = lambda module: False,
) -> Found:
    """Find the mypy classes of contributions (top-level classes of their modules).

    A class absent from a module that is completely analyzed does not exist for mypy
    (an unreachable ``if sys.version_info < ...`` branch): it is neither pending nor
    missing. ``incomplete`` tells which modules are still being analyzed (none, once
    type checking runs). A class whose definition mypy has not finished (a placeholder
    in its module's names: a deferred class of another module of the same import
    cycle, as Odoo 17's ``base.models.res_partner`` for ``base.populate``) is pending
    even when its module no longer counts as incomplete.
    """
    infos: list[TypeInfo] = []
    pending = missing = False
    for item in contributions:
        tree = modules.get(item.module)
        node = tree.names.get(item.class_name) if tree is not None else None
        if node is not None and isinstance(node.node, TypeInfo):
            infos.append(node.node)
        elif tree is None:
            missing = True
        elif incomplete(item.module) or (
            node is not None and isinstance(node.node, PlaceholderNode)
        ):
            pending = missing = True
    return Found(infos=infos, pending=pending, missing=missing)


def incomplete_modules(ctx: ClassDefContext) -> Incomplete:
    """Which modules semantic analysis has not finished yet (all, if unknown)."""
    if isinstance(ctx.api, SemanticAnalyzer):
        return ctx.api.is_incomplete_namespace
    return lambda module: True


def field_sources(
    classes: Iterable[Contribution],
    modules: dict[str, MypyFile],
    incomplete: Incomplete = lambda module: False,
) -> list[tuple[TypeInfo, tuple[str, ...]]] | None:
    """The mypy classes of contributions, each with the fields it declares.

    Returns:
        ``None`` if some class's module is part of this run but the class is not
        analyzed yet; classes of modules outside the run are left out.
    """
    sources: list[tuple[TypeInfo, tuple[str, ...]]] = []
    for item in classes:
        found = class_infos([item], modules, incomplete)
        if found.pending:
            return None
        sources += [(info, item.field_names) for info in found.infos]
    return sources


def imported_view_class(
    name: str, tree: MypyFile, modules: dict[str, MypyFile], in_addon: Callable[[str], object]
) -> TypeInfo | None:
    """A merged view class of a module (``tree``) or of an addon module it imports.

    One class per model and addon would be best, but a class stored in a module that is
    not loaded with the user could not be read back from the cache. A module that the
    user imports from is always loaded with it, and values (``RECS = env["x"]``) can
    only reach the user through such imports; so the class of such a module is reused.
    """
    seen = {tree.fullname}
    stack = [tree]
    while stack:
        current = stack.pop()
        node = current.names.get(name)
        if node is not None and isinstance(node.node, TypeInfo):
            return node.node
        for module in _referenced_modules(current, modules) - seen:
            seen.add(module)
            if in_addon(module):
                stack.append(modules[module])
    return None


def _referenced_modules(tree: MypyFile, modules: dict[str, MypyFile]) -> set[str]:
    """Modules whose symbols a module's symbol table refers to (its imports, also cached)."""
    found: set[str] = set()
    for entry in tree.names.values():
        node = entry.node
        if isinstance(node, MypyFile):
            found.add(node.fullname)
        elif node is not None:
            found.add(node.fullname.rpartition(".")[0])
    found.discard(tree.fullname)
    return {module for module in found if module in modules}


def lookup_class(modules: dict[str, MypyFile], fullname: str) -> TypeInfo | None:
    """The class with a full name, if mypy has it."""
    node = lookup_fully_qualified(fullname, modules)
    return node.node if node is not None and isinstance(node.node, TypeInfo) else None


def type_checker(api: CheckerPluginInterface) -> TypeChecker:
    """Return mypy's checker behind the plugin interface.

    The interface does not expose the module being checked, which merged view
    classes must be stored in (see :mod:`odoo_typing.classes`); mypy's checker does.
    """
    if not isinstance(api, TypeChecker):
        raise TypeError(f"unexpected checker plugin interface: {type(api).__name__}")
    return api


def record_type(ctx: MethodSigContext) -> ProperType | None:
    """Type of the records a descriptor is read on (``NoneType`` for ``Model.field``)."""
    if not ctx.args or not ctx.args[0]:
        return None
    return get_proper_type(ctx.api.get_expression_type(ctx.args[0][0]))


def field_arguments(call: CallExpr, field_class: str) -> list[tuple[str, Expression]]:
    """The arguments of a field definition by parameter name (positional ones too)."""
    positional = POSITIONAL_PARAMETERS.get(field_class, ("string",))
    found: list[tuple[str, Expression]] = []
    position = 0
    for name, value, kind in zip(call.arg_names, call.args, call.arg_kinds, strict=True):
        if name is not None:
            found.append((name, value))
        elif kind == ARG_POS:
            if position < len(positional):
                found.append((positional[position], value))
            position += 1
    return found


def call_argument(ctx: MethodContext, name: str, position: int) -> Expression | None:
    """The expression passed for a parameter, by name, else by position (``-1``: none)."""
    if name in ctx.callee_arg_names:
        index = ctx.callee_arg_names.index(name)
    elif position >= 0:
        index = position
    else:
        return None
    return ctx.args[index][0] if len(ctx.args) > index and ctx.args[index] else None


def is_false(node: Expression | None) -> bool:
    """Whether an argument is the literal ``False``."""
    return isinstance(node, NameExpr) and node.name == "False"


def literal_strings_argument(ctx: MethodContext, name: str, position: int) -> list[StrExpr] | None:
    """A literal list (or tuple) of strings passed for a parameter, if one is."""
    value = call_argument(ctx, name, position)
    if not isinstance(value, (ListExpr, TupleExpr)) or not value.items:
        return None
    items = [item for item in value.items if isinstance(item, StrExpr)]
    return items if len(items) == len(value.items) else None


def literal_string(node: Expression, node_type: Type) -> StrExpr | None:
    """A string argument: a literal, or a ``Final`` / ``Literal`` str (``FIELD: Final = "a"``).

    A string taken from the type is a new node placed on the argument, so messages about it
    point there.
    """
    if isinstance(node, StrExpr):
        return node
    proper = get_proper_type(node_type)
    if isinstance(proper, Instance) and proper.last_known_value is not None:
        proper = proper.last_known_value
    if not isinstance(proper, LiteralType) or not isinstance(proper.value, str):
        return None
    found = StrExpr(proper.value)
    found.set_line(node)
    return found
