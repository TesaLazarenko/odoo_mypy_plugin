"""Which classes are field classes: what a name is bound to, and what a class derives from.

A field is declared by calling a field class in a model's class body. The class is found
the way Python finds it, not by how the call is spelled:

- An *origin* is the dotted name a file binds a name to: ``fields`` of
  ``from odoo import fields`` is ``odoo.fields``, ``BigInteger`` of
  ``from odoo.addons.my.fields import BigInteger`` is ``odoo.addons.my.fields.BigInteger``,
  a class of the file is ``<module>.<Class>`` (:func:`origin_of`).
- Odoo's own field classes are the ones ``odoo.fields`` has (:func:`odoo_class`).
- Any other class is a field class if a base of it is: :func:`field_kind` follows the
  bases (:class:`~odoo_typing.scanner.results.FieldClass`, kept in the scan of the file
  that defines the class) to the Odoo class it derives from.
- A function that returns a call of a field class in each ``return``, and
  ``functools.partial(<field class>, ...)``, make fields of that class too: they are
  described like a class that derives from it (:func:`factory_class`,
  :func:`partial_class`).

A subclass has the arguments and the values of the Odoo class it derives from, unless it
says otherwise: one that defines ``__init__`` (and a factory) takes positional arguments
of its own, and one that defines a ``convert_to_*`` method has values of its own (its
fields are then treated like those of any class the plugin does not know).
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Final

from odoo_typing.scanner.results import FieldClass
from odoo_typing.scanner.syntax import RELATIONAL_FIELDS, executed_blocks

__all__ = [
    "FieldKind",
    "Origin",
    "bind_assignment",
    "bind_import",
    "bind_import_from",
    "class_bases",
    "describe_source",
    "factory_class",
    "field_kind",
    "odoo_class",
    "origin_of",
    "partial_class",
]

ODOO_FIELDS: Final = "odoo.fields."
PARTIAL: Final = "functools.partial"
CONVERSION_PREFIX: Final = "convert_to_"

# The dotted name an expression of a file names, if the file binds it.
Origin = Callable[[ast.expr], str | None]


@dataclass(frozen=True, slots=True)
class FieldKind:
    """What a field class is, as far as its bases tell.

    Attributes:
        name: Name of the class.
        base: The Odoo field class it is or derives from (``Integer``).
        converts: Whether it (or a base before the Odoo class) converts values itself.
        arguments: Whether it (or such a base) defines ``__init__``.
    """

    name: str
    base: str
    converts: bool = False
    arguments: bool = False

    @property
    def field_type(self) -> str:
        """The class the fields of this class are checked as (``FieldInfo.field_type``).

        The Odoo class, unless the class converts values itself: then its own name, which
        no check knows. A relational class keeps its comodel either way.
        """
        return self.name if self.converts and self.base not in RELATIONAL_FIELDS else self.base


def origin_of(node: ast.expr, origins: Mapping[str, str]) -> str | None:
    """The dotted name an expression names: ``fields.Char``, ``Field[int]``, ``BigInteger``."""
    if isinstance(node, ast.Name):
        return origins.get(node.id)
    if isinstance(node, ast.Attribute):
        owner = origin_of(node.value, origins)
        return f"{owner}.{node.attr}" if owner is not None else None
    if isinstance(node, ast.Subscript):
        return origin_of(node.value, origins)
    return None


def odoo_class(origin: str) -> str | None:
    """The name of one of Odoo's field classes: ``Char`` of ``odoo.fields.Char``."""
    if not origin.startswith(ODOO_FIELDS):
        return None
    name = origin[len(ODOO_FIELDS) :]
    return name if "." not in name and name[:1].isupper() else None


def class_bases(
    node: ast.ClassDef,
    origin: Origin,
    known: Mapping[str, FieldClass],
    linkable: Callable[[str], bool],
) -> FieldClass | None:
    """What a class derives from, if that may make it a field class.

    A base that is a known class of the same addon is replaced by what that one derives
    from, so the result names only Odoo's classes and classes other scans describe.

    Args:
        node: The class.
        origin: The dotted name an expression of the file names (see :func:`origin_of`).
        known: The classes of the addon described so far, by dotted name.
        linkable: Whether a dotted name may be a class that another scan describes.

    Returns:
        ``None`` for a class with no such base.
    """
    methods = {item.name for item in node.body if isinstance(item, ast.FunctionDef)}
    return _derived(
        node.name,
        (origin(base) for base in node.bases),
        known,
        linkable,
        converts=any(name.startswith(CONVERSION_PREFIX) for name in methods),
        arguments="__init__" in methods,
    )


def factory_class(
    node: ast.FunctionDef,
    origin: Origin,
    known: Mapping[str, FieldClass],
    linkable: Callable[[str], bool],
) -> FieldClass | None:
    """A function described as the classes it returns calls of (see :func:`class_bases`).

    ``def money(**kwargs): return fields.Float(digits=(16, 2), **kwargs)`` makes fields
    of ``Float``. Only a function whose every ``return`` is a call of a name the file
    binds; what the function itself passes to the class is not read, so the positional
    arguments of its fields are not either.
    """
    returned = [item.value for item in _returns(node.body)]
    if not returned or not all(isinstance(value, ast.Call) for value in returned):
        return None
    called = [origin(value.func) for value in returned if isinstance(value, ast.Call)]
    if None in called:
        return None
    return _derived(node.name, called, known, linkable, converts=False, arguments=True)


