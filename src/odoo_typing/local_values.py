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

from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Final, TypeAlias

from mypy.nodes import (
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
    IndexExpr,
    ListComprehension,
    ListExpr,
    MatchStmt,
    MemberExpr,
    NameExpr,
    OperatorAssignmentStmt,
    OpExpr,
    RaiseStmt,
    ReturnStmt,
    SetExpr,
    Statement,
    SymbolNode,
    TryStmt,
    TupleExpr,
    Var,
    WhileStmt,
    WithStmt,
)
from mypy.patterns import (
    AsPattern,
    ClassPattern,
    MappingPattern,
    OrPattern,
    Pattern,
    SequencePattern,
    StarredPattern,
)
from mypy.types import AnyType, UnionType, get_proper_type

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

# Statements of which one arm runs: a binding in another arm of one that holds the line
# read does not reach it (``try`` handlers do follow a part of the body).
EXCLUSIVE: Final = frozenset({"if", "match"})


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
        self._arms = tuple(collector.arms)
        self._ending = frozenset(collector.ending)
        self._stored: frozenset[int] | None = None  # see stored()
        self.returns: tuple[ReturnStmt, ...] = tuple(collector.returns)

    def stored(self, expression: Expression) -> bool:
        """Whether an expression gives the first value of a variable that gets several.

        mypy takes a variable's type from its first value: ``date = move.date`` where
        ``date`` is assigned again, an item of the first tuple appended to an empty
        list. A value that is typed more narrowly there makes a later one an error; the
        later values fit either way.

        Not so for a variable declared ``Any`` (:mod:`odoo_typing.rebound` declares those a
        function binds more than once): mypy narrows it to each value assigned, and no
        later value is checked against the first. Its container's items still are, so a
        display or an empty first value counts. Nor for a value that only in-place
        operators change (``name = record.name``, then ``name += " (x)"``): what ``+=``
        gives depends on the value, not on the variable.
        """
        if self._stored is None:
            found: set[int] = set()
            for key, events in self._events.items():
                typing = _typing_value(key, events)
                if typing is not None:
                    _carried(typing, found)
            self._stored = frozenset(found)
        return id(expression) in self._stored

    def bindings(self, name: NameExpr) -> list[Expression | None]:
        """The values of every binding of a variable in the function (``None``: unknown)."""
        events = self._events.get(_key(name), [])
        return [event.value for event in events if isinstance(event, _Binding)]

    def changes(self, name: NameExpr) -> list[Change]:
        """The in-place changes of a variable in the function (``x[k] = v``, ``x.update()``)."""
        return [event for event in self._events.get(_key(name), []) if isinstance(event, Change)]

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
        here = self._arms_at(line)
        for event in sorted(events, key=lambda event: event.line):
            if event.line >= line:
                break
            if event.end >= line:
                continue  # the statement that holds the name
            if any(
                here.get(key, arm) != arm or (key not in here and (key, arm) in self._ending)
                for kind, key, arm in event.path
                if kind in EXCLUSIVE
            ):
                continue  # another arm of an ``if`` / ``match``, or one that returns
            if isinstance(event, _Binding):
                binding = event
                changes = []
            else:
                changes.append(event)
        if binding is None:
            return Built(None, 0, tuple(changes))
        return Built(binding.value, binding.line, tuple(changes), binding.path)

    def _arms_at(self, line: int) -> dict[int, int]:
        """The arm of each ``if`` / ``match`` statement whose arm holds a line."""
        return {key: arm for key, arm, first, last in self._arms if first <= line <= last}


def _is_empty(event: _Binding | Change) -> bool:
    """Whether a binding gives no type yet: ``x = []``, ``x = {}``, ``x = None``, ``x = list()``."""
    value = event.value if isinstance(event, _Binding) else None
    if isinstance(value, ListExpr | SetExpr | DictExpr):
        return not value.items
    if isinstance(value, NameExpr):
        return value.name == "None"
    return isinstance(value, CallExpr) and not value.args and isinstance(value.callee, NameExpr)


def _typing_value(key: _Key, events: list[_Binding | Change]) -> _Binding | Change | None:
    """The binding or change whose value types a variable that later values are checked against."""
    if len(events) < 2:
        return None
    first = events[0]
    if _is_empty(first):  # ``rows = []``: the next value types it
        return events[1] if _gives_false(events[2:]) else None
    if _holds_items(first):
        return first
    rebound = any(isinstance(event, _Binding) for event in events[1:])
    return first if rebound and not _declared_any(key) else None


