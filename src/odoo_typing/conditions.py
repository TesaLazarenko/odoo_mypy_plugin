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

* **An alias of a condition.** A local variable bound once to a value whose truth
  implies the truth of names or member expressions (``x and ...``, ``... if x else
  False``, ``(x and ...) or []``, ``bool(x)``) gets them added where an ``if``,
  ``elif``, ``while`` or ``assert`` tests it, as an operand of ``and`` / ``or`` /
  ``not`` or alone: ``if cron_enabled:`` becomes ``if cron_enabled and cron:``. The two
  are true together, so the code means the same, and mypy narrows ``cron``. Only
  where the alias and the names it implies are bound before the test and not bound
  again in the function.
* **``self.assertTrue(value)``**, as a statement, is followed by ``assert value`` (the
  two in a block that takes the statement's place): the call is checked as before, and
  the test fails where the value is false, so mypy narrows it after.
"""

from __future__ import annotations

from collections.abc import Iterator

from mypy.nodes import (
    AssertStmt,
    AssignmentStmt,
    Block,
    CallExpr,
    ConditionalExpr,
    DictExpr,
    Expression,
    ExpressionStmt,
    FuncItem,
    IfStmt,
    IntExpr,
    ListExpr,
    MemberExpr,
    MypyFile,
    NameExpr,
    OperatorAssignmentStmt,
    OpExpr,
    Statement,
    StrExpr,
    TupleExpr,
    UnaryExpr,
    Var,
    WhileStmt,
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
        changed = {
            target.node
            for target in bindings.augmented
            if isinstance(target, NameExpr) and isinstance(target.node, Var)
        }
        self.bound_lines: dict[Var, list[int]] = {
            variable: [name.line for name, _ in names] for variable, names in bindings.names.items()
        }
        self.unstable = changed | bindings.excluded
        self.assigned_members = {
            _member_key(target)
            for statement in _statements(function.body)
            if isinstance(statement, AssignmentStmt | OperatorAssignmentStmt)
            for target in _targets(statement)
            if isinstance(target, MemberExpr)
        }
        self.aliases: dict[Var, tuple[int, list[Expression]]] = {}
        for variable, names in bindings.names.items():
            if len(names) != 1 or variable in self.unstable or variable.is_argument:
                continue
            name, value = names[0]
            implied = list(_implied(value, 0)) if value is not None else []
            kept = [item for item in implied if self._stable(item, name.line)]
            if kept:
                self.aliases[variable] = (name.line, kept)

    def rewrite(self) -> None:
        if not self.aliases and not _has_assert_true(self.function.body):
            return
        for block in list(_blocks(self.function.body)):  # not the blocks made below
            for index, statement in enumerate(block.body):
                if isinstance(statement, IfStmt):
                    statement.expr = [self._test(test, statement.line) for test in statement.expr]
                elif isinstance(statement, WhileStmt | AssertStmt):
                    statement.expr = self._test(statement.expr, statement.line)
                elif (asserted := _asserted(statement)) is not None:
                    block.body[index] = asserted

    def _stable(self, item: Expression, line: int) -> bool:
        """Whether a name an alias implies keeps the value it had when the alias was bound."""
        root = item
        while isinstance(root, MemberExpr):
            if _member_key(root) in self.assigned_members:
                return False
            root = root.expr
        if not isinstance(root, NameExpr) or not isinstance(root.node, Var):
            return False
        if root.node in self.unstable:
            return False
        lines = self.bound_lines.get(root.node, [])
        return all(bound <= line for bound in lines) and len(lines) <= 1

    def _test(self, test: Expression, line: int) -> Expression:
        """A test with each alias it reads as an operand replaced by ``alias and implied``."""
        if isinstance(test, NameExpr) and isinstance(test.node, Var) and test.node in self.aliases:
            bound, implied = self.aliases[test.node]
            if bound >= line:
                return test
            found: Expression = test
            for item in implied:
                found = _located(OpExpr("and", found, _copy(item)), test)
            return found
        if isinstance(test, OpExpr) and test.op in {"and", "or"}:
            test.left = self._test(test.left, line)
            test.right = self._test(test.right, line)
        elif isinstance(test, UnaryExpr) and test.op == "not":
            test.expr = self._test(test.expr, line)
        return test


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


def _targets(statement: AssignmentStmt | OperatorAssignmentStmt) -> list[Expression]:
    if isinstance(statement, OperatorAssignmentStmt):
        return [statement.lvalue]
    found: list[Expression] = []
    pending = list(statement.lvalues)
    while pending:
        target = pending.pop()
        if isinstance(target, TupleExpr | ListExpr):
            pending.extend(target.items)
        else:
            found.append(target)
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
