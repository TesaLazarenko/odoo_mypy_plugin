"""The scan of an addon: its ``__init__.py`` imports replayed in Python's execution order.

Only files reachable through the addon's ``__init__.py`` imports are scanned. Imports
run where they appear, also inside ``try``/``if`` blocks, which is the order in which
Odoo registers the classes.
"""

from __future__ import annotations

import ast
from collections import ChainMap
from dataclasses import dataclass, field
from pathlib import Path

from odoo_typing.addons import Addon, LoadKey, find_manifest
from odoo_typing.guards import model_guards
from odoo_typing.scanner.class_bodies import (
    abstract,
    as_names,
    declared_comodels,
    defined_methods,
    delegated_models,
    inherits_pairs,
    log_access,
    model_name,
)
from odoo_typing.scanner.evaluation import UNKNOWN, assign, class_attributes
from odoo_typing.scanner.fields import declared_fields
from odoo_typing.scanner.referenced import referenced_models
from odoo_typing.scanner.results import AddonScan, Contribution, ModuleScan, SourceError
from odoo_typing.scanner.syntax import inner_nodes, parse_source
from odoo_typing.xmlids import scan_data

__all__ = ["scan_addon"]


def scan_addon(addon: Addon, load_key: LoadKey) -> AddonScan:
    """Scan all Python files of an addon that its ``__init__.py`` imports, and its data.

    Args:
        addon: Addon to scan.
        load_key: Position of the addon in Odoo's load order
            (:meth:`odoo_typing.addons.AddonIndex.load_key`).

    Returns:
        The scanned files, and the files that could not be parsed.
    """
    walker = _ImportWalker(addon, load_key)
    walker.visit_module(addon.module_prefix, addon.path)
    data = scan_data(addon.name, addon.path, find_manifest(addon.path))
    return AddonScan(modules=tuple(walker.scans), errors=tuple(walker.errors), data=data)


@dataclass(slots=True)
class _File:
    """State of one module while its top-level statements are walked."""

    module: str
    package: str  # what relative imports are relative to
    names: dict[str, object] = field(default_factory=dict)  # module-level constants
    contributions: list[Contribution] = field(default_factory=list)


class _ImportWalker:
    """Replays an addon's import-time execution, statement by statement.

    Odoo registers model classes in creation order (``MetaModel.module_to_models``),
    which is Python's execution order: an import runs the imported module's classes
    at the import statement, before the importing module's later classes.
    """

    def __init__(self, addon: Addon, load_key: LoadKey) -> None:
        self.addon = addon
        self.load_key = load_key
        self.scans: list[ModuleScan] = []
        self.errors: list[SourceError] = []
        self._seen: set[Path] = set()
        self._classes: set[str] = set()
        self._names: dict[str, dict[str, object]] = {}  # module-level names of each module
        # Absolute spellings of the addon's own package: Odoo's, and the one mypy uses.
        self._roots = {f"odoo.addons.{addon.name}", addon.module_prefix}

    def visit_module(self, module: str, base: Path) -> None:
        """Execute a module or package of the addon, the first time it is imported."""
        init = base / "__init__.py"
        if init.is_file():
            self._visit(init, module, package=module)
        else:
            self._visit(base.with_suffix(".py"), module, package=module.rpartition(".")[0])

    def _visit(self, path: Path, module: str, package: str) -> None:
        if path in self._seen or not path.is_file():
            return
        self._seen.add(path)
        try:
            tree = parse_source(path)
        except SourceError as error:
            self.errors.append(error)
            return
        nodes = inner_nodes(tree)
        file = _File(module=module, package=package)
        self._names[module] = file.names
        self._walk(tree.body, file)
        self.scans.append(
            ModuleScan(
                module=module,
                path=path,
                contributions=tuple(file.contributions),
                referenced_models=frozenset(referenced_models(tree, nodes)),
                guards=model_guards(nodes),
            )
        )

    def _walk(self, body: list[ast.stmt], file: _File) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                self._add_class(node, file)
            elif isinstance(node, ast.ImportFrom):
                self._import_from(node, file)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self._import(alias.name.split("."))
            elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
                assign(node, file.names)
            else:
                for block in _executed_blocks(node):
                    self._walk(block, file)

    def _import_from(self, node: ast.ImportFrom, file: _File) -> None:
        package = file.package.split(".") if node.level else []
        dotted = package[: len(package) - node.level + 1] if node.level else []
        dotted += node.module.split(".") if node.module else []
        parts = self._inner_parts(dotted)
        if parts is None:
            return
        self._import(dotted)
        source = self._names.get(".".join([self.addon.module_prefix, *parts]), {})
        for alias in node.names:
            self._import([*dotted, alias.name])  # runs only if it is a submodule
            if alias.name in source:  # ``from .consts import STATES``: a known value
                file.names[alias.asname or alias.name] = source[alias.name]

    def _import(self, dotted: list[str]) -> None:
        """Run ``import a.b.c`` if it names this addon: each package, then the module."""
        parts = self._inner_parts(dotted)
        module, base = self.addon.module_prefix, self.addon.path
        for part in parts or ():
            module, base = f"{module}.{part}", base / part
            self.visit_module(module, base)

    def _inner_parts(self, dotted: list[str]) -> list[str] | None:
        for root in self._roots:
            root_parts = root.split(".")
            if dotted[: len(root_parts)] == root_parts:
                return dotted[len(root_parts) :]
        return None

    def _add_class(self, node: ast.ClassDef, file: _File) -> None:
        attrs = class_attributes(node, file.names)
        file.names[node.name] = UNKNOWN
        model = model_name(node.name, attrs)
        fullname = f"{file.module}.{node.name}"
        # A second definition under the same name (if/else alternatives) is not the
        # class mypy knows by that name; mypy keeps the first one.
        if model is None or not attrs.get("_register", True) or fullname in self._classes:
            return
        self._classes.add(fullname)
        methods = defined_methods(node)
        inherits = attrs.get("_inherits")
        scope = ChainMap(attrs, file.names)
        file.contributions.append(
            Contribution(
                addon=self.addon.name,
                module=file.module,
                class_name=node.name,
                model=model,
                parents=as_names(attrs.get("_inherit")),
                position=(*self.load_key, len(self._classes)),
                comodels=declared_comodels(node),
                fields=declared_fields(node, scope, frozenset(methods)),
                methods=methods,
                delegates=delegated_models(node, inherits),
                inherits=inherits_pairs(inherits),
                log_access=log_access(attrs),
                abstract=abstract(node, attrs),
            )
        )


def _executed_blocks(node: ast.stmt) -> list[list[ast.stmt]]:
    """The statement lists of a compound statement that may run at import time.

    A static scan cannot tell which branch runs, so both branches of an ``if`` and
    all clauses of a ``try`` count, in source order. ``if TYPE_CHECKING:`` bodies
    never run. Function and class bodies are not module-level code, and loops are
    left out (a class created in a loop needs a computed ``_name`` anyway).
    """
    if isinstance(node, ast.If):
        return [node.orelse] if _is_type_checking(node.test) else [node.body, node.orelse]
    if isinstance(node, ast.Try | ast.TryStar):
        handlers = [handler.body for handler in node.handlers]
        return [node.body, *handlers, node.orelse, node.finalbody]
    if isinstance(node, ast.With | ast.AsyncWith):
        return [node.body]
    if isinstance(node, ast.Match):
        return [case.body for case in node.cases]
    return []


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )
