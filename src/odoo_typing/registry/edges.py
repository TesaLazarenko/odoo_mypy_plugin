"""Dependency edges: the modules mypy must load for the model classes a module uses.

Without them mypy may type-check a file before the classes of the models it refers to
(``_inherit``, ``env[...]``, comodels) are analyzed, or not load them at all.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from odoo_typing.addons import Addon
from odoo_typing.config import Settings
from odoo_typing.registry.annotations import AnnotatedModels
from odoo_typing.registry.scans import IndexLoader, ScanCache
from odoo_typing.scanner import Contribution, SourceError, parse_source

__all__ = ["DependencyEdges", "imported_names"]

IMPORT_LIMIT = 200  # modules followed from one file: far above any chain of test classes


def _module_file(addon: Addon, module: str) -> Path | None:
    """The file of a module of an addon that no scan lists (a test module)."""
    relative = module[len(addon.module_prefix) :].strip(".").split(".")
    base = addon.path.joinpath(*relative)
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def imported_names(tree: ast.Module, package: str) -> frozenset[str]:
    """What the import statements of a module name, as dotted names.

    ``from a.b import c`` gives ``a.b`` and ``a.b.c``: ``c`` may be a module.

    Args:
        tree: The module's syntax tree.
        package: The package its relative imports are relative to.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parts = package.split(".") if package else []
            base = parts[: len(parts) - (node.level - 1)] if node.level else []
            source = ".".join([*base, *([node.module] if node.module else [])])
            if source:
                found.add(source)
                found.update(f"{source}.{alias.name}" for alias in node.names)
    return frozenset(found)


