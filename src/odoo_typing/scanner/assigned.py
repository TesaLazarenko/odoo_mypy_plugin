"""What the methods of a class assign to the attributes of records.

``record.move_data = json.dumps(...)`` in ``_compute_move_data``: the value a compute
method gives a field. A field that is computed at each read holds what its compute
methods assigned (:mod:`odoo_typing.computed`), so the scan keeps, per method and
attribute name, what kind each assigned value is:

* a string by its syntax (:data:`TEXT`): a literal, an f-string, ``"%s" % x``,
  ``"a" + x``, ``"{}".format(x)``, ``", ".join(x)``;
* a value that is neither ``None`` nor ``False``, of a type the scan cannot tell
  (:data:`TRUTHY`): an operand of ``a or b`` before the last, which is its value only
  where it is true (``url or "#"``);
* the call of a name of the module (``json.dumps``, ``str``): the dotted name, as
  written. What it returns is known where the module's names are (mypy's symbol table);
* anything else: :data:`ANY_VALUE`. Of these only the attribute's name is kept, per class.

Counted as assignments are ``x.name = value`` and ``setattr(x, "name", value)``, whatever
``x`` is: the scan knows no types, and a value it counts for a field that another object
got only makes the answer more careful. ``x["name"] = value`` and ``x.update({"name":
value})`` / ``x.write({...})`` with a dict display are mostly a dict's, so they count
where ``x`` starts at the method's records: its first parameter, or a name the method
binds (``=``, ``for``) to an expression that starts at one (``for record in self:``,
``lines = self.line_ids``). A name the scan cannot read (``setattr(x, name, value)``,
``**values`` in the display) is :data:`ANY_NAME`. A value that is not known, written to
the records that such a name reaches through attributes (``menu.page_id.write({"url":
menu.url})``), is kept with the attributes: the fields of the model tell whether these are
records of another model.

The scan also keeps which attributes a method assigns, on every path, to each of its
records (:func:`filled_attributes`): a record a compute method leaves without a value
reads ``False`` where Odoo does not raise for it.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Final, NamedTuple

__all__ = [
    "ANY_NAME",
    "ANY_VALUE",
    "OVERRIDDEN",
    "TEXT",
    "TRUTHY",
    "Assigned",
    "Assignments",
    "assigned_values",
    "filled_attributes",
]

TEXT: Final = ""  # a string by its syntax
TRUTHY: Final = "+"  # neither None nor False, of a type the scan cannot tell
ANY_VALUE: Final = "?"  # a value the scan cannot tell
ANY_NAME: Final = "*"  # an attribute whose name the scan cannot read
OVERRIDDEN: Final = "^"  # filled: the method calls the one it overrides on every path
MARKS: Final = frozenset({TEXT, TRUTHY, ANY_VALUE})  # the kinds that are no call
WRITES: Final = frozenset({"update", "write"})
TEXT_METHODS: Final = frozenset({"format", "join"})
SETATTR_ARGUMENTS: Final = 3
BLOCKS: Final = ("body", "orelse", "finalbody")

# (method, attribute, kinds): the kinds of the values a method assigns to an attribute.
Assigned = tuple[str, str, tuple[str, ...]]


class Assignments(NamedTuple):
    """What the methods of a class body assign (see the module).

    Attributes:
        known: ``(method, attribute, kinds)`` of the attributes whose values are all
            known, in source order with the kinds sorted, and of :data:`ANY_NAME`.
        unknown: The attributes some method assigns a value that is not known, sorted.
        through: ``(attribute, path)`` of the values that are not known, written to the
            records the method's records reach through the attributes of ``path``, sorted.
        filled: ``(method, attributes)``: the attributes with known values a method
            assigns to each of its records on every path, and :data:`OVERRIDDEN`, sorted.
    """

    known: tuple[Assigned, ...]
    unknown: tuple[str, ...]
    through: tuple[tuple[str, tuple[str, ...]], ...]
    filled: tuple[tuple[str, tuple[str, ...]], ...]


def assigned_values(node: ast.ClassDef) -> Assignments:
    """The kinds of the values each method of a class body assigns to each attribute."""
    known: list[Assigned] = []
    unknown: set[str] = set()
    through: set[tuple[str, tuple[str, ...]]] = set()
    filled: list[tuple[str, tuple[str, ...]]] = []
    for statement in node.body:
        if not isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        writes, bindings = _writes(statement.body)
        records = _records(statement, bindings)
        values: dict[str, set[str]] = {}
        for name, value, receiver, of_records in writes:
            start = _start(receiver)
            if of_records and start not in records:
                continue
            kinds = _kinds(value) if value is not None else {ANY_VALUE}
            path = _path(receiver) if start in records else None
            if path and name != ANY_NAME and ANY_VALUE in kinds:
                through.add((name, path))  # the records of a field: maybe another model's
                continue
            values.setdefault(name, set()).update(kinds)
        _drop_local_calls(statement, values)
        given: set[str] = set()
        for name, kinds in values.items():
            if name == ANY_NAME:
                known.append((statement.name, name, (ANY_VALUE,)))
            elif ANY_VALUE in kinds:
                unknown.add(name)
            else:
                known.append((statement.name, name, tuple(sorted(kinds))))
                given.add(name)
        # An attribute with a value that is not known is empty anyway: not followed.
        if given or _hands_on(statement):
            fills = filled_attributes(statement) & (given | {OVERRIDDEN})
            if fills:
                filled.append((statement.name, tuple(sorted(fills))))
    return Assignments(tuple(known), tuple(sorted(unknown)), tuple(sorted(through)), tuple(filled))


def _drop_local_calls(
    function: ast.FunctionDef | ast.AsyncFunctionDef, values: dict[str, set[str]]
) -> None:
    """Make unknown the values that are calls of names the function binds itself.

    ``url.geturl()`` of a local ``url``, ``self._name_of(record)``: no name of the module.
    """
    called = [kinds for kinds in values.values() if kinds - MARKS]
    if not called:
        return
    bound = _bound_names(function)
    for kinds in called:
        if any(kind.partition(".")[0] in bound for kind in kinds - MARKS):
            kinds.add(ANY_VALUE)


# (name, value, receiver, of_records): the value is ``None`` where the statement gives
# none of its own (``x.name += 1``, an item of an unpacked value); the receiver is the
# expression written to; ``of_records`` for the item and ``update`` writes, which count
# only where the receiver starts at the method's records.
Write = tuple[str, ast.expr | None, ast.expr, bool]


def _writes(statements: Iterable[ast.stmt]) -> tuple[list[Write], list[tuple[str, str]]]:
    """The assignments of some statements and of those inside them, and the names bound.

    Returns:
        The writes, and ``(name, start)`` for each name that ``=`` or ``for`` binds to
        an expression that starts at a name.
    """
    writes: list[Write] = []
    bindings: list[tuple[str, str]] = []
    pending = list(statements)
    while pending:
        statement = pending.pop()
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                writes.extend(_targets(target, statement.value))
                _bind(target, statement.value, bindings)
        elif isinstance(statement, ast.AnnAssign):
            if statement.value is not None:
                writes.extend(_targets(statement.target, statement.value))
                _bind(statement.target, statement.value, bindings)
        elif isinstance(statement, ast.AugAssign):
            writes.extend(_targets(statement.target, None))
        elif isinstance(statement, ast.Expr):
            if isinstance(statement.value, ast.Call):
                writes.extend(_written(statement.value))
        else:
            if isinstance(statement, ast.For | ast.AsyncFor):
                _bind(statement.target, statement.iter, bindings)
            for block in BLOCKS:
                pending.extend(getattr(statement, block, ()))
            for handler in getattr(statement, "handlers", ()):
                pending.extend(handler.body)
            for case in getattr(statement, "cases", ()):
                pending.extend(case.body)
    return writes, bindings


def _bind(target: ast.expr, value: ast.expr, bindings: list[tuple[str, str]]) -> None:
    start = _start(value)
    if isinstance(target, ast.Name) and start is not None:
        bindings.append((target.id, start))


def _start(node: ast.expr) -> str | None:
    """The name an expression starts at: ``self`` of ``self.line_ids.filtered(...)[0]``."""
    while isinstance(node, ast.Attribute | ast.Subscript | ast.Call):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def _path(node: ast.expr) -> tuple[str, ...] | None:
    """The attributes of a name and its attributes: ``("page_id",)`` of ``menu.page_id``."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return tuple(reversed(parts)) if isinstance(node, ast.Name) else None


