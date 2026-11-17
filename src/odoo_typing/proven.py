"""What the code of a function proves about an expression where it is evaluated.

mypy narrows names and member expressions, not calls: after
``if not self.env.context.get("active_ids"): raise ...`` the next
``self.env.context.get("active_ids")`` is ``Any | None`` again, and so is
``env.ref("base.partner", raise_if_not_found=False)`` after the same call was bound to
``partner`` and ``if not partner: raise ...`` tested it. :func:`proven` reads the facts
that hold where an expression is evaluated, along the path from the function's body to
it:

* the tests of the ``if`` / ``elif`` / ``while`` arms it is in (true), and those of the
  arms before (false);
* an earlier statement of an enclosing block that ends unless its test is false
  (``if not x: return`` / ``raise`` / ``continue`` / ``break``), and an ``assert``;
* the left operand of ``and`` / ``or`` and the test of a conditional expression it is
  an operand of.

A test gives facts through ``not``, ``and`` / ``or``, ``x is None`` / ``x is not None``
and ``len(x) < n`` / ``len(x) == 0`` (also of ``x or []``), and through a local name to
the value it was bound to last. A fact is about an expression with the same structure
(``context.get("k")`` and ``context["k"]`` read the same item), and holds while no name
it reads is bound again between the test and the expression. The callers decide which
expressions keep their value (a read of ``env.context``, ``env.ref``).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from mypy.nodes import (
    AssertStmt,
    AssignmentStmt,
    Block,
    BreakStmt,
    CallExpr,
    ClassDef,
    ComparisonExpr,
    ConditionalExpr,
    ContinueStmt,
    Decorator,
    DictExpr,
    Expression,
    ExpressionStmt,
    ForStmt,
    FuncDef,
    FuncItem,
    IfStmt,
    IndexExpr,
    IntExpr,
    LambdaExpr,
    ListExpr,
    MatchStmt,
    MemberExpr,
    NameExpr,
    OperatorAssignmentStmt,
    OpExpr,
    RaiseStmt,
    ReturnStmt,
    Statement,
    StrExpr,
    TryStmt,
    TupleExpr,
    UnaryExpr,
    Var,
    WhileStmt,
    WithStmt,
)

from odoo_typing.rebound import function_bindings

__all__ = ["Proof", "proven"]

Proof = Literal["truthy", "not_none"]
FALSY_NAMES = frozenset({"builtins.False", "builtins.None"})
MAX_DEPTH = 16  # nested tests followed for facts


@dataclass(frozen=True, slots=True)
class _Fact:
    """An expression that is true (or not ``None``) from a line on."""

    expression: Expression
    truthy: bool  # ``False``: only not ``None``
    line: int


def proven(function: FuncItem, target: Expression) -> Proof | None:
    """What the function proves about an expression where it is evaluated (see the module)."""
    facts: list[_Fact] = []
    if not _walk(function.body, target, facts):
        return None
    lines = {
        variable: [name.line for name, _ in names]
        for variable, names in function_bindings(function).names.items()
    }
    values = _latest_values(function)
    found: Proof | None = None
    for fact in _with_values(facts, values):
        if not _same(fact.expression, target) or not _unchanged(target, fact.line, lines):
            continue
        if fact.truthy:
            return "truthy"
        found = "not_none"
    return found


def _walk(block: Block, target: Expression, facts: list[_Fact]) -> bool:
    """Collect the facts on the way to the statement of a block that holds the target."""
    for statement in block.body:
        if _contains(statement, target):
            return _enter(statement, target, facts)
        facts.extend(_after(statement))
    return False


def _enter(statement: Statement, target: Expression, facts: list[_Fact]) -> bool:
    if isinstance(statement, FuncDef | Decorator | ClassDef):
        return False  # runs later: what holds here need not hold there
    if isinstance(statement, IfStmt):
        for index, (test, body) in enumerate(zip(statement.expr, statement.body, strict=True)):
            if _contains(test, target):
                facts.extend(_false_before(statement, index))
                return _expression(test, target, facts)
            if _contains(body, target):
                facts.extend(_false_before(statement, index))
                facts.extend(_true(test, test.line))
                return _walk(body, target, facts)
        if statement.else_body is not None and _contains(statement.else_body, target):
            facts.extend(_false_before(statement, len(statement.expr)))
            return _walk(statement.else_body, target, facts)
        return False
    if isinstance(statement, WhileStmt):
        if _contains(statement.expr, target):
            return _expression(statement.expr, target, facts)
        if _contains(statement.body, target):
            facts.extend(_true(statement.expr, statement.expr.line))
            return _walk(statement.body, target, facts)
        return statement.else_body is not None and _walk(statement.else_body, target, facts)
    blocks = _blocks(statement)
    if blocks:
        expressions = _expressions(statement)
        for expression in expressions:
            if _contains(expression, target):
                return _expression(expression, target, facts)
        return any(_contains(inner, target) and _walk(inner, target, facts) for inner in blocks)
    return any(
        _contains(expression, target) and _expression(expression, target, facts)
        for expression in _expressions(statement)
    )


def _expression(expression: Expression, target: Expression, facts: list[_Fact]) -> bool:
    """Collect the facts of the operands before the target inside an expression."""
    if expression is target:
        return True
    if (
        isinstance(expression, OpExpr)
        and expression.op in {"and", "or"}
        and _contains(expression.right, target)
    ):
        if expression.op == "and":
            facts.extend(_true(expression.left, expression.line))
        else:
            facts.extend(_false(expression.left, expression.line))
        return _expression(expression.right, target, facts)
    if isinstance(expression, ConditionalExpr):
        if _contains(expression.if_expr, target):
            facts.extend(_true(expression.cond, expression.line))
            return _expression(expression.if_expr, target, facts)
        if _contains(expression.else_expr, target):
            facts.extend(_false(expression.cond, expression.line))
            return _expression(expression.else_expr, target, facts)
    return any(
        _contains(child, target) and _expression(child, target, facts)
        for child in _children(expression)
    )


def _false_before(statement: IfStmt, index: int) -> Iterator[_Fact]:
    for test in statement.expr[:index]:
        yield from _false(test, test.line)


def _after(statement: Statement) -> list[_Fact]:
    """The facts that hold after a statement that completes."""
    if isinstance(statement, AssertStmt):
        return list(_true(statement.expr, statement.line))
    if isinstance(statement, Block):
        return [fact for inner in statement.body for fact in _after(inner)]
    if isinstance(statement, IfStmt) and len(statement.expr) == 1:
        test = statement.expr[0]
        if _ends(statement.body[0]) and statement.else_body is None:
            return list(_false(test, statement.line))
        ends = statement.else_body is not None and _ends(statement.else_body)
        if ends and not _ends(statement.body[0]):
            return list(_true(test, statement.line))
    return []


def _ends(block: Block) -> bool:
    """Whether a block never completes: it returns, raises, continues or breaks."""
    if not block.body:
        return False
    last = block.body[-1]
    if isinstance(last, ReturnStmt | RaiseStmt | ContinueStmt | BreakStmt):
        return True
    if isinstance(last, IfStmt) and last.else_body is not None:
        return all(_ends(body) for body in last.body) and _ends(last.else_body)
    return isinstance(last, Block) and _ends(last)


def _true(test: Expression, line: int, depth: int = 0) -> Iterator[_Fact]:
    """The facts a true test gives."""
    if depth > MAX_DEPTH:
        return
    for part in _truthy_parts(test):
        yield _Fact(part, True, line)
    if isinstance(test, OpExpr) and test.op == "and":
        yield from _true(test.left, line, depth + 1)
        yield from _true(test.right, line, depth + 1)
    elif isinstance(test, UnaryExpr) and test.op == "not":
        yield from _false(test.expr, line, depth + 1)
    elif isinstance(test, ComparisonExpr):
        yield from _compared(test, line, negated=False)


def _false(test: Expression, line: int, depth: int = 0) -> Iterator[_Fact]:
    """The facts a false test gives."""
    if depth > MAX_DEPTH:
        return
    if isinstance(test, UnaryExpr) and test.op == "not":
        yield from _true(test.expr, line, depth + 1)
    elif isinstance(test, OpExpr) and test.op == "or":
        yield from _false(test.left, line, depth + 1)
        yield from _false(test.right, line, depth + 1)
    elif isinstance(test, ComparisonExpr):
        yield from _compared(test, line, negated=True)


def _compared(test: ComparisonExpr, line: int, negated: bool) -> Iterator[_Fact]:
    """``x is (not) None`` and ``len(x) < n`` / ``len(x) == 0``, true or (``negated``) false."""
    if len(test.operators) != 1:
        return
    operator = test.operators[0]
    left, right = test.operands
    is_none = isinstance(right, NameExpr) and right.fullname == "builtins.None"
    if is_none and operator == ("is" if negated else "is not"):
        yield _Fact(left, False, line)
        return
    size = _len_argument(left)
    if size is None or not isinstance(right, IntExpr):
        return
    # Whether the test says len(x) >= 1: a false ``len(x) < n`` (n >= 1) or ``== 0``,
    # a true ``len(x) >= n`` (n >= 1), ``> n`` or ``!= 0``.
    value = right.value
    small_when_true = (operator == "<" and value >= 1) or (operator == "==" and value == 0)
    sized_when_true = (
        (operator == ">=" and value >= 1)
        or (operator == ">" and value >= 0)
        or (operator == "!=" and value == 0)
    )
    if (negated and small_when_true) or (not negated and sized_when_true):
        for part in _truthy_parts(size):
            yield _Fact(part, True, line)


def _len_argument(expression: Expression) -> Expression | None:
    if (
        isinstance(expression, CallExpr)
        and isinstance(expression.callee, NameExpr)
        and expression.callee.fullname == "builtins.len"
        and len(expression.args) == 1
    ):
        return expression.args[0]
    return None


def _truthy_parts(expression: Expression) -> Iterator[Expression]:
    """An expression that is true, and what it being true makes true (``x or []``: ``x``)."""
    yield expression
    if isinstance(expression, OpExpr) and expression.op == "or" and _falsy(expression.right):
        yield from _truthy_parts(expression.left)


def _falsy(expression: Expression) -> bool:
    if isinstance(expression, NameExpr):
        return expression.fullname in FALSY_NAMES
    if isinstance(expression, ListExpr | TupleExpr | DictExpr):
        return not expression.items
    if isinstance(expression, IntExpr | StrExpr):
        return not expression.value
    return False


def _latest_values(function: FuncItem) -> dict[Var, list[tuple[int, Expression | None]]]:
    """The values each local variable is bound to, with the lines of the bindings."""
    return {
        variable: [(name.line, value) for name, value in names]
        for variable, names in function_bindings(function).names.items()
    }


def _with_values(
    facts: list[_Fact], values: dict[Var, list[tuple[int, Expression | None]]]
) -> Iterator[_Fact]:
    """The facts, and for a fact about a local name the same about its value then."""
    for fact in facts:
        yield fact
        name = fact.expression
        if not isinstance(name, NameExpr) or not isinstance(name.node, Var):
            continue
        earlier = [(line, value) for line, value in values.get(name.node, []) if line <= fact.line]
        if earlier and earlier[-1][1] is not None:
            line, value = earlier[-1]
            assert value is not None
            yield _Fact(value, fact.truthy, max(line, fact.line))


def _unchanged(target: Expression, since: int, lines: dict[Var, list[int]]) -> bool:
    """Whether no name the target reads is bound between a fact's line and the target."""
    for node in _names(target):
        if isinstance(node.node, Var) and any(
            since < line <= target.line for line in lines.get(node.node, [])
        ):
            return False
    return True


