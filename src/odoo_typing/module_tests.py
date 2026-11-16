"""The tests about installed modules in a function, on the tree mypy checks.

``if self.env["ir.module.module"]._get("sale").state == "installed":`` runs its body only
with ``sale``; so does the rest of a function after ``if ...: return`` (``raise``,
``continue``, ``break``, ``self.skipTest(...)``). A test is about modules when it holds
the string ``"ir.module.module"`` or ``"installed"``, also through a name the function
binds once (``module = self.env["ir.module.module"].search([("name", "=", "sale")])``,
then ``if module:``). It names a module where Odoo's API takes one: the argument of
``_get("sale")`` / ``ensure_installed("sale")``, the left side of ``"sale" in
modules._installed()``, the value of a ``("name", "=", "sale")`` (``in``) term. A string
of another test or call (``journal.type == "sale"``, ``mapped("sale_id")``) is a value.

The tests of a function are collected once, from mypy's own tree of it: nothing is
parsed again.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator
from typing import Final

from mypy.nodes import (
    AssignmentStmt,
    Block,
    BreakStmt,
    CallExpr,
    ComparisonExpr,
    ConditionalExpr,
    ContinueStmt,
    DictExpr,
    Expression,
    ExpressionStmt,
    ForStmt,
    FuncItem,
    IfStmt,
    IndexExpr,
    ListExpr,
    MemberExpr,
    NameExpr,
    OpExpr,
    RaiseStmt,
    ReturnStmt,
    SetExpr,
    Statement,
    StrExpr,
    TryStmt,
    TupleExpr,
    UnaryExpr,
    WhileStmt,
    WithStmt,
)

__all__ = ["MODULE_TEST_MARKS", "ModuleTests"]

# A test that holds one of these asks which modules are installed.
MODULE_TEST_MARKS: Final = frozenset({"ir.module.module", "installed"})
# ``modules._get("sale")``, ``cls.ensure_installed("sale")``, ``modules._installed()``.
MODULE_GETTERS: Final = frozenset({"_get", "ensure_installed"})
INSTALLED: Final = "_installed"
# ``self.skipTest(...)`` ends a test as ``return`` does (unittest raises ``SkipTest``).
SKIP_TEST: Final = "skipTest"


class ModuleTests:
    """The tests about installed modules of one function, and the modules they name."""

    def __init__(self, function: FuncItem) -> None:
        self.function = function
        self.body = function.body  # the daemon merges a new body into the old function
        statements = list(_statements(function.body))
        bound = [
            (target.name, statement.rvalue)
            for statement in statements
            if isinstance(statement, AssignmentStmt)
            for target in statement.lvalues
            if isinstance(target, NameExpr)
        ]
        counts = Counter(name for name, _ in bound)
        flags = {name: value for name, value in bound if counts[name] == 1}
        self._tests: list[tuple[Block, frozenset[str]]] = []
        for statement in statements:
            if not isinstance(statement, IfStmt):
                continue
            for test, block in zip(statement.expr, statement.body, strict=True):
                parts = [test, *(flags[n.name] for n in _names(test) if n.name in flags)]
                nodes = [node for part in parts for node in _expressions(part)]
                strings = {node.value for node in nodes if isinstance(node, StrExpr)}
                if not strings.isdisjoint(MODULE_TEST_MARKS):
                    names = frozenset(name for node in nodes for name in _module_names(node))
                    self._tests.append((block, names))

    def names(self, line: int) -> frozenset[str]:
        """The modules named by the tests about modules that decide a line runs."""
        found: set[str] = set()
        for block, names in self._tests:
            if names and _decides(block, line):
                found |= names
        return frozenset(found)


def _decides(block: Block, line: int) -> bool:
    """Whether the arm of an ``if`` holds a line, or ends the function before it."""
    if not block.body:
        return False
    last = block.body[-1]
    end = last.end_line or last.line
    if block.body[0].line <= line <= end:
        return True
    return _ends(last) and end < line


def _ends(statement: Statement) -> bool:
    """``return``, ``raise``, ``continue``, ``break``, or a call of ``skipTest``."""
    if isinstance(statement, (ReturnStmt, RaiseStmt, ContinueStmt, BreakStmt)):
        return True
    call = statement.expr if isinstance(statement, ExpressionStmt) else None
    return (
        isinstance(call, CallExpr)
        and isinstance(call.callee, MemberExpr)
        and call.callee.name == SKIP_TEST
    )


def _module_names(node: Expression) -> Iterator[str]:
    """The module names one node of a test gives (see the module docstring)."""
    if isinstance(node, CallExpr) and isinstance(node.callee, MemberExpr):
        if node.callee.name in MODULE_GETTERS and node.args:
            yield from _strings(node.args[0])
    elif isinstance(node, ComparisonExpr):
        operands = node.operands
        for operator, left, right in zip(node.operators, operands, operands[1:], strict=False):
            if operator in ("in", "not in") and _is_installed_call(right):
                yield from _strings(left)
    elif isinstance(node, TupleExpr) and len(node.items) == 3:
        field, relation, value = node.items
        if _string(field) == "name" and _string(relation) in ("=", "in"):
            yield from _strings(value)


def _is_installed_call(node: Expression) -> bool:
    return (
        isinstance(node, CallExpr)
        and isinstance(node.callee, MemberExpr)
        and node.callee.name == INSTALLED
    )


def _string(node: Expression) -> str | None:
    return node.value if isinstance(node, StrExpr) else None


def _strings(node: Expression) -> Iterator[str]:
    """A string, or the strings of a list, tuple or set display."""
    items = node.items if isinstance(node, (ListExpr, TupleExpr, SetExpr)) else [node]
    for item in items:
        if isinstance(item, StrExpr):
            yield item.value


def _names(test: Expression) -> Iterator[NameExpr]:
    return (node for node in _expressions(test) if isinstance(node, NameExpr))


def _expressions(node: Expression) -> Iterator[Expression]:
    """An expression and the expressions inside it."""
    pending = [node]
    while pending:
        item = pending.pop()
        yield item
        pending += _children(item)


def _children(node: Expression) -> list[Expression]:
    if isinstance(node, CallExpr):
        return [node.callee, *node.args]
    if isinstance(node, MemberExpr):
        return [node.expr]
    if isinstance(node, IndexExpr):
        return [node.base, node.index]
    if isinstance(node, OpExpr):
        return [node.left, node.right]
    if isinstance(node, ComparisonExpr):
        return list(node.operands)
    if isinstance(node, UnaryExpr):
        return [node.expr]
    if isinstance(node, (ListExpr, TupleExpr, SetExpr)):
        return list(node.items)
    if isinstance(node, DictExpr):
        return [item for pair in node.items for item in pair if item is not None]
    if isinstance(node, ConditionalExpr):
        return [node.cond, node.if_expr, node.else_expr]
    return []


def _statements(block: Block) -> Iterator[Statement]:
    """The statements of a block and of the compound statements in it."""
    for statement in block.body:
        yield statement
        yield from (inner for nested in _blocks(statement) for inner in _statements(nested))


def _blocks(statement: Statement) -> Iterable[Block]:
    if isinstance(statement, IfStmt):
        return [*statement.body, *([statement.else_body] if statement.else_body else [])]
    if isinstance(statement, (ForStmt, WhileStmt)):
        return [statement.body, *([statement.else_body] if statement.else_body else [])]
    if isinstance(statement, WithStmt):
        return [statement.body]
    if isinstance(statement, TryStmt):
        others = (statement.else_body, statement.finally_body)
        return [statement.body, *statement.handlers, *(block for block in others if block)]
    if isinstance(statement, Block):
        return [statement]
    return []
