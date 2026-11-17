"""The attributes of a field object that hold the arguments of its definition.

``journal._fields["code"].size - len(counter)`` of a field defined with ``size=5``, and
``partner._fields["credit"].groups.split(",")`` of one defined with ``groups="..."``: Odoo
declares ``size`` and ``groups`` on the field class with ``None``, the value of a field
that passes none. ``Field.__init__`` keeps the arguments it is called with, and the setup
of the field gives the object an attribute of the same name for each
(``self.__dict__.update(attrs)``), so an attribute whose argument the definitions of the
field pass as a true literal holds a value of that literal's type.

Not the arguments the setup itself rewrites: ``_get_attrs`` and ``_setup_attrs`` /
``_setup_attrs__`` of the field classes pop some (``group_operator`` is kept as
``aggregator`` since Odoo 18, ``depends`` as ``_depends``) and assign others
(``ondelete``, ``compute`` of a company-dependent field). Which ones is read from these
methods in the source of the field classes, once per file: the keys they assign, delete
or pop from a dict and the attributes they assign on ``self``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

__all__ = ["FieldSetup"]

SETUP_METHODS: Final = ("_get_attrs", "_setup_attrs")  # also ``_setup_attrs__``
NOTHING: Final[frozenset[str]] = frozenset()


class FieldSetup:
    """The attributes the setup of field classes rewrites, read once per source file."""

    def __init__(self) -> None:
        self._rewritten: dict[str, frozenset[str]] = {}

    def rewritten(self, path: str) -> frozenset[str]:
        """The names the setup methods of a file's classes rewrite (see the module)."""
        found = self._rewritten.get(path)
        if found is None:
            found = self._rewritten[path] = _read(Path(path))
        return found


def _read(path: Path) -> frozenset[str]:
    try:
        tree = ast.parse(path.read_bytes())
    except (OSError, SyntaxError, ValueError):
        return NOTHING
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith(SETUP_METHODS):
            for item in ast.walk(node):
                names.update(_written(item))
    return frozenset(names)


def _written(node: ast.AST) -> list[str]:
    """The keys and attributes of ``self`` that a node assigns, deletes or pops."""
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AugAssign | ast.AnnAssign):
        targets = [node.target]
    elif isinstance(node, ast.Delete):
        targets = node.targets
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "pop"
        and node.args
    ):
        key = _text(node.args[0])
        return [key] if key is not None else []
    else:
        return []
    found: list[str] = []
    for target in targets:
        if isinstance(target, ast.Subscript):
            key = _text(target.slice)
            if key is not None:
                found.append(key)
        elif (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        ):
            found.append(target.attr)
    return found


def _text(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None