def _records(
    function: ast.FunctionDef | ast.AsyncFunctionDef, bindings: list[tuple[str, str]]
) -> set[str]:
    """The names of a method that start at its first parameter (see the module)."""
    parameters = function.args.posonlyargs + function.args.args
    records = {parameters[0].arg} if parameters else set()
    changed = bool(records)
    while changed:
        changed = False
        for name, start in bindings:
            if start in records and name not in records:
                records.add(name)
                changed = True
    return records


def _targets(target: ast.expr, value: ast.expr | None) -> Iterator[Write]:
    if isinstance(target, ast.Attribute):
        yield target.attr, value, target.value, False
    elif isinstance(target, ast.Subscript):
        name = _text(target.slice)
        if name is not None:  # ``x[key] = value`` is mostly a dict's: not counted
            yield name, value, target.value, True
    elif isinstance(target, ast.Tuple | ast.List):
        items: list[ast.expr | None] = [None] * len(target.elts)
        if isinstance(value, ast.Tuple | ast.List) and len(value.elts) == len(target.elts):
            items = list(value.elts)
        for item, given in zip(target.elts, items, strict=True):
            yield from _targets(item, given)


def _written(call: ast.Call) -> Iterator[Write]:
    """``x.update({...})`` / ``x.write({...})`` and ``setattr(x, name, value)``."""
    function = call.func
    if isinstance(function, ast.Attribute) and function.attr in WRITES and call.args:
        display = call.args[0]
        if isinstance(display, ast.Dict):
            for key, value in zip(display.keys, display.values, strict=True):
                name = _text(key) if key is not None else None
                yield _item(name, value, function.value)
    elif (
        isinstance(function, ast.Name)
        and function.id == "setattr"
        and len(call.args) == SETATTR_ARGUMENTS
    ):
        name = _text(call.args[1])
        if name is None:
            yield ANY_NAME, None, call.args[0], False
        else:
            yield name, call.args[2], call.args[0], False


