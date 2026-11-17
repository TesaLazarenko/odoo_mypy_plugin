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
once to ``search(...)`` / ``search_fetch(...)`` (``x |= v`` binds it again), or from the
variable of a ``for`` over such a local, has no ``False``. The ``for`` is the one that
binds the variable, or, for a variable bound more than once (two loops, ``for lead in
team1_leads:`` then ``for lead in team2_leads:``), the innermost ``for`` binding it whose
body holds the read, when no other binding of the variable may run in a pass of that
body before the read: none before the read in the body, none in a loop of the body
around the read.
"""

from __future__ import annotations

from collections.abc import Callable

from mypy.nodes import (
    ARG_POS,
    Block,
    CallExpr,
    Expression,
    ForStmt,
    FuncItem,
    IntExpr,
    ListExpr,
    MemberExpr,
    NameExpr,
    Statement,
    StrExpr,
    TupleExpr,
    Var,
    WhileStmt,
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
    over such a local (see the module).
    """
    if not isinstance(receiver, NameExpr) or not isinstance(receiver.node, Var):
        return None
    collected = function_bindings(function)
    values = {variable: names[0][1] for variable, names in collected.names.items()}
    bound = {variable: [name for name, _ in names] for variable, names in collected.names.items()}
    for target in collected.augmented:  # ``x |= v``
        if isinstance(target, NameExpr) and isinstance(target.node, Var):
            bound.setdefault(target.node, []).append(target)

    def once(variable: Var) -> Expression | None:
        """The value of a local bound once (``None`` for a ``for`` target)."""
        return values.get(variable) if len(bound.get(variable, [])) == 1 else None

    names = bound.get(receiver.node, [])
    value = once(receiver.node)
    if value is None and names:  # a ``for`` target, or a name bound more than once
        if len(names) == 1:
            iterated = _iterated(function, receiver.node)
        else:
            iterated = _loop_over(function, receiver, names)
        if not isinstance(iterated, NameExpr) or not isinstance(iterated.node, Var):
            return None
        value = once(iterated.node)
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


def _loop_over(function: FuncItem, read: NameExpr, bound: list[NameExpr]) -> Expression | None:
    """What the innermost ``for`` binding a name whose body holds a read of it iterates.

    ``None`` when another binding of the name may run in a pass of that body before the
    read: one before the read in the body, one in a loop of the body around the read.
    """
    path: list[Statement] = []  # the statements around the read, the outermost first
    blocks = [function.body]
    while holding := [item for block in blocks for item in block.body if _holds(item, read)]:
        path.append(holding[0])
        blocks = [block for block in nested_blocks(holding[0]) if _holds(block, read)]
    loops = [
        (number, statement)
        for number, statement in enumerate(path)
        if isinstance(statement, ForStmt)
        and isinstance(statement.index, NameExpr)
        and statement.index.node is read.node
        and _holds(statement.body, read)
    ]
    if not loops:
        return None
    number, loop = loops[-1]
    inner = [item for item in path[number + 1 :] if isinstance(item, ForStmt | WhileStmt)]
    for name in bound:
        if name is loop.index or not _holds(loop.body, name):
            continue
        if _position(name) < _position(read) or any(_holds(item, name) for item in inner):
            return None
    return loop.expr


def _holds(node: Statement | Block, expression: Expression) -> bool:
    """Whether the source of a statement or block holds an expression."""
    end = (node.end_line or node.line, node.end_column if node.end_column is not None else 10**9)
    return (node.line, node.column) <= _position(expression) <= end


def _position(expression: Expression) -> tuple[int, int]:
    return expression.line, expression.column


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
