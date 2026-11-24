"""Parameters an Odoo function handles itself when they are empty.

Odoo annotates ``format_date(env, value: datetime | date | str, ...)`` and starts it
with ``if not value: return ''``; ``str2bool(s: str, ...)`` starts with ``if type(s) is
bool: return s``; ``Date.to_date(value) -> date | None`` (also ``from_string``) returns
``None`` only from ``if not value: return None``. Odoo calls them with the values of
fields that may be ``False``, as the code they guard expects, so the annotation is
narrower than the function. mypy drops the bodies of the modules whose errors it does
not show, so the guards are read from the source: the statements a function starts
with (after its docstring) that are ``if <test>: return <value>`` without ``else``, for
the tests ``not p``, ``p is None``, ``isinstance(p, bool)`` and ``type(p) is bool`` of a
parameter ``p``.

A parameter is handled as well when every read of it only tests its truth or runs once
such a test passed: ``get_lang(env, lang_code: str | None = None)`` reads ``lang_code``
in ``if lang_code and lang_code in langs: lang = lang_code`` alone, so ``False`` does
what ``None`` does (:func:`guarded_parameters`). Handing it on to a function of the same
file that handles it counts as such a read: ``format_date(env, value, lang_code=None)``
only calls ``get_lang(env, lang_code)``.

Each file is indexed once (one pass over its lines for ``class`` and ``def`` lines); a
function's own text is parsed only when a hook asks for it.
"""

from __future__ import annotations

import ast
import re
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from odoo_typing.scanner.native import native_falsy_guards

__all__ = ["FalsyGuards", "Guard", "guarded_parameters"]

DEFINITION: Final = re.compile(r"^(?P<indent>[ \t]*)(?:async\s+)?def\s+(?P<name>\w+)\s*\(")
CLASS: Final = re.compile(r"^class\s+(?P<name>\w+)\b")
ALIAS: Final = re.compile(r"^[ \t]+(?P<name>\w+)\s*=\s*(?P<target>\w+)\s*(?:#.*)?$")
MAX_ALIASES: Final = 4  # ``from_string = to_date``: aliases followed to a ``def``
NESTED: Final = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
EXITS: Final = (ast.Return, ast.Raise, ast.Continue, ast.Break)

# Whether a call handles the argument at a position (or of a keyword) itself when false.
Passes = Callable[[ast.Call, int | str], bool]


@dataclass(frozen=True, slots=True)
class Guard:
    """What a function does first with its empty parameters.

    Attributes:
        falsy: Parameters it returns early for when they are false (``if not p:``), and
            those it only reads where it found them true.
        booleans: Parameters it returns as they are when they are a ``bool``.
        none_only_for: Parameters whose false value is the only way the function returns
            ``None`` / ``False``: for a true argument the result is neither.
        callable_first: Whether the function takes a function in place of its ``*args``:
            it tests ``callable(args[0])`` (``api.depends``, ``api.constrains`` of Odoo 17
            and 18, which declare ``*args: str``).
    """

    falsy: frozenset[str] = frozenset()
    booleans: frozenset[str] = frozenset()
    none_only_for: frozenset[str] = frozenset()
    callable_first: bool = False


NO_GUARD: Final = Guard()


@dataclass(frozen=True, slots=True)
class _Index:
    """Where the definitions of a file are: (class or "", name) -> line index."""

    lines: tuple[str, ...]
    definitions: dict[tuple[str, str], int]
    aliases: dict[tuple[str, str], str]


