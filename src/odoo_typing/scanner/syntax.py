"""Reading Python sources: parsing a file, and the small questions about its nodes.

Sources are parsed from bytes, like Python and mypy do, so a UTF-8 BOM or a PEP 263
coding cookie is honoured.
"""

from __future__ import annotations

import ast
from pathlib import Path

from odoo_typing.scanner.results import SourceError

__all__ = [
    "RELATIONAL_FIELDS",
    "callee_name",
    "comodel_argument",
    "parse_source",
    "single_target",
    "string_literal",
]

RELATIONAL_FIELDS = frozenset({"Many2one", "One2many", "Many2many"})


def parse_source(path: Path) -> ast.Module:
    """Parse a Python file the way the interpreter reads it.

    The file is passed to the parser as bytes, so a UTF-8 BOM and coding cookies
    (``# -*- coding: latin-1 -*-``) are honoured.

    Args:
        path: Source file.

    Returns:
        The module's syntax tree.

    Raises:
        SourceError: If the file cannot be read or is not valid Python.
    """
    try:
        return ast.parse(path.read_bytes(), filename=str(path))
    except OSError as exc:
        raise SourceError(path, exc.strerror or str(exc)) from exc
    except SyntaxError as exc:
        raise SourceError(path, f"{exc.msg} (line {exc.lineno})") from exc
    except ValueError as exc:  # e.g. null bytes on older Python versions
        raise SourceError(path, str(exc)) from exc


def callee_name(node: ast.expr) -> str | None:
    """The last name of a callee or base: ``Many2one`` of ``fields.Many2one``."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def string_literal(node: ast.expr) -> str | None:
    """The value of a string literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def comodel_argument(call: ast.Call) -> str | None:
    """The comodel literal of a relational field call (first argument, or keyword)."""
    if call.args:
        return string_literal(call.args[0])
    for keyword in call.keywords:
        if keyword.arg == "comodel_name":
            return string_literal(keyword.value)
    return None


def single_target(statement: ast.stmt) -> str | None:
    """The name assigned by ``x = ...`` or ``x: T = ...`` (one plain name)."""
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target: ast.expr = statement.targets[0]
    elif isinstance(statement, ast.AnnAssign):
        target = statement.target
    else:
        return None
    return target.id if isinstance(target, ast.Name) else None
