"""What the methods of a class assign to the attributes of records.

``record.move_data = json.dumps(...)`` in ``_compute_move_data``: the value a compute
method gives a field. A field that is computed at each read holds what its compute
methods assigned (:mod:`odoo_typing.computed`), so the scan keeps, per method and
attribute name, what kind each assigned value is:

* a string by its syntax (:data:`TEXT`): a literal, an f-string, ``"%s" % x``,
  ``"a" + x``, ``"{}".format(x)``, ``", ".join(x)``;
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
``**values`` in the display) is :data:`ANY_NAME`.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from typing import Final

__all__ = ["ANY_NAME", "ANY_VALUE", "TEXT", "Assigned", "assigned_values"]

TEXT: Final = ""  # a string by its syntax
ANY_VALUE: Final = "?"  # a value the scan cannot tell
ANY_NAME: Final = "*"  # an attribute whose name the scan cannot read
WRITES: Final = frozenset({"update", "write"})
TEXT_METHODS: Final = frozenset({"format", "join"})
SETATTR_ARGUMENTS: Final = 3
BLOCKS: Final = ("body", "orelse", "finalbody")

# (method, attribute, kinds): the kinds of the values a method assigns to an attribute.
Assigned = tuple[str, str, tuple[str, ...]]


def assigned_values(node: ast.ClassDef) -> tuple[tuple[Assigned, ...], tuple[str, ...]]:
    """The kinds of the values each method of a class body assigns to each attribute.

    Returns:
        ``(method, attribute, kinds)`` of the attributes whose values are all known, in
        source order with the kinds sorted, and of :data:`ANY_NAME`; then the names of
        the attributes some method assigns a value that is not known, sorted.
    """
    known: list[Assigned] = []
    unknown: set[str] = set()
    for statement in node.body:
        if not isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        writes, bindings = _writes(statement.body)
        records = _records(statement, bindings)
        values: dict[str, set[str]] = {}
        for name, value, receiver in writes:
            if receiver is None or receiver in records:
                values.setdefault(name, set()).update(_kinds(value) if value else {ANY_VALUE})
        _drop_local_calls(statement, values)
        for name, kinds in values.items():
            if name == ANY_NAME:
                known.append((statement.name, name, (ANY_VALUE,)))
            elif ANY_VALUE in kinds:
                unknown.add(name)
            else:
                known.append((statement.name, name, tuple(sorted(kinds))))
    return tuple(known), tuple(sorted(unknown))


def _drop_local_calls(
    function: ast.FunctionDef | ast.AsyncFunctionDef, values: dict[str, set[str]]
) -> None:
    """Make unknown the values that are calls of names the function binds itself.

    ``url.geturl()`` of a local ``url``, ``self._name_of(record)``: no name of the module.
    """
    called = [kinds for kinds in values.values() if kinds - {TEXT, ANY_VALUE}]
    if not called:
        return
    bound = _bound_names(function)
    for kinds in called:
        if any(kind.partition(".")[0] in bound for kind in kinds - {TEXT, ANY_VALUE}):
            kinds.add(ANY_VALUE)


# (name, value, receiver): the value is ``None`` where the statement gives none of its own
# (``x.name += 1``, an item of an unpacked value); the receiver is the name an item or
# ``update`` write starts at ("" if it starts at none), ``None`` for the other writes.
Write = tuple[str, ast.expr | None, str | None]


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
        yield target.attr, value, None
    elif isinstance(target, ast.Subscript):
        name = _text(target.slice)
        if name is not None:  # ``x[key] = value`` is mostly a dict's: not counted
            yield name, value, _start(target.value) or ""
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
        receiver = _start(function.value) or ""
        if isinstance(display, ast.Dict):
            for key, value in zip(display.keys, display.values, strict=True):
                name = _text(key) if key is not None else None
                yield (name, value, receiver) if name is not None else (ANY_NAME, None, receiver)
    elif (
        isinstance(function, ast.Name)
        and function.id == "setattr"
        and len(call.args) == SETATTR_ARGUMENTS
    ):
        name = _text(call.args[1])
        yield (name, call.args[2], None) if name is not None else (ANY_NAME, None, None)


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