class FalsyGuards:
    """The guards of the functions of source files, read once per file and function."""

    def __init__(self) -> None:
        self._indexes: dict[Path, _Index | None] = {}
        self._guards: dict[tuple[Path, str, str], Guard] = {}
        # The same reading in Rust (odoo_typing.scanner.native), where it is installed.
        self._native = native_falsy_guards()

    def of(self, path: Path, owner: str, name: str) -> Guard:
        """The guard of function ``name`` of a file, a method of top-level class ``owner``."""
        key = (path, owner, name)
        found = self._guards.get(key)
        if found is None and self._native is not None:
            falsy, booleans, none_only_for, callable_first = self._native.of(str(path), owner, name)
            found = self._guards[key] = Guard(
                frozenset(falsy), frozenset(booleans), frozenset(none_only_for), callable_first
            )
        if found is None:
            self._guards[key] = NO_GUARD  # a function that hands a parameter back to itself
            found = self._guards[key] = self._read(path, owner, name)
        return found

    def _passes(self, path: Path) -> Passes:
        """Whether a call to a top-level function of a file handles an argument itself."""

        def passes(call: ast.Call, where: int | str) -> bool:
            if not isinstance(call.func, ast.Name):
                return False
            index = self._index(path)
            start = index.definitions.get(("", call.func.id)) if index is not None else None
            if index is None or start is None:
                return False
            parameter = _parameter_at(_function_text(index.lines, start), where)
            return parameter is not None and parameter in self.of(path, "", call.func.id).falsy

        return passes

    def _read(self, path: Path, owner: str, name: str) -> Guard:
        index = self._index(path)
        if index is None:
            return NO_GUARD
        for _ in range(MAX_ALIASES):
            start = index.definitions.get((owner, name))
            if start is not None:
                return _guard(_function_text(index.lines, start), self._passes(path))
            target = index.aliases.get((owner, name))
            if target is None:
                return NO_GUARD
            name = target
        return NO_GUARD

    def _index(self, path: Path) -> _Index | None:
        if path in self._indexes:
            return self._indexes[path]
        try:
            lines = tuple(path.read_text(encoding="utf-8").splitlines())
        except (OSError, ValueError):
            self._indexes[path] = None
            return None
        definitions: dict[tuple[str, str], int] = {}
        aliases: dict[tuple[str, str], str] = {}
        owner = ""
        member_indent: str | None = None
        for number, line in enumerate(lines):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line[0].isspace():
                found_class = CLASS.match(line)
                owner = found_class.group("name") if found_class else ""
                member_indent = None
            definition = DEFINITION.match(line)
            if definition is not None:
                indent = definition.group("indent")
                if not indent:
                    definitions.setdefault(("", definition.group("name")), number)
                elif owner and (member_indent is None or indent == member_indent):
                    member_indent = indent
                    definitions.setdefault((owner, definition.group("name")), number)
                continue
            alias = ALIAS.match(line) if owner else None
            if alias is not None and (member_indent is None or line.startswith(member_indent)):
                aliases.setdefault((owner, alias.group("name")), alias.group("target"))
        found = self._indexes[path] = _Index(lines, definitions, aliases)
        return found


def _function_text(lines: tuple[str, ...], start: int) -> str:
    """The text of the function whose ``def`` is on a line (its signature may span lines)."""
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = start + 1
    while end < len(lines):
        line = lines[end]
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            current = len(line) - len(line.lstrip())
            if current <= indent and not stripped.startswith((")", "]")):
                break
        end += 1
    return textwrap.dedent("\n".join(lines[start:end]))


def _parameter_at(text: str, where: int | str) -> str | None:
    """The parameter of a function's text that a positional index or a keyword gives."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    function = tree.body[0] if tree.body else None
    if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
        return None
    arguments = function.args
    positional = [argument.arg for argument in (*arguments.posonlyargs, *arguments.args)]
    if isinstance(where, int):
        return positional[where] if where < len(positional) else None
    named = [argument.arg for argument in (*arguments.args, *arguments.kwonlyargs)]
    return where if where in named else None


def _guard(text: str, passes: Passes | None = None) -> Guard:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return NO_GUARD
    function = tree.body[0] if tree.body else None
    if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
        return NO_GUARD
    arguments = function.args
    parameters = {
        argument.arg
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
    }
    body = function.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # the docstring
    falsy: set[str] = set()
    booleans: set[str] = set()
    empty_returns: set[str] = set()
    guards: list[ast.stmt] = []
    for statement in body:
        found = _leading_guard(statement, parameters)
        if found is None:
            break
        guards.append(statement)
        parameter, kind, returned = found
        if kind == "falsy":
            falsy.add(parameter)
            if _is_empty_value(returned):
                empty_returns.add(parameter)
        else:
            booleans.add(parameter)
    other_empty = any(
        _is_empty_value(node.value)
        for statement in body
        if statement not in guards
        for node in _returns(statement)
    )
    # The function must end in a return of a value (or a raise): no ``None`` by falling off.
    last = body[-1] if body else None
    ends = isinstance(last, ast.Raise) or (
        isinstance(last, ast.Return) and not _is_empty_value(last.value)
    )
    single = len(empty_returns) == 1 and not other_empty and ends
    falsy |= guarded_parameters(function, parameters, passes)
    return Guard(
        frozenset(falsy),
        frozenset(booleans),
        frozenset(empty_returns if single else ()),
        _tests_callable_first(function),
    )


def _tests_callable_first(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a function tests ``callable(args[0])`` of its ``*args``."""
    star = function.args.vararg
    if star is None:
        return False
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "callable"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Subscript)
        and isinstance(node.args[0].value, ast.Name)
        and node.args[0].value.id == star.arg
        and isinstance(node.args[0].slice, ast.Constant)
        and node.args[0].slice.value == 0
        for node in ast.walk(function)
    )


