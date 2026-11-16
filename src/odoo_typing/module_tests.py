"""The tests about installed modules in a function, on the tree mypy checks.

``if self.env["ir.module.module"]._get("sale").state == "installed":`` runs its body only
with ``sale``; so does the rest of a function after ``if ...: return`` (``raise``,
``continue``, ``break``, ``self.skipTest(...)``). A test is about modules when it holds
the string ``"ir.module.module"`` or ``"installed"``, also through a name the function
binds once (``module = self.env["ir.module.module"].search([("name", "=", "sale")])``,
then ``if module:``), or through an attribute that the methods of its class bind once
(``cls.has_documents = "documents" in modules._installed()`` in ``setUpClass``, then
``if self.has_documents:``). It names a module where Odoo's API takes one: the argument of
``_get("sale")`` / ``ensure_installed("sale")``, the left side of ``"sale" in
modules._installed()``, the value of a ``("name", "=", "sale")`` (``in``) term. A string
of another test or call (``journal.type == "sale"``, ``mapped("sale_id")``) is a value.
A statement ``self.ensure_installed("sale")`` skips the test without ``sale``: the rest of
its block runs with it.

The tests of a function are collected once, from mypy's own tree of it: nothing is
parsed again.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator
from typing import Final

from mypy.nodes import (
    FUNC_NO_INFO,
    AssignmentStmt,
    Block,
    BreakStmt,
    CallExpr,
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
    ListExpr,
    MemberExpr,
    NameExpr,
    OpExpr,
    OverloadedFuncDef,
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

__all__ = ["MODULE_TEST_MARKS", "ClassFlags", "ModuleTests"]

# A test that holds one of these asks which modules are installed.
MODULE_TEST_MARKS: Final = frozenset({"ir.module.module", "installed"})
# ``modules._get("sale")``, ``cls.ensure_installed("sale")``, ``modules._installed()``.
ENSURE_INSTALLED: Final = "ensure_installed"
MODULE_GETTERS: Final = frozenset({"_get", ENSURE_INSTALLED})
INSTALLED: Final = "_installed"
# ``self.skipTest(...)`` ends a test as ``return`` does (unittest raises ``SkipTest``).
SKIP_TEST: Final = "skipTest"


# The attributes of ``self`` that the methods of a class bind once, by class definition:
# read once per class (a test class has many methods), kept with the body they come from.
ClassFlags = dict[int, tuple[Block, dict[str, Expression]]]


class ModuleTests:
    """The tests about installed modules of one function, and the modules they name."""

    def __init__(self, function: FuncItem, class_flags: ClassFlags | None = None) -> None:
        self.function = function
        self.body = function.body  # the daemon merges a new body into the old function
        self._class_flags: ClassFlags = class_flags if class_flags is not None else {}
        located = list(_located(function.body))
        statements = [statement for _, statement in located]
        bound = [
            (target.name, statement.rvalue)
            for statement in statements
            if isinstance(statement, AssignmentStmt)
            for target in statement.lvalues
            if isinstance(target, NameExpr)
        ]
        counts = Counter(name for name, _ in bound)
        flags = {name: value for name, value in bound if counts[name] == 1}
        receiver = _receiver(function)
        self._attributes: dict[str, Expression] | None = None  # read once a test needs them
        self._tests: list[tuple[Block, frozenset[str]]] = []
        self._ensured: list[tuple[int, int, frozenset[str]]] = []
        for block, statement in located:
            names = frozenset(_ensured(statement))
            if names:
                last = block.body[-1]
                self._ensured.append((statement.line, last.end_line or last.line, names))
        for statement in statements:
            if not isinstance(statement, IfStmt):
                continue
            for test, block in zip(statement.expr, statement.body, strict=True):
                parts = [
                    test,
                    *(flags[name.name] for name in _names(test) if name.name in flags),
                    *self._attribute_values(_own_attributes(test, receiver)),
                ]
                nodes = [node for part in parts for node in _expressions(part)]
                strings = {node.value for node in nodes if isinstance(node, StrExpr)}
                if not strings.isdisjoint(MODULE_TEST_MARKS):
                    names = frozenset(name for node in nodes for name in _module_names(node))
                    self._tests.append((block, names))

    def _flags_of_class(self) -> dict[str, Expression]:
        """The attributes the methods of the function's class bind once (kept per class)."""
        info = self.function.info
        if info is FUNC_NO_INFO:
            return {}
        body = info.defn.defs
        kept = self._class_flags.get(id(info.defn))
        if kept is None or kept[0] is not body:
            kept = self._class_flags[id(info.defn)] = (body, _attribute_flags(body))
        return kept[1]

    def _attribute_values(self, members: Iterable[MemberExpr]) -> list[Expression]:
        """The values of the attributes of ``self`` a test reads, bound once in the class."""
        found: list[Expression] = []
        for member in members:
            if self._attributes is None:
                self._attributes = self._flags_of_class()
            if member.name in self._attributes:
                found.append(self._attributes[member.name])
        return found

    def names(self, line: int) -> frozenset[str]:
        """The modules named by the tests about modules that decide a line runs."""
        found: set[str] = set()
        for block, names in self._tests:
            if names and _decides(block, line):
                found |= names
        for first, last, names in self._ensured:
            if first < line <= last:
                found |= names
        return frozenset(found)


def _attribute_flags(body: Block) -> dict[str, Expression]:
    """The attributes of ``self`` / ``cls`` the methods of a class body bind once."""
    bound: list[tuple[str, Expression]] = []
    for method in _methods(body.body):
        receiver = _receiver(method)
        for statement in _statements(method.body):
            if not isinstance(statement, AssignmentStmt):
                continue
            bound += [
                (target.name, statement.rvalue)
                for target in statement.lvalues
                if isinstance(target, MemberExpr)
                and isinstance(target.expr, NameExpr)
                and target.expr.name == receiver
            ]
    counts = Counter(name for name, _ in bound)
    return {name: value for name, value in bound if counts[name] == 1}


def _methods(body: list[Statement]) -> Iterator[FuncDef]:
    for statement in body:
        if isinstance(statement, FuncDef):
            yield statement
        elif isinstance(statement, Decorator):
            yield statement.func
        elif isinstance(statement, OverloadedFuncDef):
            for item in statement.items:
                yield item.func if isinstance(item, Decorator) else item


def _receiver(function: FuncItem) -> str | None:
    """The name of a method's first argument (``self``, ``cls``)."""
    return function.arguments[0].variable.name if function.arguments else None


def _own_attributes(test: Expression, receiver: str | None) -> Iterator[MemberExpr]:
    """``self.x`` / ``cls.x`` in a test."""
    return (
        node
        for node in _expressions(test)
        if isinstance(node, MemberExpr)
        and isinstance(node.expr, NameExpr)
        and node.expr.name == receiver
    )


def _ensured(statement: Statement) -> Iterator[str]:
    """The modules of a statement ``self.ensure_installed("sale")``."""
    call = statement.expr if isinstance(statement, ExpressionStmt) else None
    if (
        isinstance(call, CallExpr)
        and isinstance(call.callee, MemberExpr)
        and call.callee.name == ENSURE_INSTALLED
    ):
        for argument in call.args:
            yield from _strings(argument)


def _located(block: Block) -> Iterator[tuple[Block, Statement]]:
    """The statements of a block and of the compound statements in it, with their block."""
    for statement in block.body:
        yield block, statement
        for nested in _blocks(statement):
            yield from _located(nested)


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