def _item(name: str | None, value: ast.expr, receiver: ast.expr) -> Write:
    """The write of an item of a display: :data:`ANY_NAME` of ``**values``."""
    return (name, value, receiver, True) if name is not None else (ANY_NAME, None, receiver, True)


def _text(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _kinds(value: ast.expr) -> set[str]:
    """The kinds of the values an expression gives (see the module)."""
    if isinstance(value, ast.Constant):
        return {TEXT} if isinstance(value.value, str) else {ANY_VALUE}
    if isinstance(value, ast.JoinedStr):
        return {TEXT}
    if isinstance(value, ast.IfExp):
        return _kinds(value.body) | _kinds(value.orelse)
    if isinstance(value, ast.NamedExpr):
        return _kinds(value.value)
    if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or):
        # An operand before the last is the value only where it is true.
        first = {
            TEXT if kind == TEXT else TRUTHY for item in value.values[:-1] for kind in _kinds(item)
        }
        return first | _kinds(value.values[-1])
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Mod | ast.Add):
        # A string on either side of ``+`` gives a string or raises; so does one before ``%``.
        left = _kinds(value.left)
        if ANY_VALUE not in left or isinstance(value.op, ast.Mod):
            return left
        return _kinds(value.right)
    if isinstance(value, ast.Call):
        function = value.func
        if (
            isinstance(function, ast.Attribute)
            and function.attr in TEXT_METHODS
            and ANY_VALUE not in (receiver := _kinds(function.value))
        ):
            return receiver  # a method of a string that gives a string
        dotted = _dotted(function)
        if dotted is not None:
            return {dotted}
    return {ANY_VALUE}