def partial_class(
    name: str,
    value: ast.expr,
    origin: Origin,
    known: Mapping[str, FieldClass],
    linkable: Callable[[str], bool],
) -> FieldClass | None:
    """``name = functools.partial(<class>, ...)`` described as that class (a factory)."""
    if not isinstance(value, ast.Call) or not value.args or origin(value.func) != PARTIAL:
        return None
    return _derived(name, [origin(value.args[0])], known, linkable, converts=False, arguments=True)


def _derived(
    name: str,
    named: Iterable[str | None],
    known: Mapping[str, FieldClass],
    linkable: Callable[[str], bool],
    *,
    converts: bool,
    arguments: bool,
) -> FieldClass | None:
    bases: dict[str, None] = {}
    for base in named:
        if base is None:
            continue
        inner = known.get(base)
        if inner is not None:
            bases.update(dict.fromkeys(inner.bases))
            converts = converts or inner.converts
            arguments = arguments or inner.arguments
        elif odoo_class(base) is not None or linkable(base):
            bases[base] = None
    return FieldClass(name, tuple(bases), converts, arguments) if bases else None


def _returns(body: list[ast.stmt]) -> Iterator[ast.Return]:
    """The ``return`` statements of a function body (not those of nested functions)."""
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Return):
            yield node
        elif not isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda
        ):
            pending.extend(ast.iter_child_nodes(node))


def field_kind(
    origin: str, lookup: Callable[[str], FieldClass | None], seen: frozenset[str] = frozenset()
) -> FieldKind | None:
    """The kind of the class an origin names; ``None`` if it is not a field class.

    Args:
        origin: Dotted name of the class.
        lookup: The description of a class by its dotted name, if a scan has one.
        seen: The classes being resolved (bases that name each other).
    """
    name = odoo_class(origin)
    if name is not None:
        return FieldKind(name, name)
    described = lookup(origin) if origin not in seen else None
    if described is None:
        return None
    for base in described.bases:
        kind = field_kind(base, lookup, seen | {origin})
        if kind is not None:
            return FieldKind(
                described.name,
                kind.base,
                described.converts or kind.converts,
                described.arguments or kind.arguments,
            )
    return None


def describe_source(
    tree: ast.Module, module: str, package: str
) -> tuple[dict[str, FieldClass], dict[str, str]]:
    """The classes of a module read on its own, and what its other names are bound to.

    For a module outside the addons (Odoo's own, a library): no scan describes it, so
    the registry reads its source when a field class is traced into it.

    Args:
        tree: The module's syntax tree.
        module: Its dotted name.
        package: The package its relative imports are relative to.

    Returns:
        The classes (and factories) that may be field classes by name, and the dotted
        names that the module's names are bound to.
    """
    origins: dict[str, str] = {}
    classes: dict[str, FieldClass] = {}
    # No class of the module stands for its bases here: each keeps the bases it names,
    # so that a class can be recognized by its dotted name.
    known: dict[str, FieldClass] = {}

    def origin(node: ast.expr) -> str | None:
        return origin_of(node, origins)

    def describe(name: str, found: FieldClass | None) -> None:
        origins[name] = f"{module}.{name}"
        if found is not None:
            classes[name] = found

    def walk(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, ast.ImportFrom):
                bind_import_from(node, package, origins)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    bind_import(alias, origins)
            elif isinstance(node, ast.ClassDef):
                if node.name not in origins or origins[node.name] != f"{module}.{node.name}":
                    describe(node.name, class_bases(node, origin, known, _anything))
            elif isinstance(node, ast.FunctionDef):
                describe(node.name, factory_class(node, origin, known, _anything))
            elif isinstance(node, ast.Assign):
                bind_assignment(node, origins, origin, known, _anything, describe)
            else:
                for block in executed_blocks(node):
                    walk(block)

    walk(tree.body)
    return classes, origins


def _anything(_origin: str) -> bool:
    return True


def bind_import_from(node: ast.ImportFrom, package: str, origins: dict[str, str]) -> None:
    """Bind the names of ``from a.b import c`` (also relative to a package)."""
    parts = package.split(".") if node.level else []
    dotted = parts[: len(parts) - node.level + 1] if node.level else []
    dotted += node.module.split(".") if node.module else []
    for alias in node.names:
        if alias.name != "*":
            origins[alias.asname or alias.name] = ".".join([*dotted, alias.name])


def bind_import(alias: ast.alias, origins: dict[str, str]) -> None:
    """Bind what ``import a.b`` (the name ``a``) or ``import a.b as c`` names."""
    if alias.asname is not None:
        origins[alias.asname] = alias.name
    else:
        top = alias.name.partition(".")[0]
        origins[top] = top


def bind_assignment(
    node: ast.Assign,
    origins: dict[str, str],
    origin: Origin,
    known: Mapping[str, FieldClass],
    linkable: Callable[[str], bool],
    describe: Callable[[str, FieldClass | None], None],
) -> None:
    """Follow ``Alias = Class`` and ``factory = partial(Class, ...)``.

    A name bound to anything else is forgotten: it no longer names what it did.
    """
    named = origin(node.value) if isinstance(node.value, ast.Name | ast.Attribute) else None
    for target in node.targets:
        if not isinstance(target, ast.Name):
            continue
        made = partial_class(target.id, node.value, origin, known, linkable)
        if made is not None:
            describe(target.id, made)
        elif named is not None:
            origins[target.id] = named
        else:
            origins.pop(target.id, None)
