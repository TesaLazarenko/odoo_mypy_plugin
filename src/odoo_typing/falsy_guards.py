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

Each file is indexed once (one pass over its lines for ``class`` and ``def`` lines); a
function's own text is parsed only when a hook asks for it.
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = ["FalsyGuards", "Guard"]

DEFINITION: Final = re.compile(r"^(?P<indent>[ \t]*)(?:async\s+)?def\s+(?P<name>\w+)\s*\(")
CLASS: Final = re.compile(r"^class\s+(?P<name>\w+)\b")
ALIAS: Final = re.compile(r"^[ \t]+(?P<name>\w+)\s*=\s*(?P<target>\w+)\s*(?:#.*)?$")
MAX_ALIASES: Final = 4  # ``from_string = to_date``: aliases followed to a ``def``
NESTED: Final = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


@dataclass(frozen=True, slots=True)
class Guard:
    """What a function does first with its empty parameters.

    Attributes:
        falsy: Parameters it returns early for when they are false (``if not p:``).
        booleans: Parameters it returns as they are when they are a ``bool``.
        none_only_for: Parameters whose false value is the only way the function returns
            ``None`` / ``False``: for a true argument the result is neither.
    """

    falsy: frozenset[str] = frozenset()
    booleans: frozenset[str] = frozenset()
    none_only_for: frozenset[str] = frozenset()


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

    def of(self, path: Path, owner: str, name: str) -> Guard:
        """The guard of function ``name`` of a file, a method of top-level class ``owner``."""
        key = (path, owner, name)
        found = self._guards.get(key)
        if found is None:
            found = self._guards[key] = self._read(path, owner, name)
        return found

    def _read(self, path: Path, owner: str, name: str) -> Guard:
        index = self._index(path)
        if index is None:
            return NO_GUARD
        for _ in range(MAX_ALIASES):
            start = index.definitions.get((owner, name))
            if start is not None:
                return _guard(_function_text(index.lines, start))
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


def _guard(text: str) -> Guard:
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
    return Guard(frozenset(falsy), frozenset(booleans), frozenset(empty_returns if single else ()))


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
