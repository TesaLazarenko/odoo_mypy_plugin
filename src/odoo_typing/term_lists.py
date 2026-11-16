"""Lists of domain terms that grow: ``domain.append(("company_id", "in", companies.ids))``.

mypy infers the type of a list from its first value: ``[("groups_id", "=", group.id)]`` is a
``list[tuple[str, str, int]]``, and a term added later with another kind of value (a list
of ids, a date) is an ``arg-type`` error although the code is a correct domain. The terms
of a domain have a value of any type, so the methods that add to a list of terms take any
term: ``append``, ``insert``, ``extend``, ``+=`` and ``+`` of a list whose items are
``(field, operator, value)`` tuples also take such a tuple with another value, or an
operator (``"|"``), or Odoo's ``TRUE_LEAF`` / ``FALSE_LEAF`` (``(0, "=", 1)``). The list
keeps its type; what the terms name is checked where the
domain is used (:mod:`odoo_typing.hooks.domains`).

The same goes for a list of the commands of a relational field: ``[Command.update(...)]``
is a list of ``tuple[Command, int, ...]`` (Odoo 19 and 20 type ``Command`` as an ``int``
enumeration), and ``commands.append((2, line.id))``, the plain tuple Odoo takes as well,
was an ``arg-type`` error.
"""

from __future__ import annotations

from typing import Final, TypeGuard

from mypy.plugin import MethodSigContext
from mypy.subtypes import is_same_type
from mypy.types import (
    AnyType,
    CallableType,
    FunctionLike,
    Instance,
    LiteralType,
    ProperType,
    TupleType,
    Type,
    TypeOfAny,
    UnionType,
    get_proper_type,
)

__all__ = ["is_term_list_method", "term_list_signature"]

LIST: Final = "builtins.list"
STR: Final = "builtins.str"
INT: Final = "builtins.int"
TUPLE: Final = "builtins.tuple"
# The methods of ``list`` that add items (``+`` gives a list of the same type).
ADDING_METHODS: Final = frozenset({"append", "insert", "extend", "__iadd__", "__add__"})
TERM_LENGTH: Final = 3


def is_term_list_method(fullname: str) -> bool:
    """Whether a method adds items to a ``list`` (see the module)."""
    owner, _, name = fullname.rpartition(".")
    return owner == LIST and name in ADDING_METHODS


def term_list_signature(ctx: MethodSigContext) -> FunctionLike:
    """Let a list of ``(field, operator, value)`` terms take a term with any value."""
    signature = ctx.default_signature
    receiver = get_proper_type(ctx.type)
    if (
        not isinstance(signature, CallableType)
        or not isinstance(receiver, Instance)
        or len(receiver.args) != 1
    ):
        return signature
    item = get_proper_type(receiver.args[0])
    value = AnyType(TypeOfAny.special_form)
    added: Type
    if _is_term(item):
        term = item.copy_modified(items=[item.items[0], item.items[1], value])
        # Odoo's TRUE_LEAF / FALSE_LEAF, ``(1, "=", 1)`` / ``(0, "=", 1)``: a term too.
        leaf = item.copy_modified(items=[ctx.api.named_generic_type(INT, []), item.items[1], value])
        added = UnionType([term, leaf, ctx.api.named_generic_type(STR, [])])
    elif _is_command(item):
        added = ctx.api.named_generic_type(TUPLE, [value])  # ``(2, id)``, ``(5,)``
    else:
        return signature
    arg_types = [_taking(declared, item, added) for declared in signature.arg_types]
    return signature.copy_modified(arg_types=arg_types)


def _is_command(item: ProperType) -> bool:
    """Whether a type is a tuple that starts with a member of an ``int`` enumeration.

    ``Command.create(vals)`` is a ``tuple[Command, int, ...]``: a command of a
    relational field. Odoo takes the plain tuples too (``(0, 0, vals)``, ``(2, id)``),
    and a list that starts with one kind is filled with the other.
    """
    if not isinstance(item, TupleType) or not item.items:
        return False
    first = get_proper_type(item.items[0])
    if isinstance(first, LiteralType):
        first = first.fallback
    return isinstance(first, Instance) and first.type.is_enum and first.type.has_base(INT)


def _is_term(item: ProperType) -> TypeGuard[TupleType]:
    """Whether a type is a tuple of a field name, an operator and a value."""
    return (
        isinstance(item, TupleType)
        and len(item.items) == TERM_LENGTH
        and all(_is_str(get_proper_type(part)) for part in item.items[:2])
    )


def _is_str(part: ProperType) -> bool:
    if isinstance(part, LiteralType):
        part = part.fallback
    return isinstance(part, Instance) and part.type.fullname == STR


def _taking(declared: Type, item: ProperType, added: Type) -> Type:
    """A parameter type with the list's item type replaced (``T``, ``Iterable[T]``)."""
    proper = get_proper_type(declared)
    if is_same_type(proper, item):
        return added
    if isinstance(proper, Instance) and any(is_same_type(arg, item) for arg in proper.args):
        args = [added if is_same_type(arg, item) else arg for arg in proper.args]
        return proper.copy_modified(args=args)
    return declared