def _gives_false(events: list[_Binding | Change]) -> bool:
    """Whether a later value of a container is ``False`` or ``None`` as written.

    ``rows = []``, ``rows.append((tag.display_name, ...))``, then
    ``rows.append((False, ...))``: the list holds the empty value on purpose, so its
    first item keeps the type Odoo declares. Values of fields keep their own type: a
    field that may be empty, added after a required one, is reported where it is added.
    """
    pending = [
        value
        for event in events
        for value in ([event.value] if isinstance(event, _Binding) else list(event.args))
        if value is not None
    ]
    while pending:
        node = pending.pop()
        if isinstance(node, NameExpr) and node.name in {"False", "None"}:
            return True
        if isinstance(node, ListExpr | TupleExpr | SetExpr):
            pending.extend(node.items)
        elif isinstance(node, DictExpr):
            pending.extend(value for _, value in node.items)
        elif isinstance(node, ConditionalExpr):
            pending.extend((node.if_expr, node.else_expr))
        elif isinstance(node, OpExpr) and node.op in {"and", "or"}:
            pending.extend((node.left, node.right))
    return False


def _holds_items(event: _Binding | Change) -> bool:
    """Whether a binding gives a container whose items later changes are checked against."""
    value = event.value if isinstance(event, _Binding) else None
    return isinstance(value, ListExpr | TupleExpr | SetExpr | DictExpr | ListComprehension)


def _declared_any(key: _Key) -> bool:
    """Whether a variable is declared ``Any`` (or ``Any | Literal[False]``)."""
    declared = get_proper_type(key.type) if isinstance(key, Var) and key.type is not None else None
    if isinstance(declared, UnionType):
        return any(isinstance(get_proper_type(item), AnyType) for item in declared.items)
    return isinstance(declared, AnyType)


def _carried(event: _Binding | Change, found: set[int]) -> None:
    """Collect the expressions whose types a binding or a change carries into a variable.

    The value itself and, through it, the items of displays, the arms of a conditional
    expression or of ``and`` / ``or``, and the element of a list comprehension.
    """
    values = [event.value] if isinstance(event, _Binding) else list(event.args)
    pending = [value for value in values if value is not None]
    while pending:
        node = pending.pop()
        found.add(id(node))
        if isinstance(node, ListExpr | TupleExpr | SetExpr):
            pending.extend(node.items)
        elif isinstance(node, DictExpr):
            pending.extend(value for _, value in node.items)
        elif isinstance(node, ConditionalExpr):
            pending.extend((node.if_expr, node.else_expr))
        elif isinstance(node, OpExpr) and node.op in {"and", "or"}:
            pending.extend((node.left, node.right))
        elif isinstance(node, ListComprehension):
            pending.append(node.generator.left_expr)


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
        # The arms of ``if`` / ``match`` statements: (statement, arm, first line, last line).
        self.arms: list[tuple[int, int, int, int]] = []
        self.ending: set[tuple[int, int]] = set()  # arms whose last statement returns or raises
        self._path: list[tuple[str, int, int]] = []

    def _within(self, kind: str, statement: Statement, arm: int, block: Block | None) -> None:
        """Collect a block of a compound statement, with the statement on the path."""
        if kind in EXCLUSIVE and block is not None and block.body:
            last = block.body[-1]
            self.arms.append((id(statement), arm, block.body[0].line, last.end_line or last.line))
            if isinstance(last, ReturnStmt | RaiseStmt):
                self.ending.add((id(statement), arm))
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
        elif isinstance(statement, MatchStmt):
            # Its cases are alternatives, like the arms of an ``if``; the names a pattern
            # captures (``case {"x": vals}``) get unknown values.
            cases = zip(statement.patterns, statement.bodies, strict=True)
            for arm, (pattern, body) in enumerate(cases):
                for name in _captured(pattern):
                    self._add(name, _Binding(None, pattern.line))
                self._within("match", statement, arm, body)
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


def _captured(pattern: Pattern) -> Iterator[NameExpr]:
    """The names a ``case`` pattern binds (``case [x, *rest]``, ``case {"k": v} as d``)."""
    if isinstance(pattern, AsPattern):
        if pattern.name is not None:
            yield pattern.name
        if pattern.pattern is not None:
            yield from _captured(pattern.pattern)
    elif isinstance(pattern, StarredPattern):
        if pattern.capture is not None:
            yield pattern.capture
    elif isinstance(pattern, OrPattern | SequencePattern):
        for item in pattern.patterns:
            yield from _captured(item)
    elif isinstance(pattern, MappingPattern):
        for item in pattern.values:
            yield from _captured(item)
        if pattern.rest is not None:
            yield pattern.rest
    elif isinstance(pattern, ClassPattern):
        for item in (*pattern.positionals, *pattern.keyword_values):
            yield from _captured(item)