def guarded_parameters(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parameters: set[str],
    passes: Passes | None = None,
) -> set[str]:
    """The parameters every read of which tests their truth, or runs once a test passed.

    A false value of such a parameter does what any other false value does: the
    function never reads which one it got. A read counts as a test where only its truth
    is used (``if p``, ``not p``, an operand of ``and`` / ``or`` there, ``p or default``)
    and as proven where a test of the parameter passed before it runs: the body of
    ``if p:`` / ``if p and ...:``, the later operands of ``p and ...``, the ``else`` of
    ``if not p:``, the statements after ``if not p: return`` and after ``assert p``.
    Not: a parameter the function binds again, never reads, compares (``p is None``
    tells ``None`` from ``False``), passes on or returns as it is (``return p and
    p.strip()``), or reads in a function defined inside. Passing it as it is to a call
    that ``passes`` says handles that argument itself counts as a test.
    """
    found: set[str] = set()
    for name in parameters:
        reads = _Reads(name, passes)
        reads.block(function.body, False)
        if reads.count and not reads.other:
            found.add(name)
    return found


class _Reads:
    """The reads of one parameter in a function body (see :func:`guarded_parameters`)."""

    def __init__(self, name: str, passes: Passes | None = None) -> None:
        self.name = name
        self.passes = passes
        self.count = 0
        self.other = False  # a read of the value as it is, or a binding of the name

    def block(self, statements: list[ast.stmt], proven: bool) -> None:
        for statement in statements:
            proven = self.statement(statement, proven)

    def statement(self, node: ast.stmt, proven: bool) -> bool:
        """Visit a statement; whether the parameter is true for the statements after it."""
        if isinstance(node, ast.If):
            self.expression(node.test, proven, True)
            self.block(node.body, proven or self._implies(node.test))
            self.block(node.orelse, proven or self._refutes(node.test))
            leaves = bool(node.body) and isinstance(node.body[-1], EXITS)
            return proven or (self._refutes(node.test) and not node.orelse and leaves)
        if isinstance(node, ast.While):
            self.expression(node.test, proven, True)
            self.block(node.body, proven or self._implies(node.test))
            self.block(node.orelse, proven)
            return proven
        if isinstance(node, ast.Assert):
            self.expression(node.test, proven, True)
            if node.msg is not None:
                self.expression(node.msg, proven, False)
            return proven or self._implies(node.test)
        if isinstance(node, NESTED):
            self._closed(node)
            return proven
        self._parts(node, proven)
        return proven

    def _parts(self, node: ast.AST, proven: bool) -> None:
        """The statements and expressions of another node (a ``for``, an ``except``, a ``case``)."""
        if self.name in {getattr(node, "name", None), getattr(node, "rest", None)}:
            self.other = True  # ``except E as p``, ``case {**p}``, ``case x as p``
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                self.statement(child, proven)
            elif isinstance(child, ast.expr):
                self.expression(child, proven, False)
            else:
                self._parts(child, proven)

    def expression(self, node: ast.expr, proven: bool, test: bool) -> None:
        """Visit an expression; ``test`` if only its truth is used."""
        if isinstance(node, ast.Name):
            if node.id == self.name:
                self.count += 1
                if not isinstance(node.ctx, ast.Load) or not (test or proven):
                    self.other = True
        elif isinstance(node, ast.BoolOp):
            last = len(node.values) - 1
            for index, value in enumerate(node.values):
                # ``p or default`` gives the parameter only when it is true; ``p and x``
                # gives it when it is false, so there only its truth may be used.
                kept = isinstance(node.op, ast.Or) and index < last
                self.expression(value, proven, test or kept)
                reached = self._implies if isinstance(node.op, ast.And) else self._refutes
                proven = proven or reached(value)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            self.expression(node.operand, proven, True)
        elif isinstance(node, ast.IfExp):
            self.expression(node.test, proven, True)
            self.expression(node.body, proven or self._implies(node.test), test)
            self.expression(node.orelse, proven or self._refutes(node.test), test)
        elif isinstance(node, NESTED):
            self._closed(node)
        elif isinstance(node, ast.Call) and self.passes is not None:
            self._call(node, proven)
        else:
            self._operands(node, proven)

    def _call(self, node: ast.Call, proven: bool) -> None:
        """A call: the parameter handed as it is to one that handles it is a test of it."""
        assert self.passes is not None
        self.expression(node.func, proven, False)
        positional = True
        for index, argument in enumerate(node.args):
            positional = positional and not isinstance(argument, ast.Starred)
            if positional and self._is_parameter(argument) and self.passes(node, index):
                self.count += 1
            else:
                self.expression(argument, proven, False)
        for keyword in node.keywords:
            named = keyword.arg
            if named and self._is_parameter(keyword.value) and self.passes(node, named):
                self.count += 1
            else:
                self.expression(keyword.value, proven, False)

    def _is_parameter(self, node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and node.id == self.name

    def _operands(self, node: ast.expr, proven: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self.expression(child, proven, False)
            elif isinstance(child, ast.comprehension):
                self.expression(child.target, proven, False)
                self.expression(child.iter, proven, False)
                for condition in child.ifs:
                    self.expression(condition, proven, True)
            elif isinstance(child, ast.keyword):
                self.expression(child.value, proven, False)

    def _closed(self, node: ast.AST) -> None:
        """A function or class defined inside: it may run where nothing is proven."""
        for child in ast.walk(node):
            named = child.id if isinstance(child, ast.Name) else None
            named = child.arg if isinstance(child, ast.arg) else named
            if named == self.name:
                self.count += 1
                self.other = True

    def _implies(self, test: ast.expr) -> bool:
        """Whether a test that passed proves the parameter true."""
        if isinstance(test, ast.Name):
            return test.id == self.name
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
            return any(map(self._implies, test.values))
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            return self._refutes(test.operand)
        return False

    def _refutes(self, test: ast.expr) -> bool:
        """Whether a test that failed proves the parameter true."""
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            return self._implies(test.operand)
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
            return any(map(self._refutes, test.values))
        return False


def _leading_guard(
    statement: ast.stmt, parameters: set[str]
) -> tuple[str, str, ast.expr | None] | None:
    """``if not p: return v`` (``falsy``) or ``if isinstance(p, bool): return v`` (``bool``)."""
    if (
        not isinstance(statement, ast.If)
        or statement.orelse
        or len(statement.body) != 1
        or not isinstance(statement.body[0], ast.Return)
    ):
        return None
    test = statement.test
    returned = statement.body[0].value
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        name = _parameter(test.operand, parameters)
        return (name, "falsy", returned) if name else None
    if (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Is)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
    ):
        name = _parameter(test.left, parameters)
        return (name, "falsy", returned) if name else None
    if _is_bool_test(test):
        call_or_compare = test.args[0] if isinstance(test, ast.Call) else test
        subject = (
            call_or_compare.left if isinstance(call_or_compare, ast.Compare) else call_or_compare
        )
        if isinstance(subject, ast.Call):  # ``type(p) is bool``
            subject = subject.args[0] if subject.args else subject
        name = _parameter(subject, parameters)
        return (name, "bool", returned) if name else None
    return None


def _is_bool_test(test: ast.expr) -> bool:
    """``isinstance(p, bool)`` or ``type(p) is bool``."""
    if (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Name)
        and test.func.id == "isinstance"
        and len(test.args) == 2
        and isinstance(test.args[1], ast.Name)
        and test.args[1].id == "bool"
    ):
        return True
    return (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Is)
        and isinstance(test.left, ast.Call)
        and isinstance(test.left.func, ast.Name)
        and test.left.func.id == "type"
        and len(test.left.args) == 1
        and isinstance(test.comparators[0], ast.Name)
        and test.comparators[0].id == "bool"
    )


def _parameter(node: ast.expr, parameters: set[str]) -> str | None:
    return node.id if isinstance(node, ast.Name) and node.id in parameters else None


def _is_empty_value(node: ast.expr | None) -> bool:
    """``return``, ``return None``, ``return False``."""
    return node is None or (isinstance(node, ast.Constant) and node.value in (None, False))


def _returns(statement: ast.stmt) -> list[ast.Return]:
    """The ``return`` statements of a statement, not of the functions defined in it."""
    found: list[ast.Return] = []
    pending: list[ast.AST] = [statement]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Return):
            found.append(node)
        elif not isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef
        ):
            pending.extend(ast.iter_child_nodes(node))
    return found
