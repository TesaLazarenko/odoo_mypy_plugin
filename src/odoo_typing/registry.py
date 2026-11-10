"""Static model registry: which classes make up each Odoo model, per addon view.

Odoo builds every model at runtime as a chain of all classes that define or
``_inherit`` it, in module load order. An addon only "sees" the classes from
itself and its dependency closure, so every question here is asked from the
point of view of an addon. Code outside any addon has no such view and is never
resolved against the registry (see :mod:`odoo_typing.plugin`).

Like ``BaseModel._build_model``, every model but ``base`` inherits ``base`` last,
and a class only extends classes registered before it. ``_inherit`` cycles, which
Odoo cannot build, are found as strongly connected components before any bases are
computed: their classes get no extra bases, so no answer depends on which class
mypy asks about first.

Everything is computed lazily: the manifests are read on the first question, and
an addon's files are scanned only when a view that contains the addon is queried.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from odoo_typing.addons import BASE, AddonIndex
from odoo_typing.config import Settings
from odoo_typing.graphs import components_from
from odoo_typing.scanner import (
    Contribution,
    ModuleScan,
    SourceError,
    parse_source,
    referenced_models,
    scan_addon,
)

__all__ = ["MissingParent", "Registry"]

TipsKey = tuple[str, str, str | None]
# `_name = "x"`, also chained (`_name = _description = "x"`); %s is the escaped name.
DEFINITION_TEMPLATE = rb"""_name\s*=\s*(?:\w+\s*=\s*)*[rRuU]?['"]%s['"]"""
SKIPPED_DIRS = frozenset({"static", "i18n", "node_modules", "__pycache__"})


@dataclass(frozen=True, slots=True)
class MissingParent:
    """An ``_inherit`` entry that Odoo would not find when building a class.

    Attributes:
        model: The model named in ``_inherit``.
        defined_later: Some class in the addon's view defines the model, but only
            after this class is registered.
        outside_addons: Addons outside the dependency closure that define the model.
        missing_depends: Dependencies in the closure that are on no addons path.
    """

    model: str
    defined_later: bool
    outside_addons: tuple[str, ...]
    missing_depends: tuple[str, ...]


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
        self._candidate_memo: dict[tuple[str, str], tuple[Contribution, ...]] = {}
        self._tips: dict[TipsKey, tuple[Contribution, ...]] = {}
        self._bases: dict[str, tuple[Contribution, ...]] = {}
        self._ancestors: dict[str, frozenset[str]] = {}
        self._explored: set[Contribution] = set()  # classified for _inherit cycles
        self._cycles: dict[Contribution, tuple[Contribution, ...]] = {}
        self._definers: dict[str, tuple[str, ...]] = {}

    @cached_property
    def addons(self) -> AddonIndex:
        """Index of all addons, built (manifests read) on first use."""
        return AddonIndex(self._settings.odoo_path, self._settings.addons_paths)

    def _models_of(self, addon_name: str) -> dict[str, list[Contribution]]:
        """Scan an addon once; return its contributions by model name."""
        if addon_name not in self._by_model:
            models = self._by_model[addon_name] = {}
            addon = self.addons.addons[addon_name]
            result = scan_addon(addon, self.addons.load_key(addon_name))
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

    def _candidates(self, model: str, addon_name: str) -> tuple[Contribution, ...]:
        """The classes of a model visible from an addon, in registration order.

        Extensions registered before any class defines the model are left out: Odoo
        refuses to load them ("Model ... does not exist in registry"), and counting
        them would make a misspelled ``_inherit`` look like a known model.
        """
        key = (model, addon_name)
        if key not in self._candidate_memo:
            found = sorted(
                (
                    contribution
                    for name in self.addons.closure(addon_name)
                    for contribution in self._models_of(name).get(model, ())
                ),
                key=lambda item: item.position,
            )
            first = next((i for i, item in enumerate(found) if _defines(item)), len(found))
            self._candidate_memo[key] = tuple(found[first:])
        return self._candidate_memo[key]

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
        if key not in self._tips:
            self._tips[key] = self._compute_tips(model, addon_name, before)
        return self._tips[key]

    def _compute_tips(
        self, model: str, addon_name: str, before: Contribution | None
    ) -> tuple[Contribution, ...]:
        candidates = [
            item
            for item in self._candidates(model, addon_name)
            if before is None or item.position < before.position
        ]
        covered: set[str] = set()
        for item in candidates:
            covered |= self._ancestor_names(item)
        return tuple(item for item in reversed(candidates) if item.fullname not in covered)

    def bases(self, contribution: Contribution) -> tuple[Contribution, ...]:
        """Return the classes a contribution inherits from, through ``_inherit``.

        Classes on an ``_inherit`` cycle get none (see :meth:`inherit_cycle`).
        """
        key = contribution.fullname
        if key not in self._bases:
            # Recursion below only follows _dependencies() edges; once the cycles
            # among them are known and cut here, it cannot come back to this class.
            cyclic = bool(self.inherit_cycle(contribution))
            self._bases[key] = () if cyclic else self._compute_bases(contribution)
        return self._bases[key]

    def _compute_bases(self, contribution: Contribution) -> tuple[Contribution, ...]:
        result: dict[str, Contribution] = {}
        for parent in _parents(contribution):
            for base in self._parent_classes(parent, contribution):
                result.setdefault(base.fullname, base)
        return tuple(result.values())

    def _parent_classes(self, parent: str, contribution: Contribution) -> tuple[Contribution, ...]:
        """The classes that stand for one ``_inherit`` entry among a class's bases.

        Usually the tips of the parent model. For ``base``, which every model
        inherits, all its classes, most recently loaded first: that is how Odoo's
        registry class for ``base`` lists them. Unrelated addons extend ``base``
        (mail, web_editor...); with tips only, two classes could linearize those
        extensions in different orders, and mypy could not build an MRO for a
        class that inherits both.
        """
        before = contribution if parent == contribution.model else None
        if parent != BASE:
            return self.model_tips(parent, contribution.addon, before)
        candidates = self._candidates(parent, contribution.addon)
        return tuple(
            item
            for item in reversed(candidates)
            if before is None or item.position < before.position
        )

    def _dependencies(self, contribution: Contribution) -> list[Contribution]:
        """Every class whose bases may be needed to compute this one's bases."""
        return [
            item
            for parent in _parents(contribution)
            for item in self._candidates(parent, contribution.addon)
            if parent != contribution.model or item.position < contribution.position
        ]

    def _ancestor_names(self, contribution: Contribution) -> frozenset[str]:
        key = contribution.fullname
        if key not in self._ancestors:
            names: set[str] = set()
            for base in self.bases(contribution):
                names.add(base.fullname)
                names |= self._ancestor_names(base)
            self._ancestors[key] = frozenset(names)
        return self._ancestors[key]

    def inherit_cycle(self, contribution: Contribution) -> tuple[Contribution, ...]:
        """Return the classes on an ``_inherit`` cycle through this one, if any.

        Odoo cannot build such models. The whole strongly connected component is
        returned (in registration order), so the answer does not depend on which
        class is asked about first.
        """
        if contribution not in self._explored:
            for component in components_from(contribution, self._dependencies, self._explored):
                if len(component) > 1:
                    members = tuple(sorted(component, key=lambda item: item.position))
                    self._cycles.update(dict.fromkeys(members, members))
        return self._cycles.get(contribution, ())

    def missing_parents(self, contribution: Contribution) -> tuple[MissingParent, ...]:
        """Return the ``_inherit`` entries Odoo would not find when building the class.

        ``_build_model`` needs every parent registered before the class itself, from
        the addons the class's addon depends on; otherwise it raises ``TypeError``.
        """
        problems = []
        for parent in dict.fromkeys(contribution.parents):
            candidates = self._candidates(parent, contribution.addon)
            if any(item.position < contribution.position for item in candidates):
                continue
            closure = self.addons.closure(contribution.addon)
            outside = () if candidates else self._defining_addons(parent)
            problems.append(
                MissingParent(
                    model=parent,
                    defined_later=bool(candidates),
                    outside_addons=tuple(name for name in outside if name not in closure),
                    missing_depends=self.addons.missing_depends(contribution.addon),
                )
            )
        return tuple(problems)

    def _defining_addons(self, model: str) -> tuple[str, ...]:
        """Addons anywhere on the addons paths that define (not extend) a model.

        Only asked on an error path. Addons whose sources do not contain
        ``_name = "<model>"`` are skipped without being parsed.
        """
        if model not in self._definers:
            pattern = re.compile(DEFINITION_TEMPLATE % re.escape(model.encode()))
            self._definers[model] = tuple(
                name
                for name in sorted(self.addons.addons, key=self.addons.load_key)
                if _mentions(self.addons.addons[name].path, pattern)
                and any(_defines(item) for item in self._models_of(name).get(model, ()))
            )
        return self._definers[model]


def _parents(contribution: Contribution) -> tuple[str, ...]:
    """``_inherit``, plus ``base``, which ``_build_model`` appends to every other model."""
    if contribution.model == BASE or BASE in contribution.parents:
        return contribution.parents
    return (*contribution.parents, BASE)


def _defines(contribution: Contribution) -> bool:
    """Whether a class defines its model rather than extending an existing one."""
    return contribution.model not in contribution.parents


def _mentions(directory: Path, pattern: re.Pattern[bytes]) -> bool:
    """Whether any Python file of an addon matches a pattern (a cheap text search)."""
    for root, subdirs, files in os.walk(directory):
        subdirs[:] = [name for name in subdirs if name not in SKIPPED_DIRS]
        for name in files:
            if name.endswith(".py") and pattern.search(Path(root, name).read_bytes()):
                return True
    return False


def _warn_unparsable(error: SourceError) -> None:
    """Tell the user why the models of a file are missing (once per file and run)."""
    print(
        f"odoo-typing: warning: cannot parse {error.path}: {error.reason}; "
        "the Odoo models it defines are ignored",
        file=sys.stderr,
    )
