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

A subclass has the arguments and the values of the Odoo class it derives from, unless it
says otherwise: one that defines ``__init__`` takes positional arguments of its own, and
one that defines a ``convert_to_*`` method has values of its own (its fields are then
treated like those of any class the plugin does not know).
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from odoo_typing.scanner.results import FieldClass
from odoo_typing.scanner.syntax import RELATIONAL_FIELDS

__all__ = [
    "POSITIONAL",
    "FieldKind",
    "class_bases",
    "field_kind",
    "odoo_class",
    "origin_of",
]

ODOO_FIELDS: Final = "odoo.fields."
# In ``FieldInfo.dynamic``: the positional arguments of the call were not read (the class
# is not known yet, or has an ``__init__`` of its own).
POSITIONAL: Final = "<positional>"
CONVERSION_PREFIX: Final = "convert_to_"


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
    origin: Callable[[ast.expr], str | None],
    known: Mapping[str, FieldClass],
    linkable: Callable[[str], bool],
) -> FieldClass | None:
    """What a class derives from, if that may make it a field class.

    A base that is a known class of the same addon is replaced by what that one derives
    from, so the result names only Odoo's classes and classes of other addons.

    Args:
        node: The class.
        origin: The dotted name an expression of the file names (see :func:`origin_of`).
        known: The classes of the addon described so far, by dotted name.
        linkable: Whether a dotted name may be a class that another scan describes.

    Returns:
        ``None`` for a class with no such base.
    """
    bases: dict[str, None] = {}
    methods = {item.name for item in node.body if isinstance(item, ast.FunctionDef)}
    converts = any(name.startswith(CONVERSION_PREFIX) for name in methods)
    arguments = "__init__" in methods
    for base in node.bases:
        named = origin(base)
        if named is None:
            continue
        inner = known.get(named)
        if inner is not None:
            bases.update(dict.fromkeys(inner.bases))
            converts = converts or inner.converts
            arguments = arguments or inner.arguments
        elif odoo_class(named) is not None or linkable(named):
            bases[named] = None
    if not bases:
        return None
    return FieldClass(node.name, tuple(bases), converts, arguments)


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