def _names(expression: Expression) -> Iterator[NameExpr]:
    pending = [expression]
    while pending:
        current = pending.pop()
        if isinstance(current, NameExpr):
            yield current
        elif isinstance(current, MemberExpr):
            pending.append(current.expr)
        else:
            pending.extend(_children(current))


def _same(first: Expression, second: Expression) -> bool:
    """Whether two expressions read the same value (``get("k")`` and ``["k"]`` alike)."""
    one, other = _item(first), _item(second)
    if one is not None and other is not None:
        return _same(one[0], other[0]) and _same(one[1], other[1])
    if type(first) is not type(second):
        return False
    if isinstance(first, NameExpr) and isinstance(second, NameExpr):
        return first.name == second.name and first.node is second.node
    if isinstance(first, MemberExpr) and isinstance(second, MemberExpr):
        return first.name == second.name and _same(first.expr, second.expr)
    if isinstance(first, StrExpr | IntExpr) and isinstance(second, StrExpr | IntExpr):
        return first.value == second.value
    if isinstance(first, CallExpr) and isinstance(second, CallExpr):
        return (
            _same(first.callee, second.callee)
            and first.arg_names == second.arg_names
            and first.arg_kinds == second.arg_kinds
            and all(_same(one, two) for one, two in zip(first.args, second.args, strict=True))
        )
    return False


