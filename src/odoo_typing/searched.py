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
* ``like`` / ``ilike`` / ``=like`` / ``=ilike`` with a string that cannot be empty: a
  literal that is not empty, or an f-string or a ``+`` with a part that is one
  (``f"{prefix}%"``). An empty pattern finds the records without a value (Odoo 18 adds
  ``OR f IS NULL``, 19 makes ``=like ""`` an ``= False``), so a ``str`` variable does not;
* ``<`` / ``<=`` / ``>`` / ``>=`` on a ``Date``, ``Datetime`` or ``Many2one`` field that
  Odoo searches in SQL (no ``search=`` method, not ``related``), with any value
  (``fields.Datetime.to_string(...)`` too): Odoo adds ``OR f IS NULL`` to an inequality
  only for a field with a falsy value of its own (numbers, and from Odoo 19 strings and
  booleans), which these fields have not. A ``search=`` method gives what it wants
  (purchase_stock's ``days_to_arrive`` finds pickings whose value is ``False``).

A read of such a field from the result of ``read`` / ``search_read``, from a local bound
once to ``search(...)`` / ``search_fetch(...)`` (``x |= v`` binds it again; ``x =
x.filtered(...)``, ``x.sorted(...)`` and the like keep some of its records, which counts as
bound once; a parameter is never bound once; nor is a name ``:=``, ``except ... as`` or a
``case`` binds) where the code tested that there are some (of none a field reads
``False``: ``hooks.field_values``), or from the variable of a ``for`` over such a local,
has no ``False``. The ``for`` is the one that
binds the variable, or, for a variable bound more than once (two loops, ``for lead in
team1_leads:`` then ``for lead in team2_leads:``), the innermost ``for`` binding it whose
body holds the read, when no other binding of the variable may run in a pass of that
body before the read: none before the read in the body, none in a loop of the body
around the read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from mypy.nodes import (
    ARG_POS,
    AssignmentExpr,
    Block,
    CallExpr,
    Expression,
    ForStmt,
    FuncItem,
    IntExpr,
    LambdaExpr,
    ListExpr,
    MemberExpr,
    NameExpr,
    OpExpr,
    Statement,
    StrExpr,
    TupleExpr,
    Var,
    WhileStmt,
)
from mypy.server.subexpr import get_subexpressions
from mypy.types import Instance, Type, get_proper_type

from odoo_typing.rebound import function_bindings, nested_blocks, walrus_targets
from odoo_typing.scanner import FieldInfo

__all__ = [
    "SEARCH_METHODS",
    "domain_argument",
    "filtered_fields",
    "kept_fields",
    "loop_record",
    "restricted_fields",
    "searched_domain",
    "sql_kinds",
]

SEARCH_METHODS = frozenset({"search", "search_fetch"})
EQUAL = frozenset({"=", "==", "child_of", "parent_of"})
LIKE = frozenset({"like", "ilike", "=like", "=ilike"})
INEQUALITIES = frozenset({"<", "<=", ">", ">="})
# Field classes with no falsy value of their own: an inequality matches no NULL.
NO_FALSY_VALUE = frozenset({"Date", "Datetime", "Many2one"})
# Methods that give some of the records they are called on, with the same field values.
SUBSETS = frozenset({"filtered", "filtered_domain", "sorted", "exists", "sudo"})
EMPTY_NAMES = frozenset({"builtins.False", "builtins.None"})
TERM_SIZE = 3


def domain_argument(call: CallExpr) -> Expression | None:
    """The domain a call passes: its first positional argument or ``domain=``."""
    for argument, kind, name in zip(call.args, call.arg_kinds, call.arg_names, strict=True):
        if name == "domain" or (name is None and kind == ARG_POS):
            return argument
    return None


def sql_kinds(fields: Mapping[str, FieldInfo]) -> dict[str, str]:
    """The field class of the fields Odoo searches in SQL, not by a ``search=`` method.

    Not of a ``related`` field (one of ``_inherits`` too): Odoo searches it through the
    field it relates to, whose ``search=`` method or x2many path decides. Odoo 17 gives a
    ``company_dependent`` field a ``search`` of its own, on ``ir.property``, which compares
    the stored values in SQL as well, and records without one read the default found.
    """
    return {
        name: info.field_type
        for name, info in fields.items()
        if info.search is None
        and info.related is None
        and not {"search", "related"} & info.dynamic
        and not info.compute_sql
    }


def restricted_fields(
    domain: Expression | None,
    type_of: Callable[[Expression], Type | None],
    kinds: Mapping[str, str],
) -> frozenset[str]:
    """The fields a literal domain makes set on every record it finds (see the module).

    Args:
        domain: The domain.
        type_of: The type mypy has for an expression.
        kinds: The field class of the fields Odoo searches in SQL (``Datetime``), from
            :func:`sql_kinds`.
    """
    if not isinstance(domain, ListExpr | TupleExpr):
        return frozenset()
    found: set[str] = set()
    for term in _conjuncts(list(domain.items)):
        field, operator, value = term
        if not isinstance(field, StrExpr) or not isinstance(operator, StrExpr):
            continue
        if "." in field.value:
            continue  # a path: through a relational field, other records
        if _sets(operator.value, value, type_of) or (
            operator.value in INEQUALITIES and kinds.get(field.value) in NO_FALSY_VALUE
        ):
            found.add(field.value)
    return frozenset(found)


def searched_domain(function: FuncItem, receiver: Expression) -> Expression | None:
    """The literal domain of the search that gave the records a name holds, if one did.

    A local bound once to ``records.search(domain, ...)``, or the variable of a ``for``
    over such a local or over the search itself (see the module).
    """
    value = _records_source(function, receiver)
    if (
        isinstance(value, CallExpr)
        and isinstance(value.callee, MemberExpr)
        and value.callee.name in SEARCH_METHODS
    ):
        return domain_argument(value)
    return None


def filtered_fields(function: FuncItem, receiver: Expression) -> frozenset[str]:
    """The fields that ``filtered(...)`` set on the records a name holds.

    ``for website in self.filtered("homepage_url"):`` keeps the records whose field is true
    (Odoo's ``rec[func]``): ``website.homepage_url`` is no ``False``. A local bound once to
    such a call or the variable of a ``for`` over one, as for a search; the calls may be
    chained (``filtered("a").filtered("b")``). What each call keeps: :func:`filter_fields`.
    """
    return kept_fields(_records_source(function, receiver))


def kept_fields(value: Expression | None) -> frozenset[str]:
    """The fields set on the records of ``records.filtered(...)``, chained calls too."""
    found: set[str] = set()
    while (
        isinstance(value, CallExpr)
        and isinstance(value.callee, MemberExpr)
        and value.callee.name == "filtered"
        and len(value.args) == 1
    ):
        found |= filter_fields(value.args[0])
        value = value.callee.expr
    return frozenset(found)


def filter_fields(argument: Expression) -> frozenset[str]:
    """The fields that the argument of ``filtered`` proves true on the records it keeps.

    Odoo keeps the records for which ``rec[name]`` (a string) or ``func(rec)`` (a
    function) is true: ``filtered("f")`` and ``filtered(lambda r: r.f)`` keep those whose
    ``f`` is set, ``lambda r: r.f and r.g and ...`` those whose every field read off the
    parameter in the conjunction is. A path (``"partner_id.email"``, ``r.partner_id.email``)
    keeps the records of which some value is true, which says nothing of the last field
    of the others. Odoo calls the function with the record alone (``func(rec)``): its
    first parameter takes it, the others must take nothing (a default, ``*args``,
    ``**kwargs``). Nothing is proved by a lambda that binds a name
    (``lambda r: (r := r.parent_id) and r.name``).
    """
    if isinstance(argument, StrExpr):
        name = argument.value
        return frozenset({name}) if name and "." not in name else frozenset()
    if (
        not isinstance(argument, LambdaExpr)
        or not argument.arguments
        or not argument.arguments[0].kind.is_positional()
        or any(item.kind.is_required() for item in argument.arguments[1:])
    ):
        return frozenset()
    body = argument.expr()
    if any(isinstance(node, AssignmentExpr) for node in get_subexpressions(body)):
        return frozenset()
    parameter = argument.arguments[0].variable
    return frozenset(
        operand.name
        for operand in _and_operands(body)
        if isinstance(operand, MemberExpr)
        and isinstance(operand.expr, NameExpr)
        and operand.expr.node is parameter
    )


def _and_operands(expression: Expression) -> list[Expression]:
    """The operands of ``a and b and ...``: each is true where the whole is."""
    if isinstance(expression, OpExpr) and expression.op == "and":
        return [*_and_operands(expression.left), *_and_operands(expression.right)]
    return [expression]


def loop_record(function: FuncItem, receiver: Expression) -> bool:
    """Whether a name is the variable of a ``for`` over records: one record each time.

    The records a search or ``filtered`` gave may be none, and a field read off none
    reads ``False``; a record a ``for`` goes through is one.
    """
    return _records_source(function, receiver, loops_only=True) is not None


def _records_source(
    function: FuncItem, receiver: Expression, *, loops_only: bool = False
) -> Expression | None:
    """The expression that gave a local its records: its one value, or what its ``for`` iterates.

    For the variable of a ``for`` over a local, the local's one value. With
    ``loops_only``, only for the variable of a ``for``.
    """
    if not isinstance(receiver, NameExpr) or not isinstance(receiver.node, Var):
        return None
    collected = function_bindings(function)
    values = {variable: names[0][1] for variable, names in collected.names.items()}
    bound = {variable: [name for name, _ in names] for variable, names in collected.names.items()}
    for target in collected.augmented:  # ``x |= v``
        if isinstance(target, NameExpr) and isinstance(target.node, Var):
            bound.setdefault(target.node, []).append(target)
    parameters = {argument.variable for argument in function.arguments}
    # Bound where the scan of statements does not look: ``(x := v)``, ``except ... as x``,
    # a ``case`` capture.
    elsewhere = {
        name.node for name in walrus_targets(function) if isinstance(name.node, Var)
    } | collected.excluded
    if receiver.node in elsewhere:
        return None  # ``for rec in found: (rec := other)``

    def once(variable: Var) -> Expression | None:
        """The value of a local bound once (``None`` for a ``for`` target).

        Also of one bound again only to some of its own records (``x = x.filtered(...)``).
        Not of a parameter: under an ``if`` it may still hold the caller's records.
        """
        if variable in parameters or variable in elsewhere:
            return None
        names = bound.get(variable, [])
        bindings = collected.names.get(variable, [])
        if len(names) != len(bindings):
            return None  # ``x |= v`` among them
        if all(_subset_of(value, variable) for _, value in bindings[1:]):
            return values.get(variable)
        return None

    names = bound.get(receiver.node, [])
    value = once(receiver.node)
    if value is not None and loops_only:
        return None
    if value is None and names:  # a ``for`` target, or a name bound more than once
        if len(names) == 1:
            iterated = _iterated(function, receiver.node)
        else:
            iterated = _loop_over(function, receiver, names)
        if isinstance(iterated, CallExpr):
            return iterated  # ``for record in records.search(...)``
        if not isinstance(iterated, NameExpr) or not isinstance(iterated.node, Var):
            return None
        value = once(iterated.node)
    return value


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


def _subset_of(value: Expression | None, variable: Var) -> bool:
    """``variable.filtered(...).sorted(...)``: some of the records the variable holds."""
    while (
        isinstance(value, CallExpr)
        and isinstance(value.callee, MemberExpr)
        and value.callee.name in SUBSETS
    ):
        value = value.callee.expr
    return isinstance(value, NameExpr) and value.node is variable


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
        return _not_empty(value)
    if operator == "in":
        if isinstance(value, ListExpr | TupleExpr):
            return bool(value.items) and all(_true_literal(item) for item in value.items)
        return _holds_no_false(type_of(value))
    return False


def _not_empty(value: Expression) -> bool:
    """A string that cannot be empty: ``"x"``, ``f"{prefix}%"``, ``prefix + "%"``."""
    if isinstance(value, StrExpr):
        return bool(value.value)
    if isinstance(value, OpExpr) and value.op == "+":
        return _not_empty(value.left) or _not_empty(value.right)
    # mypy parses an f-string as ``"".join([parts])``
    return (
        isinstance(value, CallExpr)
        and isinstance(value.callee, MemberExpr)
        and value.callee.name == "join"
        and isinstance(value.callee.expr, StrExpr)
        and not value.callee.expr.value
        and len(value.args) == 1
        and isinstance(value.args[0], ListExpr)
        and any(_not_empty(part) for part in value.args[0].items)
    )


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
