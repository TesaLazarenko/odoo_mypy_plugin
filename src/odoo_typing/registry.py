"""Static model registry: which classes make up each Odoo model, per addon view.

Odoo builds every model at runtime as a chain of all classes that define or
``_inherit`` it, in module load order. An addon only "sees" the classes from
itself and its dependency closure, so every question here is asked from the
point of view of an addon. Code outside any addon has no such view and is never
resolved against the registry (see :mod:`odoo_typing.plugin`).

Everything is computed lazily: the manifests are read on the first question, and
an addon's files are scanned only when a view that contains the addon is queried.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from functools import cached_property
from pathlib import Path

from odoo_typing.addons import AddonIndex
from odoo_typing.config import Settings
from odoo_typing.scanner import (
    Contribution,
    ModuleScan,
    SourceError,
    parse_source,
    referenced_models,
    scan_addon,
)

__all__ = ["Registry"]

TipsKey = tuple[str, str, str | None]


class Registry:
    """Lazily scanned index of Odoo model classes."""

    def __init__(self, settings: Settings) -> None:
        """Create a registry for the configured Odoo installation.

        Nothing is read from disk yet: a fully cached mypy run never asks a question.

        Args:
            settings: Plugin settings.
        """
        self._settings = settings
        self._by_module: dict[str, ModuleScan] = {}
        self._by_path: dict[Path, ModuleScan] = {}
        self._by_class: dict[str, Contribution] = {}
        self._by_model: dict[str, dict[str, list[Contribution]]] = {}
        self._tips: dict[TipsKey, tuple[Contribution, ...]] = {}
        self._bases: dict[str, tuple[Contribution, ...]] = {}
        self._ancestors: dict[str, frozenset[str]] = {}
        self._resolving = 0  # depth of bases() computations in progress

    @cached_property
    def addons(self) -> AddonIndex:
        """Index of all addons, built (manifests read) on first use."""
        return AddonIndex(self._settings.odoo_path, self._settings.addons_paths)

    def _models_of(self, addon_name: str) -> dict[str, list[Contribution]]:
        """Scan an addon once; return its contributions by model name."""
        if addon_name not in self._by_model:
            models = self._by_model[addon_name] = {}
            addon = self.addons.addons[addon_name]
            result = scan_addon(addon, self.addons.load_order[addon_name])
            for error in result.errors:
                _warn_unparsable(error)
            for scan in result.modules:
                self._index(scan, models)
        return self._by_model[addon_name]

    def _index(self, scan: ModuleScan, models: dict[str, list[Contribution]]) -> None:
        self._by_module[scan.module] = scan
        self._by_path[scan.path] = scan
        for contribution in scan.contributions:
            self._by_class[contribution.fullname] = contribution
            models.setdefault(contribution.model, []).append(contribution)

    def module_scan(self, module: str) -> ModuleScan | None:
        """Return the scan of a Python module, if it belongs to a known addon."""
        addon = self.addons.addon_for_module(module)
        if addon is None:
            return None
        self._models_of(addon.name)
        return self._by_module.get(module)

    def contribution(self, class_fullname: str) -> Contribution | None:
        """Return the model contribution defined by a class, if any."""
        module = class_fullname.rpartition(".")[0]
        if self.module_scan(module) is None:
            return None
        return self._by_class.get(class_fullname)

    def referenced_models(self, path: str | Path) -> frozenset[str]:
        """Model names a source file refers to, also for files the addon does not import.

        A file the scanner reached is answered from its scan, whatever path spelling
        mypy uses for it (relative, or through a symlink). Other files, such as tests
        that ``__init__.py`` does not import, are parsed once here.
        """
        addon = self.addons.addon_for_path(path)
        if addon is not None:
            self._models_of(addon.name)
        resolved = self.addons.real_path(path)
        scan = self._by_path.get(resolved)
        if scan is not None:
            return scan.referenced_models
        try:
            tree = parse_source(resolved)
        except SourceError:
            return frozenset()  # mypy reports unparsable files it checks itself
        return frozenset(referenced_models(tree))

    def addon_name_for_path(self, path: str | Path) -> str | None:
        """Return the name of the addon a source file belongs to."""
        addon = self.addons.addon_for_path(path)
        return addon.name if addon else None

    def _contributions(self, model: str, view: Iterable[str]) -> list[Contribution]:
        found = [
            contribution
            for addon_name in view
            for contribution in self._models_of(addon_name).get(model, ())
        ]
        return sorted(found, key=lambda item: item.position)

    def model_tips(
        self, model: str, addon_name: str, before: Contribution | None = None
    ) -> tuple[Contribution, ...]:
        """Return the most derived classes of a model, as seen from an addon.

        Usually there is exactly one tip: the last class in the inheritance
        chain. Several tips appear when unrelated addons both extend the model.
        Answers are cached; mypy asks the same question for every ``env[...]``.

        Args:
            model: Odoo model name.
            addon_name: Point of view: only this addon and its dependency closure
                are visible.
            before: Only consider classes registered before this one.

        Returns:
            Tips ordered from the most recently loaded one.
        """
        key = (model, addon_name, before.fullname if before else None)
        cached = self._tips.get(key)
        if cached is not None:
            return cached
        tips = self._compute_tips(model, addon_name, before)
        if not self._resolving:
            # Inside bases(), an _inherit cycle guard may hide some ancestors; such
            # partial answers must not outlive the computation.
            self._tips[key] = tips
        return tips

    def _compute_tips(
        self, model: str, addon_name: str, before: Contribution | None
    ) -> tuple[Contribution, ...]:
        candidates = [
            item
            for item in self._contributions(model, self.addons.closure(addon_name))
            if before is None or item.position < before.position
        ]
        covered: set[str] = set()
        for item in candidates:
            covered |= self._ancestor_names(item)
        return tuple(item for item in reversed(candidates) if item.fullname not in covered)

    def bases(self, contribution: Contribution) -> tuple[Contribution, ...]:
        """Return the classes a contribution inherits from, through ``_inherit``."""
        key = contribution.fullname
        if key not in self._bases:
            self._bases[key] = ()  # guard against _inherit cycles
            self._resolving += 1
            try:
                self._bases[key] = self._compute_bases(contribution)
            finally:
                self._resolving -= 1
        return self._bases[key]

    def _compute_bases(self, contribution: Contribution) -> tuple[Contribution, ...]:
        result: dict[str, Contribution] = {}
        for parent in contribution.parents:
            before = contribution if parent == contribution.model else None
            for tip in self.model_tips(parent, contribution.addon, before):
                if tip.fullname != contribution.fullname:
                    result.setdefault(tip.fullname, tip)
        return tuple(result.values())

    def _ancestor_names(self, contribution: Contribution) -> frozenset[str]:
        key = contribution.fullname
        if key not in self._ancestors:
            self._ancestors[key] = frozenset()  # guard against cycles
            names: set[str] = set()
            for base in self.bases(contribution):
                names.add(base.fullname)
                names |= self._ancestor_names(base)
            self._ancestors[key] = frozenset(names)
        return self._ancestors[key]


def _warn_unparsable(error: SourceError) -> None:
    """Tell the user why the models of a file are missing (once per file and run)."""
    print(
        f"odoo-typing: warning: cannot parse {error.path}: {error.reason}; "
        "the Odoo models it defines are ignored",
        file=sys.stderr,
    )
