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

from dataclasses import dataclass, replace
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
    ReturnStmt,
    Statement,
    SymbolNode,
    TryStmt,
    TupleExpr,
    WhileStmt,
    WithStmt,
)

__all__ = ["MAX_DEPTH", "Built", "Change", "LocalValues", "Path"]

# How many variables deep a value is followed (``vals_list.append(vals)``,
# ``domain = expression.AND([domain, [...]])``).
MAX_DEPTH: Final = 3

# Methods that change a dict or a list in place.
CHANGING_METHODS: Final = frozenset({"append", "extend", "insert", "setdefault", "update"})
# ``x += v``, ``x |= v`` (a dict merge, or ``Domain`` OR), ``x &= v`` (``Domain`` AND).
IN_PLACE_OPERATORS: Final = {"+": "__iadd__", "|": "__ior__", "&": "__iand__"}


# The compound statements a statement is in, outermost first: ``("loop", id, 0)`` for the
# body of a ``for`` / ``while``, ``("if", id, arm)`` / ``("try", id, arm)`` for the arm of
# an ``if`` (``elif`` arms are nested ``if`` statements) or of a ``try`` (0: its body and
# ``else``, 1...: its handlers). Two statements in other arms of one statement never both
# run; one in a loop may run many times.
Path: TypeAlias = tuple[tuple[str, int, int], ...]


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
        end_line: The last line of its statement.
        path: The loops and branches it is in (:data:`Path`).
    """

    method: str
    args: tuple[Expression, ...]
    arg_names: tuple[str | None, ...]
    line: int
    end_line: int = 0
    path: Path = ()

    @property
    def end(self) -> int:
        """The last line of the statement (its first line if not known)."""
        return max(self.end_line, self.line)


@dataclass(frozen=True, slots=True)
class Built:
    """What a variable holds at a line, as far as its function shows.

    Attributes:
        value: The value of its last binding before the line; ``None`` if there is none or
            its value is unknown.
        line: The line of that binding (0 if there is none).
        changes: The in-place changes after that binding and before the line.
        path: The loops and branches the binding is in (:data:`Path`).
    """

    value: Expression | None
    line: int
    changes: tuple[Change, ...]
    path: Path = ()


NOTHING: Final = Built(None, 0, ())

_Key: TypeAlias = SymbolNode | str


@dataclass(frozen=True, slots=True)
class _Binding:
    value: Expression | None
    line: int
    end_line: int = 0
    path: Path = ()

    @property
    def end(self) -> int:
        return max(self.end_line, self.line)


class LocalValues:
    """The bindings and changes of the variables of one function, collected once.

    Attributes:
        function: The function.
        body: Its body when collected: the daemon merges a new definition into the old
            function object (``astmerge``) with a new body, so a cache of these values
            compares the body too.
        returns: Its ``return`` statements (not those of nested functions).
    """

    def __init__(self, function: FuncItem) -> None:
        self.function = function
        self.body = function.body
        collector = _Collector()
        collector.block(function.body)
        self._events = collector.events
        self._loops = tuple(collector.loops)
        self.returns: tuple[ReturnStmt, ...] = tuple(collector.returns)

    def bindings(self, name: NameExpr) -> list[Expression | None]:
        """The values of every binding of a variable in the function (``None``: unknown)."""
        events = self._events.get(_key(name), [])
        return [event.value for event in events if isinstance(event, _Binding)]

    def bindings_before(self, name: NameExpr, line: int) -> list[Expression | None] | None:
        """The values of the bindings of a variable before a line (``None`` items: unknown).

        A binding counts when its statement ends before the line: one whose value holds
        the name itself (a multi-line ``domain = [... domain ...]``) gives the value after
        it. ``None`` if a binding on that line or later is in a loop that also runs
        the line (``for ...: search(d); d = ...``): the value at the line is then not known.
        """
        found: list[Expression | None] = []
        for event in self._events.get(_key(name), []):
            if not isinstance(event, _Binding):
                continue
            if event.end < line:
                found.append(event.value)
            elif any(
                start <= min(event.line, line) and max(event.end, line) <= end
                for start, end in self._loops
            ):
                return None
        return found

    def built(self, name: NameExpr, line: int) -> Built:
        """What the variable ``name`` holds before ``line``.

        Only statements that end before the line count: one that holds the name (a
        multi-line ``domain = [... domain ...]`` or ``domain.append(... domain ...)``)
        runs after the name is read.
        """
        events = self._events.get(_key(name), [])
        binding: _Binding | None = None
        changes: list[Change] = []
        for event in sorted(events, key=lambda event: event.line):
            if event.line >= line:
                break
            if event.end >= line:
                continue  # the statement that holds the name
            if isinstance(event, _Binding):
                binding = event
                changes = []
            else:
                changes.append(event)
        if binding is None:
            return Built(None, 0, tuple(changes))
        return Built(binding.value, binding.line, tuple(changes), binding.path)


def _key(name: NameExpr) -> _Key:
    return name.node if name.node is not None else name.name


class _Collector:
    """Collect the bindings and in-place changes of simple names, by variable.

    A plain walk of the statements: compiled mypy does not let interpreted classes
    subclass its visitors. Calls count as changes only as statements of their own.
    """

    def __init__(self) -> None:
        self.events: dict[_Key, list[_Binding | Change]] = {}
        self.returns: list[ReturnStmt] = []
        self.loops: list[tuple[int, int]] = []  # first and last line of each loop
        self._path: list[tuple[str, int, int]] = []

    def _within(self, kind: str, statement: Statement, arm: int, block: Block | None) -> None:
        """Collect a block of a compound statement, with the statement on the path."""
        self._path.append((kind, id(statement), arm))
        self.block(block)
        self._path.pop()

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
            self._call(statement.expr, statement.end_line or statement.line)
        elif isinstance(statement, ReturnStmt):
            self.returns.append(statement)
        elif isinstance(statement, (ForStmt, WhileStmt)):
            self.loops.append((statement.line, statement.end_line or statement.line))
            if isinstance(statement, ForStmt):
                self._bind_targets(statement.index, statement.line)
            self._within("loop", statement, 0, statement.body)
            self.block(statement.else_body)
        elif isinstance(statement, IfStmt):
            for arm, body in enumerate(statement.body):
                self._within("if", statement, arm, body)
            self._within("if", statement, len(statement.body), statement.else_body)
        elif isinstance(statement, WithStmt):
            for target in statement.target:
                if target is not None:
                    self._bind_targets(target, statement.line)
            self.block(statement.body)
        elif isinstance(statement, TryStmt):
            self._within("try", statement, 0, statement.body)
            for arm, handler in enumerate(statement.handlers, start=1):
                self._within("try", statement, arm, handler)
            self._within("try", statement, 0, statement.else_body)
            self.block(statement.finally_body)
        elif isinstance(statement, Block):
            self.block(statement)

    def _add(self, name: NameExpr, event: _Binding | Change) -> None:
        self.events.setdefault(_key(name), []).append(replace(event, path=tuple(self._path)))

    def _bind_targets(self, target: Expression, line: int) -> None:
        """Mark the names of an unpacking target (``a, x = ...``) as unknown values."""
        if isinstance(target, NameExpr):
            self._add(target, _Binding(None, line))
        elif isinstance(target, (TupleExpr, ListExpr)):
            for item in target.items:
                self._bind_targets(item, line)

    def _assignment(self, statement: AssignmentStmt) -> None:
        end = statement.end_line or statement.line
        for target in statement.lvalues:
            if isinstance(target, NameExpr):
                self._add(target, _Binding(statement.rvalue, statement.line, end))
            elif isinstance(target, IndexExpr) and isinstance(target.base, NameExpr):
                args = (target.index, statement.rvalue)
                change = Change("__setitem__", args, (None, None), statement.line, end)
                self._add(target.base, change)
            else:
                self._bind_targets(target, statement.line)

    def _operator_assignment(self, statement: OperatorAssignmentStmt) -> None:
        if not isinstance(statement.lvalue, NameExpr):
            return
        method = IN_PLACE_OPERATORS.get(statement.op)
        end = statement.end_line or statement.line
        if method is not None:
            change = Change(method, (statement.rvalue,), (None,), statement.line, end)
            self._add(statement.lvalue, change)
        else:
            self._add(statement.lvalue, _Binding(None, statement.line, end))

    def _call(self, call: CallExpr, end: int) -> None:
        callee = call.callee
        if (
            isinstance(callee, MemberExpr)
            and isinstance(callee.expr, NameExpr)
            and callee.name in CHANGING_METHODS
        ):
            names = tuple(call.arg_names)
            change = Change(callee.name, tuple(call.args), names, call.line, end)
            self._add(callee.expr, change)
