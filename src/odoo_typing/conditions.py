"""Conditions mypy narrows nothing by, though the code proves a value is there.

mypy narrows the names and member expressions a test reads, not the names a test's
value was built from, and it takes a test method's ``assertTrue`` for a plain call::

    cron = self.env.ref("sale.send_pending_emails_cron", raise_if_not_found=False)
    cron_enabled = cron and cron.sudo().active
    if async_send and cron_enabled:
        cron._trigger()                     # Item "None" of "ir.cron | None"

    value_result = field_result.get(key) if field_result else False
    if value_result:
        field_result["won_total"]           # "Any | None" is not indexable

    self.assertTrue(order.effective_date)
    order.effective_date.date()             # Item "bool" of "datetime | Literal[False]"

Before mypy checks a module of an addon (with the rebound variables,
:mod:`odoo_typing.rebound`), each function's tests are rewritten so that mypy sees what
the code proves:

* **An alias of a condition.** A local variable bound to a value whose truth implies
  the truth of names or member expressions (``x and ...``, ``... if x else False``,
  ``(x and ...) or []``, ``bool(x)``) gets them added where an ``if``, ``elif``,
  ``while`` or ``assert`` tests it, as an operand of ``and`` / ``or`` / ``not`` or
  alone: ``if cron_enabled:`` becomes ``if cron_enabled and cron:``. The two are true
  together, so the code means the same, and mypy narrows ``cron``. The binding is the
  alias's last one before the test, and it must still hold there: no binding of the
  alias or of a name it implies (``x = ...``, ``x.attr = ...``, ``x += ...``, a ``for``
  or ``with`` target) may run between the two. A binding may when it is after the
  alias and before the test in the source; in a loop around the test (the test of a
  ``while`` is in its loop), unless the loop holds the alias too and the alias is an
  earlier statement of a block the test is in, so that it runs again before the test
  in each pass; in a loop around the alias but not the test (a later pass may bind it
  and skip the alias); and, for an earlier binding of the alias, unless the alias is
  such a statement (an ``if`` may skip it). A binding later in the function changes
  nothing: ``dates = (self.day and allowed) or []``, ``if not dates or
  self.day.date() ...: return``, then ``self.day = dates[0]``.
* **``self.assertTrue(value)``**, as a statement, is followed by ``assert value`` (the
  two in a block that takes the statement's place): the call is checked as before, and
  the test fails where the value is false, so mypy narrows it after.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from mypy.nodes import (
    AssertStmt,
    AssignmentStmt,
    Block,
    CallExpr,
    ConditionalExpr,
    DictExpr,
    Expression,
    ExpressionStmt,
    ForStmt,
    FuncItem,
    IfStmt,
    IntExpr,
    ListExpr,
    MemberExpr,
    MypyFile,
    NameExpr,
    OperatorAssignmentStmt,
    OpExpr,
    StarExpr,
    Statement,
    StrExpr,
    TupleExpr,
    UnaryExpr,
    Var,
    WhileStmt,
    WithStmt,
)

from odoo_typing.rebound import function_bindings, functions, nested_blocks

__all__ = ["narrow_conditions"]

ASSERT_TRUE = "assertTrue"
FALSY_NAMES = frozenset({"False", "None"})
MAX_DEPTH = 8  # nested and / or / conditional values followed for the names they imply


def narrow_conditions(tree: MypyFile) -> None:
    """Rewrite the tests of every function of a module (see the module docstring)."""
    for function in functions(tree.defs):
        _Function(function).rewrite()


class _Function:
    """The aliases of one function and the rewriting of its tests."""

    def __init__(self, function: FuncItem) -> None:
        self.function = function
        bindings = function_bindings(function)
        self.excluded = bindings.excluded  # ``except ... as e``, ``case`` captures
        # Each statement of the body in source order: the blocks it is in, with its
        # position in each; the loops among them as (first, last statement, ``while``).
        self.chains: list[tuple[tuple[int, int], ...]] = []
        self.places: dict[int, int] = {}  # id of a statement -> its place in ``chains``
        self.loops: list[tuple[int, int, bool]] = []
        # The bindings of each name and member expression: the statement, the value.
        self.names: dict[Var, list[tuple[int, Expression | None]]] = {}
        self.members: dict[tuple[str, ...], list[int]] = {}
        self._walk(function.body, ())
        self.aliases = {
            variable
            for variable, names in bindings.names.items()
            if variable not in self.excluded
            and not variable.is_argument
            and any(value is not None and next(_implied(value, 0), None) for _, value in names)
        }

    def rewrite(self) -> None:
        if not self.aliases and not _has_assert_true(self.function.body):
            return
        for block in list(_blocks(self.function.body)):  # not the blocks made below
            for index, statement in enumerate(block.body):
                if isinstance(statement, IfStmt):
                    statement.expr = [self._test(test, statement) for test in statement.expr]
                elif isinstance(statement, WhileStmt | AssertStmt):
                    statement.expr = self._test(statement.expr, statement)
                elif (asserted := _asserted(statement)) is not None:
                    block.body[index] = asserted

    def _walk(self, block: Block, chain: tuple[tuple[int, int], ...]) -> None:
        """Number the statements of a block and those nested in it; record their bindings."""
        for position, statement in enumerate(block.body):
            place = len(self.chains)
            inside = (*chain, (id(block), position))
            self.chains.append(inside)
            self.places[id(statement)] = place
            for target, value in _bound(statement):
                if isinstance(target, MemberExpr):
                    self.members.setdefault(_member_key(target), []).append(place)
                elif isinstance(target.node, Var):
                    self.names.setdefault(target.node, []).append((place, value))
            for nested in nested_blocks(statement):
                self._walk(nested, inside)
            if isinstance(statement, ForStmt | WhileStmt):
                self.loops.append((place, len(self.chains) - 1, isinstance(statement, WhileStmt)))

    def _test(self, test: Expression, statement: Statement) -> Expression:
        """A test with each alias it reads as an operand replaced by ``alias and implied``."""
        place = self.places.get(id(statement))
        if place is None:
            return test
        if isinstance(test, NameExpr) and isinstance(test.node, Var) and test.node in self.aliases:
            found: Expression = test
            for item in self._implied_at(test.node, place):
                found = _located(OpExpr("and", found, _copy(item)), test)
            return found
        if isinstance(test, OpExpr) and test.op in {"and", "or"}:
            test.left = self._test(test.left, statement)
            test.right = self._test(test.right, statement)
        elif isinstance(test, UnaryExpr) and test.op == "not":
            test.expr = self._test(test.expr, statement)
        return test

    def _implied_at(self, alias: Var, test: int) -> list[Expression]:
        """What an alias implies where a test reads it, of what still holds there."""
        bindings = self.names.get(alias, [])
        earlier = [number for number, (place, _) in enumerate(bindings) if place < test]
        if not earlier:
            return []  # bound after the test: in a loop, from an earlier pass
        chosen = earlier[-1]
        bound, value = bindings[chosen]
        if value is None:
            return []
        each_pass = self._precedes(bound, test)
        # The loops a binding in may run after the alias's and before the test: those
        # around the test, unless the alias runs again before it in each pass, and
        # those around the alias only (a later pass may bind what it tested, or skip it).
        loops = [
            (first, last)
            for first, last, repeated in self.loops
            if (
                (first < test <= last or (repeated and first == test))
                and not (each_pass and first < bound <= last)
            )
            or (first < bound <= last and not first < test <= last)
        ]

        def between(place: int) -> bool:
            """Whether a binding may run after the alias's and before the test."""
            return bound <= place < test or any(first <= place <= last for first, last in loops)

        for number, (place, _) in enumerate(bindings):
            if number != chosen and (between(place) or (place < bound and not each_pass)):
                return []
        return [item for item in _implied(value, 0) if self._holds(item, alias, between)]

    def _holds(self, item: Expression, alias: Var, between: Callable[[int], bool]) -> bool:
        """Whether no binding of a name an alias implies may run between the two."""
        keys: list[tuple[str, ...]] = []
        root = item
        while isinstance(root, MemberExpr):
            keys.append(_member_key(root))
            root = root.expr
        if not isinstance(root, NameExpr) or not isinstance(root.node, Var):
            return False
        if root.node in self.excluded or root.node is alias:
            return False
        places = [place for place, _ in self.names.get(root.node, [])]
        places += [place for key in keys for place in self.members.get(key, [])]
        return not any(map(between, places))

    def _precedes(self, first: int, second: int) -> bool:
        """Whether a statement runs before another each time that one runs.

        The first is an earlier statement of a block the second is in, or nested in.
        """
        outer, inner = self.chains[first], self.chains[second]
        depth = len(outer) - 1
        return (
            len(inner) > depth
            and inner[:depth] == outer[:depth]
            and inner[depth][0] == outer[depth][0]
            and inner[depth][1] > outer[depth][1]
        )


