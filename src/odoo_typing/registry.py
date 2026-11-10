"""Static model registry: which classes make up each Odoo model, per addon view.

Odoo builds every model at runtime as a chain of all classes that define or
``_inherit`` it, in module load order. An addon only "sees" the classes from
itself and its dependency closure, so every question here is asked from the
point of view of an addon.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from odoo_typing.addons import AddonIndex
from odoo_typing.config import Settings
from odoo_typing.scanner import Contribution, ModuleScan, referenced_models, scan_addon

__all__ = ["Registry"]


class Registry:
    """Lazily scanned index of Odoo model classes."""

    def __init__(self, settings: Settings) -> None:
        """Create a registry for the configured Odoo installation.

        Args:
            settings: Plugin settings.
        """
        self.addons = AddonIndex(settings.odoo_path, settings.addons_paths)
        self._scans: dict[str, list[ModuleScan]] = {}
        self._by_module: dict[str, ModuleScan] = {}
        self._by_class: dict[str, Contribution] = {}
        self._bases: dict[str, tuple[Contribution, ...]] = {}
        self._ancestors: dict[str, frozenset[str]] = {}

    def _scan(self, addon_name: str) -> list[ModuleScan]:
        if addon_name not in self._scans:
            addon = self.addons.addons[addon_name]
            scans = scan_addon(addon, self.addons.load_order[addon_name])
            self._scans[addon_name] = scans
            for scan in scans:
                self._by_module[scan.module] = scan
                for contribution in scan.contributions:
                    self._by_class[contribution.fullname] = contribution
        return self._scans[addon_name]

    def module_scan(self, module: str) -> ModuleScan | None:
        """Return the scan of a Python module, if it belongs to a known addon."""
        addon = self.addons.addon_for_module(module)
        if addon is None:
            return None
        self._scan(addon.name)
        return self._by_module.get(module)

    def contribution(self, class_fullname: str) -> Contribution | None:
        """Return the model contribution defined by a class, if any."""
        module = class_fullname.rpartition(".")[0]
        if self.module_scan(module) is None:
            return None
        return self._by_class.get(class_fullname)

    def referenced_models(self, path: str | Path) -> frozenset[str]:
        """Model names a source file refers to, also for files the addon does not import.

        Test files, for instance, are not reachable from ``__init__.py``.
        """
        scan = next((item for item in self._by_module.values() if item.path == Path(path)), None)
        if scan is not None:
            return scan.referenced_models
        try:
            tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            return frozenset()
        return frozenset(referenced_models(tree))

    def addon_name_for_path(self, path: str | Path) -> str | None:
        """Return the name of the addon a source file belongs to."""
        addon = self.addons.addon_for_path(path)
        return addon.name if addon else None

    def _contributions(self, model: str, view: Iterable[str]) -> list[Contribution]:
        found = [
            contribution
            for addon_name in view
            for scan in self._scan(addon_name)
            for contribution in scan.contributions
            if contribution.model == model
        ]
        return sorted(found, key=lambda item: item.position)

    def model_tips(
        self, model: str, addon_name: str | None, before: Contribution | None = None
    ) -> tuple[Contribution, ...]:
        """Return the most derived classes of a model, as seen from an addon.

        Usually there is exactly one tip: the last class in the inheritance
        chain. Several tips appear when unrelated addons both extend the model.

        Args:
            model: Odoo model name.
            addon_name: Point of view; ``None`` means all addons.
            before: Only consider classes registered before this one.

        Returns:
            Tips ordered from the most recently loaded one.
        """
        view = self.addons.closure(addon_name) if addon_name else self.addons.addons.keys()
        candidates = [
            item
            for item in self._contributions(model, view)
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
            result: dict[str, Contribution] = {}
            for parent in contribution.parents:
                before = contribution if parent == contribution.model else None
                for tip in self.model_tips(parent, contribution.addon, before):
                    if tip.fullname != key:
                        result.setdefault(tip.fullname, tip)
            self._bases[key] = tuple(result.values())
        return self._bases[key]

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
