"""The literal keys a function writes into the rows of ``read()``: ``row["extra"] = ...``.

``read()`` and ``search_read()`` give dicts that code often completes before it sends them
to the web client (a controller adds ``"numOfEl"``, pops ``"arch_db"``). The rows are
``TypedDict``s of the fields read (:meth:`odoo_typing.hooks.recordsets.RecordsetHooks.read_result`),
and mypy has no ``TypedDict`` that takes other keys. So the keys the function around the
call writes into the rows are keys of the rows too: into a name bound to the call's
result, to its first row (``read(...)[0]``), the variable of a ``for`` over either, and
an item of the list (``rows[0]["k"]``).
"""

from __future__ import annotations

import ast
from typing import Final, NamedTuple

__all__ = ["WrittenKeys", "written_keys"]

# Methods of ``dict`` whose first argument is a key the call removes, and adds.
REMOVING: Final = frozenset({"pop"})
READS: Final = frozenset({"read", "search_read"})
ADDING: Final = frozenset({"setdefault"})


class WrittenKeys(NamedTuple):
    """The keys a function writes into dicts: added (or set) ones, and removed ones."""

    added: frozenset[str] = frozenset()
    removed: frozenset[str] = frozenset()


def written_keys(tree: ast.AST, line: int) -> WrittenKeys:
    """The string keys that the function around a line writes into the rows of a read there.

    Added: ``row["k"] = v``, ``row["k"] += v``, ``row.setdefault("k", v)``,
    ``row.update({"k": v})``, ``row.update(k=v)``; removed: ``del row["k"]``,
    ``row.pop("k")``. ``row`` is a name that holds the rows of the ``read`` /
    ``search_read`` call on the line (see the module); none outside a function.
    """
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.lineno <= line <= (node.end_lineno or node.lineno)
    ]
    if not functions:
        return WrittenKeys()
    innermost = max(functions, key=lambda node: node.lineno)
    rows, lists = _row_names(innermost, line)
    added: set[str] = set()
    removed: set[str] = set()
    for node in ast.walk(innermost):
        if isinstance(node, ast.Subscript) and _is_row(node.value, rows, lists):
            if isinstance(node.ctx, ast.Store):
                added.update(_strings([node.slice]))
            elif isinstance(node.ctx, ast.Del):
                removed.update(_strings([node.slice]))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and _is_row(node.func.value, rows, lists)
        ):
            added.update(_added(node))
            if node.func.attr in REMOVING:
                removed.update(_strings(node.args[:1]))
    return WrittenKeys(frozenset(added), frozenset(removed))


def _row_names(function: ast.AST, line: int) -> tuple[set[str], set[str]]:
    """The names that hold a row, and those that hold the list, of the read on a line."""

    def is_read(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in READS
            and node.lineno == line
        )

    rows: set[str] = set()
    lists: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target, value = node.targets[0], node.value
        if not isinstance(target, ast.Name):
            continue
        if is_read(value):
            lists.add(target.id)
        elif isinstance(value, ast.Subscript) and is_read(value.value):
            rows.add(target.id)  # ``row = records.read([...])[0]``
    for node in ast.walk(function):
        if isinstance(node, ast.For | ast.comprehension) and isinstance(node.target, ast.Name):
            iterated = node.iter
            if is_read(iterated) or (isinstance(iterated, ast.Name) and iterated.id in lists):
                rows.add(node.target.id)
    return rows, lists


def _is_row(node: ast.expr, rows: set[str], lists: set[str]) -> bool:
    """``row`` (a name holding a row) or ``rows[i]`` (an item of a name holding the list)."""
    if isinstance(node, ast.Name):
        return node.id in rows
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and (node.value.id in lists)
    )


def _added(call: ast.Call) -> list[str]:
    """The keys a call of a dict method adds: ``setdefault("k")``, ``update({"k": v})``."""
    assert isinstance(call.func, ast.Attribute)
    method = call.func.attr
    if method in ADDING:
        return _strings(call.args[:1])
    if method != "update":
        return []
    keys = [key for item in call.args if isinstance(item, ast.Dict) for key in item.keys if key]
    return [*_strings(keys), *(keyword.arg for keyword in call.keywords if keyword.arg)]


def _strings(nodes: list[ast.expr]) -> list[str]:
    return [
        node.value
        for node in nodes
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