def _implied(value: Expression, depth: int) -> Iterator[Expression]:
    """The names and member expressions whose truth the truth of a value implies."""
    if depth > MAX_DEPTH:
        return
    if isinstance(value, NameExpr | MemberExpr):
        if _plain(value):
            yield value
    elif isinstance(value, OpExpr) and value.op == "and":
        yield from _implied(value.left, depth + 1)
        yield from _implied(value.right, depth + 1)
    elif isinstance(value, OpExpr) and value.op == "or":
        if _falsy(value.right):  # ``(x and ...) or []``
            yield from _implied(value.left, depth + 1)
        elif _falsy(value.left):
            yield from _implied(value.right, depth + 1)
    elif isinstance(value, ConditionalExpr) and _falsy(value.else_expr):
        yield from _implied(value.cond, depth + 1)
        yield from _implied(value.if_expr, depth + 1)
    elif (
        isinstance(value, CallExpr)
        and isinstance(value.callee, NameExpr)
        and value.callee.fullname == "builtins.bool"
        and len(value.args) == 1
    ):
        yield from _implied(value.args[0], depth + 1)


def _plain(expression: Expression) -> bool:
    """A local name, or attributes read from one (``self.commitment_date``)."""
    while isinstance(expression, MemberExpr):
        expression = expression.expr
    return isinstance(expression, NameExpr) and isinstance(expression.node, Var)