def _dotted(node: ast.expr) -> str | None:
    """``a.b.c`` of a name and its attributes."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _bound_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The names a function binds itself (also in the functions it defines)."""
    names: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store):
                names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).partition(".")[0])
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if node is not function:
                names.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def filled_attributes(function: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """The attributes a method assigns to each of its records on every path.

    ``self.name = value`` (also ``self["name"] = value``, ``setattr(self, "name", value)``,
    ``self.update({"name": value})`` / ``self.write(...)``) assigns all of them, ``for
    record in self:`` each one whose path through the loop's body assigns
    ``record.name``. A path counts where the method returns or reaches its end; one that
    raises gives no value to read. Paths are followed statement by statement: both arms
    of an ``if``; a ``continue`` ends a record's path, a ``break`` or a ``return`` in the
    loop leaves the records after it without a value. A body that may run no time (an
    inner loop) or be left at any statement (``with``, which may silence an exception;
    ``try``, whose handlers start where the ``try`` does) adds nothing; nor does a
    ``match``. :data:`OVERRIDDEN` stands for ``super().<method>()`` (or ``return`` of it)
    on every path. A method that binds its first parameter again, or a loop whose
    variable is bound in its body, assigns nothing for sure.
    """
    records = _first_parameter(function)
    if records is None or records in _stored_names(function.body):
        return frozenset()
    paths = _Paths(records, function.name, [])
    end = _block(function.body, frozenset(), paths)
    return _common([*paths.exits, end])


def _hands_on(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a statement of a method's body calls ``super()`` of the method."""
    paths = _Paths(_first_parameter(function) or "", function.name, [])
    return any(
        isinstance(statement, ast.Expr | ast.Return)
        and statement.value is not None
        and _overridden(statement.value, paths)
        for statement in function.body
    )


def _first_parameter(function: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    parameters = function.args.posonlyargs + function.args.args
    return parameters[0].arg if parameters else None


# What a path has assigned so far; ``None`` where no path reaches (it raised or left).
State = frozenset[str] | None


@dataclass(frozen=True, slots=True)
class _Paths:
    """Where the paths of a block lead (:func:`filled_attributes`).

    Attributes:
        receiver: The name assigned to: the method's first parameter, or the variable of
            ``for record in self`` in the loop's body.
        method: The method's name (its ``super()`` call).
        exits: What the paths that leave early assigned: those that return from the
            method, or in the loop's body those that end a record's path.
        returns: In the loop's body, the exits of the method; ``None`` outside of it.
        before: In the loop's body, what the method's path assigned before the loop.
        inner: In another loop: its ``continue`` and ``break`` are its own.
    """

    receiver: str
    method: str
    exits: list[frozenset[str]]
    returns: list[frozenset[str]] | None = None
    before: frozenset[str] = frozenset()
    inner: bool = False


def _block(statements: Sequence[ast.stmt], state: State, paths: _Paths) -> State:
    for statement in statements:
        if state is None:
            return None
        state = _statement(statement, state, paths)
    return state


def _statement(statement: ast.stmt, state: frozenset[str], paths: _Paths) -> State:
    if isinstance(statement, ast.Assign):
        return state | _assigned_to(statement.targets, paths.receiver)
    if isinstance(statement, ast.AnnAssign) and statement.value is not None:
        return state | _assigned_to([statement.target], paths.receiver)
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
        return state | _called(statement.value, paths)
    if isinstance(statement, ast.Return):
        _return(statement, state, paths)
        return None
    if isinstance(statement, ast.Continue | ast.Break):
        if paths.returns is not None and not paths.inner:  # a record's path ends
            paths.exits.append(state if isinstance(statement, ast.Continue) else frozenset())
        return None
    if isinstance(statement, ast.Raise):
        return None
    if isinstance(statement, ast.If):
        return _meet(_block(statement.body, state, paths), _block(statement.orelse, state, paths))
    if isinstance(statement, ast.For | ast.AsyncFor) and _over_records(statement, paths):
        return _records_loop(statement, state, paths)
    if isinstance(statement, ast.For | ast.AsyncFor | ast.While):
        inner = replace(paths, inner=True)
        _block(statement.body, state, inner)  # for the paths that leave the method in it
        _block(statement.orelse, state, inner)
        return state
    if isinstance(statement, ast.Try | ast.TryStar):
        done = _block(statement.orelse, _block(statement.body, state, paths), paths)
        handled = [_block(handler.body, state, paths) for handler in statement.handlers]
        return _block(statement.finalbody, _meet(done, *handled), paths)
    if isinstance(statement, ast.With | ast.AsyncWith):
        _block(statement.body, state, paths)
        return state
    if isinstance(statement, ast.Match):
        for case in statement.cases:
            _block(case.body, state, paths)
        return state
    return state


def _return(statement: ast.Return, state: frozenset[str], paths: _Paths) -> None:
    if paths.returns is not None:  # in the loop: the records after this one get nothing
        paths.exits.append(frozenset())
        paths.returns.append(paths.before)
        return
    if statement.value is not None and _overridden(statement.value, paths):
        state |= {OVERRIDDEN}
    paths.exits.append(state)


def _records_loop(statement: ast.For | ast.AsyncFor, state: frozenset[str], paths: _Paths) -> State:
    """``for record in self:``: what each record's path assigns, assigned to the records."""
    target = statement.target
    if not isinstance(target, ast.Name) or target.id in _stored_names(statement.body):
        return state
    loop = _Paths(target.id, paths.method, [], returns=paths.exits, before=state)
    end = _block(statement.body, frozenset(), loop)
    after = state | (_common([*loop.exits, end]) - {OVERRIDDEN})
    _block(statement.orelse, after, paths)  # for the paths that leave the method in it
    return after


def _over_records(statement: ast.For | ast.AsyncFor, paths: _Paths) -> bool:
    return (
        paths.returns is None
        and not paths.inner
        and isinstance(statement.iter, ast.Name)
        and statement.iter.id == paths.receiver
    )


def _assigned_to(targets: Iterable[ast.expr], receiver: str) -> frozenset[str]:
    """The attributes of ``receiver`` that assignment targets assign."""
    names: set[str] = set()
    pending = list(targets)
    while pending:
        target = pending.pop()
        if isinstance(target, ast.Tuple | ast.List):
            pending.extend(target.elts)
        elif isinstance(target, ast.Starred):
            pending.append(target.value)
        elif isinstance(target, ast.Attribute) and _is_name(target.value, receiver):
            names.add(target.attr)
        elif isinstance(target, ast.Subscript) and _is_name(target.value, receiver):
            name = _text(target.slice)
            if name is not None:
                names.add(name)
    return frozenset(names)


def _called(call: ast.Call, paths: _Paths) -> frozenset[str]:
    """What a call assigns to ``receiver``: ``setattr``, ``update`` / ``write``, ``super()``."""
    if _overridden(call, paths):
        return frozenset({OVERRIDDEN})
    names: set[str] = set()
    for name, _value, receiver, _of_records in _written(call):
        if name != ANY_NAME and _is_name(receiver, paths.receiver):
            names.add(name)
    return frozenset(names)


def _overridden(node: ast.expr, paths: _Paths) -> bool:
    """``super().<method>()`` or ``super(Class, self).<method>()`` of the method's records."""
    if paths.returns is not None or not isinstance(node, ast.Call):
        return False
    function = node.func
    if not isinstance(function, ast.Attribute) or function.attr != paths.method:
        return False
    owner = function.value
    return (
        isinstance(owner, ast.Call)
        and isinstance(owner.func, ast.Name)
        and owner.func.id == "super"
        and (not owner.args or _is_name(owner.args[-1], paths.receiver))
    )


def _is_name(node: ast.expr, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _meet(*states: State) -> State:
    """What every path that reaches assigned; ``None`` if none reaches."""
    reached = [state for state in states if state is not None]
    return frozenset.intersection(*reached) if reached else None


def _common(states: Iterable[State]) -> frozenset[str]:
    """What every path assigned, where some path ends (nothing if every one raises)."""
    return _meet(*states) or frozenset()


def _stored_names(statements: Iterable[ast.stmt]) -> set[str]:
    """The names some statements bind (``self = self.sudo()``, ``for x in``, ``with ... as``).

    Statement by statement, as the rest of the scan: not a name an expression binds
    (``:=``), nor one of a function defined inside.
    """
    names: set[str] = set()
    targets: list[ast.expr] = []
    pending = list(statements)
    while pending:
        statement = pending.pop()
        if isinstance(statement, ast.Assign):
            targets.extend(statement.targets)
        elif isinstance(statement, ast.AnnAssign | ast.AugAssign | ast.For | ast.AsyncFor):
            targets.append(statement.target)
        elif isinstance(statement, ast.With | ast.AsyncWith):
            targets.extend(item.optional_vars for item in statement.items if item.optional_vars)
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for block in BLOCKS:
            pending.extend(getattr(statement, block, ()))
        for handler in getattr(statement, "handlers", ()):
            pending.extend(handler.body)
        for case in getattr(statement, "cases", ()):
            pending.extend(case.body)
    while targets:
        target = targets.pop()
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Tuple | ast.List):
            targets.extend(target.elts)
        elif isinstance(target, ast.Starred):
            targets.append(target.value)
    return names