def _item(expression: Expression) -> tuple[Expression, Expression] | None:
    """``m.get(k)`` (one argument) and ``m[k]``: the mapping and the key."""
    if isinstance(expression, IndexExpr):
        return expression.base, expression.index
    if (
        isinstance(expression, CallExpr)
        and isinstance(expression.callee, MemberExpr)
        and expression.callee.name == "get"
        and len(expression.args) == 1
        and expression.arg_names == [None]
    ):
        return expression.callee.expr, expression.args[0]
    return None


def _contains(node: Statement | Expression | Block, target: Expression) -> bool:
    """Whether a node's lines hold the target (lines and columns of the source)."""
    start = (node.line, node.column)
    end = (node.end_line or node.line, node.end_column if node.end_column is not None else 10**9)
    where = (target.line, target.column)
    return start <= where <= end


def _blocks(statement: Statement) -> list[Block]:
    if isinstance(statement, ForStmt):
        return [statement.body, *([statement.else_body] if statement.else_body else [])]
    if isinstance(statement, WithStmt):
        return [statement.body]
    if isinstance(statement, TryStmt):
        found = [statement.body, *statement.handlers]
        return found + [block for block in (statement.else_body, statement.finally_body) if block]
    if isinstance(statement, MatchStmt):
        return list(statement.bodies)
    if isinstance(statement, Block):
        return [statement]
    return []