def _falsy(value: Expression) -> bool:
    """A literal that is always false: ``False``, ``None``, ``0``, ``""``, an empty display."""
    if isinstance(value, NameExpr):
        return value.name in FALSY_NAMES and value.fullname in {f"builtins.{value.name}", ""}
    if isinstance(value, IntExpr):
        return value.value == 0
    if isinstance(value, StrExpr):
        return not value.value
    if isinstance(value, ListExpr | TupleExpr):
        return not value.items
    return isinstance(value, DictExpr) and not value.items


def _copy(expression: Expression) -> Expression:
    """A fresh node for a name or member expression, bound as the original is."""
    if isinstance(expression, NameExpr):
        name = NameExpr(expression.name)
        name.node = expression.node
        name.kind = expression.kind
        name.fullname = expression.fullname
        return _located(name, expression)
    assert isinstance(expression, MemberExpr)
    member = MemberExpr(_copy(expression.expr), expression.name)
    member.kind = expression.kind
    member.fullname = expression.fullname
    member.node = expression.node
    return _located(member, expression)


def _located(node: Expression, origin: Expression) -> Expression:
    node.set_line(origin)
    return node


def _member_key(expression: MemberExpr) -> tuple[str, ...]:
    parts: list[str] = []
    current: Expression = expression
    while isinstance(current, MemberExpr):
        parts.append(current.name)
        current = current.expr
    parts.append(current.name if isinstance(current, NameExpr) else "")
    return tuple(reversed(parts))


def _asserted(statement: Statement) -> Block | None:
    """``self.assertTrue(value, ...)`` followed by ``assert value``, in one block."""
    call = statement.expr if isinstance(statement, ExpressionStmt) else None
    if (
        not isinstance(call, CallExpr)
        or not isinstance(call.callee, MemberExpr)
        or call.callee.name != ASSERT_TRUE
        or not isinstance(call.callee.expr, NameExpr)
        or call.callee.expr.name != "self"
        or not call.args
        or call.arg_names[0] is not None
    ):
        return None
    asserted = AssertStmt(call.args[0])
    asserted.set_line(statement)
    block = Block([statement, asserted])
    block.set_line(statement)
    return block


def _has_assert_true(block: Block) -> bool:
    return any(_asserted(statement) is not None for statement in _statements(block))


def _bound(statement: Statement) -> list[tuple[NameExpr | MemberExpr, Expression | None]]:
    """The names and member expressions a statement binds, each with its value if known."""
    pending: list[tuple[Expression, Expression | None]]
    if isinstance(statement, AssignmentStmt):
        pending = [(target, statement.rvalue) for target in statement.lvalues]
    elif isinstance(statement, OperatorAssignmentStmt):
        pending = [(statement.lvalue, None)]
    elif isinstance(statement, ForStmt):
        pending = [(statement.index, None)]
    elif isinstance(statement, WithStmt):
        pending = [(target, None) for target in statement.target if target is not None]
    else:
        return []
    found: list[tuple[NameExpr | MemberExpr, Expression | None]] = []
    while pending:
        target, value = pending.pop()
        if isinstance(target, NameExpr | MemberExpr):
            found.append((target, value))
        elif isinstance(target, TupleExpr | ListExpr):
            pending.extend((item, None) for item in target.items)
        elif isinstance(target, StarExpr):
            pending.append((target.expr, None))
    return found


def _blocks(block: Block) -> Iterator[Block]:
    """A function body and the blocks nested in it (not those of nested functions)."""
    pending = [block]
    while pending:
        current = pending.pop()
        yield current
        for statement in current.body:
            pending.extend(nested_blocks(statement))


def _statements(block: Block) -> Iterator[Statement]:
    for current in _blocks(block):
        yield from current.body
