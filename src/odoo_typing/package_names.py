"""Names a module binds on a package that has no ``__init__.py``.

Since Odoo 19 ``odoo`` is a namespace package: ``from odoo import Command, _`` works only
because ``odoo/init.py`` (imported with the ORM) ends with ``import odoo`` and
``odoo.Command = Command``. mypy does not follow an assignment to a module, so each such
import in every addon was ``Module "odoo" has no attribute "Command"``, and the name was
``Any``.

:func:`bound_on_package` reads which names the package's own top-level modules bind on it
that way, and what each is imported from; :func:`import_into` adds the same imports to the
package's (empty) module, as if it had an ``__init__.py`` with them. Nothing is said about
which file does it or which names they are.

mypy parses a module again without asking the plugin about it, when another module of its
import cycle is out of date (a changed option is enough): the package's new tree is empty
again, and what mypy then stores for it has no names. :func:`bind_in` gives a package
module the names it lacks, from the modules they come from, whenever the plugin is asked
anything while that tree is the current one.
"""

from __future__ import annotations

import ast
from pathlib import Path

from mypy.nodes import GDEF, ImportFrom, MypyFile, PlaceholderNode, SymbolTableNode

from odoo_typing.scanner import SourceError, parse_source

__all__ = ["Bound", "bind_in", "bound_on_package", "import_into"]

Bound = dict[str, tuple[str, str]]
"""Name on the package -> the module it is imported from and its name there."""


def bound_on_package(directory: Path, package: str) -> Bound:
    """The names the top-level modules of a package directory bind on the package.

    Args:
        directory: The package's directory.
        package: Its dotted name (``odoo``).

    Returns:
        For each ``<package>.NAME = name`` of a module that imports the package, where
        ``name`` is imported there with ``from module import name``: ``NAME`` and that
        import.
    """
    found: Bound = {}
    for path in sorted(directory.glob("*.py")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if f"import {package}" not in text or f"\n{package}." not in text:
            continue  # the usual case, without parsing the file
        try:
            tree = parse_source(path)
        except SourceError:
            continue
        found.update(_bound_in(tree, package))
    return found


def _bound_in(tree: ast.Module, package: str) -> Bound:
    """The ``<package>.NAME = name`` statements of a module of the package."""
    # The names the module has the package under, and what it imports from modules.
    aliases = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.Import)
        for alias in statement.names
        if alias.name == package
    }
    imported = _imported(tree, package)
    found: Bound = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target, value = statement.targets[0], statement.value
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in aliases
            and isinstance(value, ast.Name)
            and value.id in imported
        ):
            found[target.attr] = imported[value.id]
    return found


def _imported(tree: ast.Module, package: str) -> Bound:
    """The names a module's ``from ... import`` statements bind, with their modules."""
    found: Bound = {}
    for statement in tree.body:
        module = _absolute(statement, package) if isinstance(statement, ast.ImportFrom) else None
        if isinstance(statement, ast.ImportFrom) and module is not None:
            for alias in statement.names:
                found[alias.asname or alias.name] = (module, alias.name)
    return found


def _absolute(statement: ast.ImportFrom, package: str) -> str | None:
    """The module a ``from ... import`` of a top-level module of the package names."""
    if statement.level == 0:
        return statement.module
    if statement.level == 1:
        return f"{package}.{statement.module}" if statement.module else package
    return None  # above the package


def import_into(module: MypyFile, bound: Bound) -> list[str]:
    """Add ``from <module> import <name> as <NAME>`` to a module for each bound name.

    Returns:
        The modules the added imports name, which mypy must load with the module.
    """
    present = {
        (statement.id, name)
        for statement in module.defs
        if isinstance(statement, ImportFrom)
        for _, name in statement.names
    }
    for name, (source, original) in sorted(bound.items()):
        if (source, name) in present or source == module.fullname:
            continue  # the tree of a daemon update, already filled; a submodule of its own
        statement = ImportFrom(source, 0, [(original, name)])
        statement.line = 1
        statement.column = 0
        statement.is_top_level = True
        module.defs.append(statement)
        module.imports.append(statement)
    return sorted({source for source, _ in bound.values()} - {module.fullname})


def bind_in(module: MypyFile, bound: Bound, modules: dict[str, MypyFile]) -> bool:
    """Give a package module the bound names it lacks, as the imports would.

    Returns:
        Whether every name is bound now; ``False`` while a module a name comes from is
        not analyzed yet (ask again later).
    """
    complete = True
    for name, (source, original) in bound.items():
        if name in module.names or source == module.fullname:
            continue
        origin = modules.get(source)
        symbol = origin.names.get(original) if origin is not None else None
        if symbol is None or symbol.node is None or isinstance(symbol.node, PlaceholderNode):
            complete = False
            continue
        module.names[name] = SymbolTableNode(GDEF, symbol.node, module_public=True)
    return complete