def _expressions(statement: Statement) -> list[Expression]:
    """The expressions a simple statement (or the head of a compound one) evaluates."""
    if isinstance(statement, AssignmentStmt):
        return [statement.rvalue, *statement.lvalues]
    if isinstance(statement, OperatorAssignmentStmt):
        return [statement.rvalue, statement.lvalue]
    if isinstance(statement, ExpressionStmt):
        return [statement.expr]
    if isinstance(statement, ReturnStmt | RaiseStmt):
        return [statement.expr] if statement.expr is not None else []
    if isinstance(statement, AssertStmt):
        return [statement.expr, *([statement.msg] if statement.msg is not None else [])]
    if isinstance(statement, ForStmt):
        return [statement.expr]
    if isinstance(statement, WithStmt):
        return list(statement.expr)
    if isinstance(statement, MatchStmt):
        return [statement.subject]
    return []


def _children(expression: Expression) -> list[Expression]:
    """The expressions directly inside an expression (lambda bodies too)."""
    if isinstance(expression, CallExpr):
        return [expression.callee, *expression.args]
    if isinstance(expression, MemberExpr):
        return [expression.expr]
    if isinstance(expression, IndexExpr):
        return [expression.base, expression.index]
    if isinstance(expression, OpExpr):
        return [expression.left, expression.right]
    if isinstance(expression, ComparisonExpr):
        return list(expression.operands)
    if isinstance(expression, UnaryExpr):
        return [expression.expr]
    if isinstance(expression, ConditionalExpr):
        return [expression.cond, expression.if_expr, expression.else_expr]
    if isinstance(expression, ListExpr | TupleExpr):
        return list(expression.items)
    if isinstance(expression, DictExpr):
        return [item for pair in expression.items for item in pair if item is not None]
    if isinstance(expression, LambdaExpr):
        return [expression.expr()]
    return []
