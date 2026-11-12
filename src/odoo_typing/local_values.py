"""What a local variable was built from in its function: assignments, item sets, calls.

``vals = {...}; vals["x"] = 1; vals.update(y=2); self.create(vals)``: mypy types ``vals``
as ``dict[str, ...]`` and keeps no keys, so the checks of literal ``vals`` and domains
(:mod:`odoo_typing.vals`, :mod:`odoo_typing.domains`) look at the statements of the
function that built the variable before the line that uses it.

The flow is approximated by source order: the last statement before that line that
rebinds the variable (``x = ...``), then what changes it in place after that
(``x[k] = v``, ``x += [...]``, ``x.update(...)``, ``x.append(...)``...). A rebinding in
another branch hides the earlier ones (fewer checks, never keys of another value); a
binding whose value is unknown (``for x in ...``, ``a, x = ...``) is ``None``. Nested
functions and lambdas are not looked into, nor calls inside other expressions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, TypeAlias

from mypy.nodes import (
    AssignmentStmt,
    Block,
    CallExpr,
    Expression,
    ExpressionStmt,
    ForStmt,
    FuncItem,
    IfStmt,
    IndexExpr,
    ListExpr,
    MemberExpr,
    NameExpr,
    OperatorAssignmentStmt,
    Statement,
    SymbolNode,
    TryStmt,
    TupleExpr,
    WhileStmt,
    WithStmt,
)

__all__ = ["MAX_DEPTH", "Built", "Change", "LocalValues"]

# How many variables deep a value is followed (``vals_list.append(vals)``,
# ``domain = expression.AND([domain, [...]])``).
MAX_DEPTH: Final = 3

# Methods that change a dict or a list in place.
CHANGING_METHODS: Final = frozenset({"append", "extend", "insert", "setdefault", "update"})
# ``x += v``, ``x |= v`` (a dict merge, or ``Domain`` OR), ``x &= v`` (``Domain`` AND).
IN_PLACE_OPERATORS: Final = {"+": "__iadd__", "|": "__ior__", "&": "__iand__"}


@dataclass(frozen=True, slots=True)
class Change:
    """An in-place change of a variable.

    Attributes:
        method: The method called (``update``, ``append``...); ``__setitem__`` for
            ``x[k] = v`` (arguments ``k, v``), ``__iadd__`` / ``__ior__`` / ``__iand__``
            for ``x += v`` / ``x |= v`` / ``x &= v``.
        args: The arguments.
        arg_names: Their keyword names (``None`` for a positional one).
        line: The line of the change.
    """

    method: str
    args: tuple[Expression, ...]
    arg_names: tuple[str | None, ...]
    line: int


@dataclass(frozen=True, slots=True)
class Built:
    """What a variable holds at a line, as far as its function shows.

    Attributes:
        value: The value of its last binding before the line; ``None`` if there is none or
            its value is unknown.
        line: The line of that binding (0 if there is none).
        changes: The in-place changes after that binding and before the line.
    """

    value: Expression | None
    line: int
    changes: tuple[Change, ...]


NOTHING: Final = Built(None, 0, ())

_Key: TypeAlias = SymbolNode | str


@dataclass(frozen=True, slots=True)
class _Binding:
    value: Expression | None
    line: int


class LocalValues:
    """The bindings and changes of the variables of one function, collected once."""

    def __init__(self, function: FuncItem) -> None:
        self.function = function
        collector = _Collector()
        collector.block(function.body)
        self._events = collector.events

    def built(self, name: NameExpr, line: int) -> Built:
        """What the variable ``name`` holds before ``line``."""
        events = self._events.get(_key(name), [])
        binding: _Binding | None = None
        changes: list[Change] = []
        for event in sorted(events, key=lambda event: event.line):
            if event.line >= line:
                break
            if isinstance(event, _Binding):
                binding = event
                changes = []
            else:
                changes.append(event)
        if binding is None:
            return Built(None, 0, tuple(changes))
        return Built(binding.value, binding.line, tuple(changes))


def _key(name: NameExpr) -> _Key:
    return name.node if name.node is not None else name.name


class _Collector:
    """Collect the bindings and in-place changes of simple names, by variable.

    A plain walk of the statements: compiled mypy does not let interpreted classes
    subclass its visitors. Calls count as changes only as statements of their own.
    """

    def __init__(self) -> None:
        self.events: dict[_Key, list[_Binding | Change]] = {}

    def block(self, block: Block | None) -> None:
        if block is None:
            return
        for statement in block.body:
            self.statement(statement)

    def statement(self, statement: Statement) -> None:
        if isinstance(statement, AssignmentStmt):
            self._assignment(statement)
        elif isinstance(statement, OperatorAssignmentStmt):
            self._operator_assignment(statement)
        elif isinstance(statement, ExpressionStmt) and isinstance(statement.expr, CallExpr):
            self._call(statement.expr)
        elif isinstance(statement, (ForStmt, WhileStmt)):
            if isinstance(statement, ForStmt):
                self._bind_targets(statement.index, statement.line)
            self.block(statement.body)
            self.block(statement.else_body)
        elif isinstance(statement, IfStmt):
            for body in statement.body:
                self.block(body)
            self.block(statement.else_body)
        elif isinstance(statement, WithStmt):
            for target in statement.target:
                if target is not None:
                    self._bind_targets(target, statement.line)
            self.block(statement.body)
        elif isinstance(statement, TryStmt):
            self.block(statement.body)
            for handler in statement.handlers:
                self.block(handler)
            self.block(statement.else_body)
            self.block(statement.finally_body)
        elif isinstance(statement, Block):
            self.block(statement)

    def _add(self, name: NameExpr, event: _Binding | Change) -> None:
        self.events.setdefault(_key(name), []).append(event)

    def _bind_targets(self, target: Expression, line: int) -> None:
        """Mark the names of an unpacking target (``a, x = ...``) as unknown values."""
        if isinstance(target, NameExpr):
            self._add(target, _Binding(None, line))
        elif isinstance(target, (TupleExpr, ListExpr)):
            for item in target.items:
                self._bind_targets(item, line)

    def _assignment(self, statement: AssignmentStmt) -> None:
        for target in statement.lvalues:
            if isinstance(target, NameExpr):
                self._add(target, _Binding(statement.rvalue, statement.line))
            elif isinstance(target, IndexExpr) and isinstance(target.base, NameExpr):
                args = (target.index, statement.rvalue)
                self._add(target.base, Change("__setitem__", args, (None, None), statement.line))
            else:
                self._bind_targets(target, statement.line)

    def _operator_assignment(self, statement: OperatorAssignmentStmt) -> None:
        if not isinstance(statement.lvalue, NameExpr):
            return
        method = IN_PLACE_OPERATORS.get(statement.op)
        if method is not None:
            change = Change(method, (statement.rvalue,), (None,), statement.line)
            self._add(statement.lvalue, change)
        else:
            self._add(statement.lvalue, _Binding(None, statement.line))

    def _call(self, call: CallExpr) -> None:
        callee = call.callee
        if (
            isinstance(callee, MemberExpr)
            and isinstance(callee.expr, NameExpr)
            and callee.name in CHANGING_METHODS
        ):
            change = Change(callee.name, tuple(call.args), tuple(call.arg_names), call.line)
            self._add(callee.expr, change)