class DependencyEdges(AnnotatedModels):
    """Which modules make up the registry classes a module relies on."""

    def __init__(
        self, settings: Settings, index: IndexLoader | None = None, scans: ScanCache | None = None
    ) -> None:
        super().__init__(settings, index, scans)
        self._view_modules: dict[tuple[str, str], frozenset[str]] = {}
        self._comodels: dict[str, frozenset[str]] = {}
        self._covered: dict[str, frozenset[str]] = {}
        self._edge_addon_memo: dict[str, frozenset[str]] = {}
        self._view_addon_memo: dict[tuple[str, str], frozenset[str]] = {}
        self._name_sets: dict[frozenset[str], frozenset[str]] = {}  # equal sets kept once
        self._imports: dict[Path, frozenset[str]] = {}  # of test modules of other addons

    def covered_models(self, module: str, path: str | Path | None = None) -> frozenset[str]:
        """Models whose whole registry class a module's dependency edges cover.

        Those named by ``env[...]`` or comodel literals in the file, by its
        ``odoo.values.*`` / ``odoo.model.*`` annotations, and the comodels of the
        relational fields that its classes' chains declare (``self.partner_id``).

        Args:
            module: Module name, as mypy sees it.
            path: Its source file (needed for files the addon scan did not reach).
        """
        if module not in self._covered:
            scan = self.module_scan(module)
            source = path if path is not None else scan.path if scan else None
            addon = self.addon_name_for_path(source) if source is not None else None
            if addon is None or source is None:
                return frozenset()
            models = set(self.referenced_models(source)) | self.annotation_models(source)
            for contribution in scan.contributions if scan else ():
                models |= self.chain_comodels(contribution)
            self._covered[module] = self._same_names(frozenset(models))
        return self._covered[module]

    def dependency_modules(self, module: str, path: str | Path) -> frozenset[str]:
        """Modules a module must be analyzed after: see class_modules and view_modules."""
        addon = self.addon_name_for_path(path)
        if addon is None:
            return frozenset()  # nothing is resolved outside addons
        modules: set[str] = set()
        scan = self.module_scan(module)
        for contribution in scan.contributions if scan else ():
            modules |= self.class_modules(contribution)
        for model in self.covered_models(module, path):
            modules |= self.view_modules(model, addon)
        return frozenset(modules)

    def inherited_view_modules(self, path: str | Path, imported: Iterable[str]) -> frozenset[str]:
        """Modules to analyze before a file that imports modules of other addons.

        A test class keeps the records its base classes create (``self.partner`` of
        another addon's common test class), and reads them as its own addon sees their
        model. The file never names the model, so nothing else makes mypy analyze its
        addon's classes of that model first. The imported modules that no addon scan
        reaches (tests) say which models: those they name, and those named by the
        modules of other addons they import in turn.

        Args:
            path: The importing file.
            imported: The modules it imports, as mypy names them.
        """
        addon = self.addon_name_for_path(path)
        if addon is None:
            return frozenset()
        modules: set[str] = set()
        seen: set[str] = set()
        pending = list(imported)
        while pending and len(seen) < IMPORT_LIMIT:
            name = pending.pop()
            found = self.addons.resolve_module(name)
            if name in seen or found is None or found[0].name == addon:
                continue
            seen.add(name)
            if self.module_scan(found[1]) is not None:
                continue  # a module the addon itself loads: not a test
            source = _module_file(found[0], found[1])
            if source is None:
                continue  # a name in a module, not a module
            for model in self.covered_models(found[1], source):
                if self.visible(model, addon):
                    modules |= self.view_modules(model, addon)
            pending.extend(self._imports_of(source, found[1]))
        return frozenset(modules)

    def _imports_of(self, path: Path, module: str) -> frozenset[str]:
        """The modules a source file imports (and what it imports from them), as dotted names."""
        if path not in self._imports:
            try:
                tree = parse_source(path)
            except SourceError:
                tree = None
            package = module if path.name == "__init__.py" else module.rpartition(".")[0]
            self._imports[path] = imported_names(tree, package) if tree is not None else frozenset()
        return self._imports[path]

    def loaded_with(
        self, model: str, addon_name: str, module: str, path: str | Path | None = None
    ) -> bool:
        """Whether mypy loads a model's whole registry class (``addon_name``'s view) with a module.

        A merged class that refers to classes mypy does not load with the module it is
        stored in could not be read back from the cache. Every module depends on its
        parent packages, whose ``__init__`` imports the rest of its addon, so loading a
        module loads its whole addon, and the modules that the addon's dependency
        edges name (with their addons). The answer is conservative: it follows one level
        of those edges.

        Args:
            model: Odoo model name.
            addon_name: The view.
            module: The module that would use the class.
            path: Its source file (needed for files the addon scan did not reach).
        """
        addon = self.addons.addon_for_module(module)
        if addon is None:
            return False
        needed = self._view_addons(model, addon_name)
        reachable = self._edge_addons(addon.name)
        if needed <= reachable:
            return True
        # A file the addon scan did not reach (tests/...): its own edges count too.
        source = path if path is not None else self._module_path(module)
        edges = self.dependency_modules(module, source) if source is not None else frozenset()
        return needed <= reachable | {self._module_addon(name) for name in edges}

    def _same_names(self, names: frozenset[str]) -> frozenset[str]:
        """An equal set of names the registry already keeps, if there is one."""
        return self._name_sets.setdefault(names, names)

    def _view_addons(self, model: str, addon_name: str) -> frozenset[str]:
        """The addons of the modules that make up a model's registry class (cached)."""
        key = (model, addon_name)
        if key not in self._view_addon_memo:
            modules = self.view_modules(model, addon_name)
            addons = frozenset(self._module_addon(name) for name in modules)
            self._view_addon_memo[key] = self._same_names(addons)
        return self._view_addon_memo[key]

    def _edge_addons(self, addon_name: str) -> frozenset[str]:
        """The addon, and the addons its modules' dependency edges point into."""
        if addon_name not in self._edge_addon_memo:
            addons = {addon_name}
            for scan in self._scans_of(addon_name):
                edges = self.dependency_modules(scan.module, scan.path)
                addons.update(self._module_addon(name) for name in edges)
            self._edge_addon_memo[addon_name] = frozenset(addons)
        return self._edge_addon_memo[addon_name]

    def view_modules(self, model: str, addon_name: str) -> frozenset[str]:
        """Modules that make up a model's registry class as an addon sees it.

        The modules of the tips of the model and of every model in its ``_inherit``
        closure: each tip's own module depends on the rest of its chain, so these are
        enough for mypy to analyze every class of the chain first.
        """
        key = (model, addon_name)
        if key not in self._view_modules:
            nodes = self._registry_mro(model, addon_name, None)
            chain = [node for node in nodes if isinstance(node, Contribution)]
            modules = frozenset(
                tip.module
                for node in nodes
                if isinstance(node, str)
                for tip in self.model_tips(node, addon_name)
            ) | self._delegate_modules(chain, model, addon_name)
            self._view_modules[key] = self._same_names(modules)
        return self._view_modules[key]

    def class_modules(self, contribution: Contribution) -> frozenset[str]:
        """Modules that a contribution's :meth:`linearization` comes from (see view_modules)."""
        modules: set[str] = set()
        for node in self._registry_mro(contribution.model, contribution.addon, contribution):
            if isinstance(node, str):
                before = contribution if node == contribution.model else None
                tips = self.model_tips(node, contribution.addon, before=before)
                modules.update(tip.module for tip in tips)
        chain = self.linearization(contribution)
        return frozenset(modules) | self._delegate_modules(
            chain, contribution.model, contribution.addon
        )

    def _delegate_modules(
        self, chain: Iterable[Contribution], model: str, addon_name: str
    ) -> frozenset[str]:
        """Modules of the tips that make up the registry classes delegated to (transitively)."""
        modules: set[str] = set()
        for delegate in self.delegate_closure(chain, model, addon_name):
            for node in self._registry_mro(delegate, addon_name, None):
                if isinstance(node, str):
                    modules.update(tip.module for tip in self.model_tips(node, addon_name))
        return frozenset(modules)

    def chain_comodels(self, contribution: Contribution) -> frozenset[str]:
        """Comodels of the relational fields that the classes of a linearization declare."""
        if contribution.fullname not in self._comodels:
            comodels = frozenset(
                comodel for item in self.linearization(contribution) for comodel in item.comodels
            )
            self._comodels[contribution.fullname] = self._same_names(comodels)
        return self._comodels[contribution.fullname]
