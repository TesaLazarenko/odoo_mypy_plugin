"""The literal keys a function writes into dicts: ``rows[0]["extra"] = ...``.

``read()`` and ``search_read()`` give dicts that code often completes before it sends them
to the web client (a controller adds ``"numOfEl"``, pops ``"arch_db"``). The rows are
``TypedDict``s of the fields read (:meth:`odoo_typing.hooks.recordsets.RecordsetHooks.read_result`),
and mypy has no ``TypedDict`` that takes other keys. So the keys the function around the
call writes are keys of the rows too: by syntax, whatever dict they are written into.
"""

from __future__ import annotations

import ast
from typing import Final, NamedTuple

__all__ = ["WrittenKeys", "written_keys"]

# Methods of ``dict`` whose first argument is a key the call removes, and adds.
REMOVING: Final = frozenset({"pop"})
ADDING: Final = frozenset({"setdefault"})


class WrittenKeys(NamedTuple):
    """The keys a function writes into dicts: added (or set) ones, and removed ones."""

    added: frozenset[str] = frozenset()
    removed: frozenset[str] = frozenset()


def written_keys(tree: ast.AST, line: int) -> WrittenKeys:
    """The string keys that the function around a line writes, deletes or pops.

    Added: ``d["k"] = v``, ``d["k"] += v``, ``d.setdefault("k", v)``, ``d.update({"k": v})``,
    ``d.update(k=v)``; removed: ``del d["k"]``, ``d.pop("k")``. In the innermost function
    that holds the line (none outside a function).
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
    added: set[str] = set()
    removed: set[str] = set()
    for node in ast.walk(innermost):
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            added.update(_strings([node.slice]))
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Del):
            removed.update(_strings([node.slice]))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            added.update(_added(node))
            if node.func.attr in REMOVING:
                removed.update(_strings(node.args[:1]))
    return WrittenKeys(frozenset(added), frozenset(removed))


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
