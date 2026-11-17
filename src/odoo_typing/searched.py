"""Fields a search domain makes set on every record it finds.

``search_read([("product_id", "in", subquery), ...], fields=["product_id"])`` gives no row
whose ``product_id`` is empty, and ``search([("email_from", "ilike", "@team1")])`` no
record whose ``email_from`` is: ``IN`` and ``ILIKE`` match no ``NULL``. A term of the
domain's top-level conjunction (not under ``|`` or ``!``) on a field of the model itself
(no path) makes the field set when it is:

* ``=`` / ``child_of`` / ``parent_of`` with a literal that is true;
* ``in`` with a display of literals that are all true, or with a value whose type holds
  no false item (a ``Query``, records, ``list[int]``);
* ``!=`` with ``False`` / ``None``;
* ``like`` / ``ilike`` / ``=like`` / ``=ilike`` with a string literal that is not empty.

A read of such a field from the result of ``read`` / ``search_read``, from a local bound
once to ``search(...)`` / ``search_fetch(...)``, or from the variable of a ``for`` over
such a local, has no ``False``.
"""

from __future__ import annotations

from collections.abc import Callable

from mypy.nodes import (
    ARG_POS,
    CallExpr,
    Expression,
    ForStmt,
    FuncItem,
    IntExpr,
    ListExpr,
    MemberExpr,
    NameExpr,
    StrExpr,
    TupleExpr,
    Var,
)
from mypy.types import Instance, Type, get_proper_type

from odoo_typing.rebound import function_bindings, nested_blocks

__all__ = ["SEARCH_METHODS", "domain_argument", "restricted_fields", "searched_domain"]

SEARCH_METHODS = frozenset({"search", "search_fetch"})
EQUAL = frozenset({"=", "==", "child_of", "parent_of"})
LIKE = frozenset({"like", "ilike", "=like", "=ilike"})
EMPTY_NAMES = frozenset({"builtins.False", "builtins.None"})
TERM_SIZE = 3


def domain_argument(call: CallExpr) -> Expression | None:
    """The domain a call passes: its first positional argument or ``domain=``."""
    for argument, kind, name in zip(call.args, call.arg_kinds, call.arg_names, strict=True):
        if name == "domain" or (name is None and kind == ARG_POS):
            return argument
    return None


def restricted_fields(
    domain: Expression | None, type_of: Callable[[Expression], Type | None]
) -> frozenset[str]:
    """The fields a literal domain makes set on every record it finds (see the module)."""
    if not isinstance(domain, ListExpr | TupleExpr):
        return frozenset()
    found: set[str] = set()
    for term in _conjuncts(list(domain.items)):
        field, operator, value = term
        if not isinstance(field, StrExpr) or not isinstance(operator, StrExpr):
            continue
        if "." in field.value:
            continue  # a path: through a relational field, other records
        if _sets(operator.value, value, type_of):
            found.add(field.value)
    return frozenset(found)


def searched_domain(function: FuncItem, receiver: Expression) -> Expression | None:
    """The literal domain of the search that gave the records a name holds, if one did.

    A local bound once to ``records.search(domain, ...)``, or the variable of a ``for``
    over such a local.
    """
    if not isinstance(receiver, NameExpr) or not isinstance(receiver.node, Var):
        return None
    bindings = function_bindings(function).names
    names = bindings.get(receiver.node, [])
    if len(names) != 1:
        return None
    _, value = names[0]
    if value is None:  # a ``for`` target: the records iterated
        iterated = _iterated(function, receiver.node)
        if not isinstance(iterated, NameExpr) or not isinstance(iterated.node, Var):
            return None
        source = bindings.get(iterated.node, [])
        value = source[0][1] if len(source) == 1 else None
    if (
        isinstance(value, CallExpr)
        and isinstance(value.callee, MemberExpr)
        and value.callee.name in SEARCH_METHODS
    ):
        return domain_argument(value)
    return None


def _iterated(function: FuncItem, variable: Var) -> Expression | None:
    """What the ``for`` statement that binds a variable iterates."""
    pending = [function.body]
    while pending:
        block = pending.pop()
        for statement in block.body:
            if (
                isinstance(statement, ForStmt)
                and isinstance(statement.index, NameExpr)
                and statement.index.node is variable
            ):
                return statement.expr
            pending.extend(nested_blocks(statement))
    return None


def _conjuncts(items: list[Expression]) -> list[tuple[Expression, ...]]:
    """The terms of a prefix domain's top-level conjunction (``&`` / implicit and)."""
    position = 0

    def parse() -> list[tuple[Expression, ...]] | None:
        nonlocal position
        if position >= len(items):
            return None
        item = items[position]
        position += 1
        if isinstance(item, StrExpr) and item.value in {"&", "|", "!"}:
            arity = 1 if item.value == "!" else 2
            parts = [parse() for _ in range(arity)]
            if any(part is None for part in parts):
                return None
            if item.value == "&":
                return [term for part in parts if part is not None for term in part]
            return []  # nothing holds for each record under ``|`` or ``!``
        if isinstance(item, TupleExpr | ListExpr) and len(item.items) == TERM_SIZE:
            return [tuple(item.items)]
        return []  # a leaf the scan cannot read: holds nothing it can use

    found: list[tuple[Expression, ...]] = []
    while position < len(items):
        part = parse()
        if part is None:
            return []
        found.extend(part)
    return found


def _sets(operator: str, value: Expression, type_of: Callable[[Expression], Type | None]) -> bool:
    if operator in EQUAL:
        return _true_literal(value)
    if operator == "!=":
        return isinstance(value, NameExpr) and value.fullname in EMPTY_NAMES
    if operator in LIKE:
        return isinstance(value, StrExpr) and bool(value.value)
    if operator == "in":
        if isinstance(value, ListExpr | TupleExpr):
            return bool(value.items) and all(_true_literal(item) for item in value.items)
        return _holds_no_false(type_of(value))
    return False


def _true_literal(value: Expression) -> bool:
    if isinstance(value, StrExpr | IntExpr):
        return bool(value.value)
    return isinstance(value, NameExpr) and value.fullname == "builtins.True"


def _holds_no_false(typ: Type | None) -> bool:
    """A ``Query``, records or a list of ints: none of their items is ``False``."""
    proper = get_proper_type(typ) if typ is not None else None
    if not isinstance(proper, Instance):
        return False
    if proper.type.fullname in {"builtins.list", "builtins.tuple", "builtins.set"}:
        items = [get_proper_type(arg) for arg in proper.args]
        return bool(items) and all(
            isinstance(item, Instance) and item.type.fullname == "builtins.int" for item in items
        )
    return any(base.name in {"Query", "BaseModel"} for base in proper.type.mro)
