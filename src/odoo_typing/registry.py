"""Static model registry: which classes make up each Odoo model, per addon view.

Odoo builds every model at runtime as a chain of all classes that define or
``_inherit`` it, in module load order. An addon only "sees" the classes from
itself and its dependency closure, so every question here is asked from the
point of view of an addon. Code outside any addon has no such view and is never
resolved against the registry (see :mod:`odoo_typing.plugin`).

Each model's MRO is computed the way Odoo builds its registry class: the
``LastOrderedSet`` of ``BaseModel._build_model`` (the model's classes, then the
registry classes of its ``_inherit`` parents, ``base`` last), linearized with C3. A
class sees the earlier classes of its own model, and every class of its parents in
its addon's view, also later ones: Odoo extends a parent's registry class in place,
so a field that ``sms`` adds to ``mail.thread`` is on ``project.task`` for every addon
that depends on both. ``_inherit`` cycles, which Odoo cannot build, are found as
strongly connected components of the model graph first; the edges inside them are
ignored, so no answer depends on which class mypy asks about first.

Everything is computed lazily: the manifests are read on the first question, and
an addon's files are scanned only when a view that contains the addon is queried.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property, partial
from pathlib import Path

from odoo_typing.addons import BASE, Addon, AddonIndex
from odoo_typing.config import Settings
from odoo_typing.graphs import c3_merge, components_from
from odoo_typing.model_fields import (
    chain_definitions,
    delegated_field,
    delegation_fields,
    merge_definitions,
    service_definitions,
)
from odoo_typing.scanner import (
    AddonScan,
    Contribution,
    FieldInfo,
    ModuleScan,
    SourceError,
    parse_source,
    referenced_models,
    scan_addon,
)
from odoo_typing.stamps import Stamps, read_index, scan_stamps, unchanged

__all__ = ["MissingParent", "ModelView", "Registry"]

TipsKey = tuple[str, str, str | None]
IndexLoader = Callable[[], tuple[AddonIndex, Stamps]]
# A registry MRO: classes, and model names standing for a parent's registry class.
Nodes = tuple["Contribution | str", ...]
# `_name = "x"`, also chained (`_name = _description = "x"`); group 1 is the name. It
# also matches `partner_name = "x"`: candidates are checked by a scan, and starting with
# a literal keeps the search fast (a look-behind would make it 20 times slower).
DEFINITION_PATTERN = re.compile(rb"""_name\s*=\s*(?:\w+\s*=\s*)*[rRuU]?['"]([^'"\\\n]+)['"]""")
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


@dataclass(frozen=True, slots=True)
class ModelView:
    """The registry class of a model, as an addon sees it (``env[model]`` there).

    Attributes:
        model: Odoo model name.
        addon: The addon whose view this is.
        chain: Its classes in MRO order, most derived first.
        tip: The one class whose own MRO has exactly these classes, if any. Otherwise
            (unrelated extensions, or parents extended outside the tip's view) a merged
            class is needed.
        bases: Direct bases for such a merged class: the tips of the model, then of
            the registry classes it inherits.
    """

    model: str
    addon: str
    chain: tuple[Contribution, ...]
    tip: Contribution | None
    bases: tuple[Contribution, ...]


class Registry:
    """Lazily scanned index of Odoo model classes."""

    def __init__(self, settings: Settings, index: IndexLoader | None = None) -> None:
        """Create a registry for the configured Odoo installation.

        Nothing is read from disk yet: a fully cached mypy run never asks a question.

        Args:
            settings: Plugin settings.
            index: Returns the addon index and the stamps of what it was built from
                (default: :func:`odoo_typing.stamps.read_index`).
        """
        self._settings = settings
        self._index_loader = index or partial(
            read_index, settings.odoo_path, settings.addons_paths, settings.custom_prefix
        )
        self._index_stamps: Stamps = {}
        self._scan_results: dict[str, tuple[AddonScan, Stamps]] = {}
        self._by_module: dict[str, ModuleScan] = {}
        self._by_path: dict[Path, ModuleScan] = {}
        self._by_class: dict[str, Contribution] = {}
        self._by_model: dict[str, dict[str, list[Contribution]]] = {}
        self._candidate_memo: dict[tuple[str, str], tuple[Contribution, ...]] = {}
        self._tips: dict[TipsKey, tuple[Contribution, ...]] = {}
        self._mros: dict[TipsKey, Nodes] = {}
        self._merged: dict[tuple[str, tuple[str, ...]], Nodes] = {}
        self._conflicts: set[tuple[str, tuple[str, ...]]] = set()
        self._views: dict[tuple[str, str], ModelView | None] = {}
        self._view_modules: dict[tuple[str, str], frozenset[str]] = {}
        self._comodels: dict[str, frozenset[str]] = {}
        self._covered: dict[str, frozenset[str]] = {}
        self._edge_addon_memo: dict[str, frozenset[str]] = {}
        self._explored: dict[str, set[str]] = {}  # per addon: models classified for cycles
        self._cycles: dict[str, dict[str, frozenset[str]]] = {}
        self._definers: dict[str, tuple[str, ...]] = {}
        self._definition_index: dict[str, list[str]] | None = None
        self._misnamed: set[str] = set()  # addons warned about by misnamed()
        self._fields: dict[tuple[str, str], Mapping[str, FieldInfo]] = {}
        self._methods: dict[tuple[str, str], frozenset[str]] = {}

    @cached_property
    def addons(self) -> AddonIndex:
        """Index of all addons, built (manifests read) on first use."""
        index, self._index_stamps = self._index_loader()
        return index

    def index_stamps(self) -> Stamps:
        """The stamps of the files the addon index describes (roots, entries, manifests)."""
        self.addons  # noqa: B018 - built on first use
        return self._index_stamps

    def scan_result(self, addon_name: str) -> tuple[AddonScan, Stamps]:
        """Scan an addon (once); return the scan and the stamps of the files it read."""
        self._models_of(addon_name)
        return self._scan_results[addon_name]

    def inputs_changed(self) -> bool:
        """Whether a file the registry has read so far changed on disk since.

        A long-lived plugin (the mypy daemon) must then start over with a new registry:
        any answer may depend on the changed file.
        """
        if "addons" not in self.__dict__:
            return False  # nothing read yet
        if not unchanged(self._index_stamps):
            return True
        return not all(unchanged(stamps) for _, stamps in self._scan_results.values())

    def _models_of(self, addon_name: str) -> dict[str, list[Contribution]]:
        """Scan an addon once; return its contributions by model name."""
        if addon_name not in self._by_model:
            models = self._by_model[addon_name] = {}
            addon = self.addons.addons[addon_name]
            result = scan_addon(addon, self.addons.load_key(addon_name))
            self._scan_results[addon_name] = (result, scan_stamps(addon, result))
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
        """Return the scan of a Python module, if it belongs to a known addon.

        Either spelling of an ``addons_path`` addon's package is accepted (see
        :meth:`AddonIndex.resolve_module`); the scan is named in the configured one.
        """
        found = self.addons.resolve_module(module)
        if found is None:
            return None
        self._models_of(found[0].name)
        return self._by_module.get(found[1])

    def contribution(self, class_fullname: str) -> Contribution | None:
        """Return the model contribution defined by a class, if any (either spelling)."""
        found = self.addons.resolve_module(class_fullname)
        if found is None:
            return None
        self._models_of(found[0].name)
        return self._by_class.get(found[1])

    def misnamed(self, module: str) -> bool:
        """Whether mypy names an addon's module in the spelling this configuration does not use.

        That happens when mypy is given an addon's real directory although the overlay
        root names it ``odoo.addons.<name>`` (or the reverse): the file is then likely
        analyzed under both names. Warns once per addon.
        """
        found = self.addons.resolve_module(module)
        if found is None or found[1] == module:
            return False
        addon = found[0]
        if addon.name not in self._misnamed:
            self._misnamed.add(addon.name)
            _warn_misnamed(module, addon)
        return True

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

        A class covers the classes of its model that its own addon sees before it (they
        are part of its MRO). Usually one class is left: the last of the chain. Several
        tips appear when unrelated addons both extend the model. Answers are cached.

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
            live = _since_last_definition(
                [
                    item
                    for item in self._candidates(model, addon_name)
                    if before is None or item.position < before.position
                ]
            )
            covered = set().union(*(self._predecessors(item) for item in live))
            self._tips[key] = tuple(item for item in reversed(live) if item not in covered)
        return self._tips[key]

    def _predecessors(self, contribution: Contribution) -> frozenset[Contribution]:
        """The earlier classes of a contribution's model that its addon sees."""
        if _defines(contribution):
            return frozenset()  # a (re)definition starts a new registry class
        candidates = self._candidates(contribution.model, contribution.addon)
        earlier = [item for item in candidates if item.position < contribution.position]
        return frozenset(_since_last_definition(earlier))

    def definition(self, model: str, addon_name: str) -> Contribution | None:
        """The class that defines a model in an addon's view (the last one, if redefined).

        Relational fields are declared with this class as comodel: it is the same for
        every addon that sees the model.
        """
        live = _since_last_definition(list(self._candidates(model, addon_name)))
        return live[0] if live else None

    # -- dependency edges ------------------------------------------------------------------

    def covered_models(self, module: str, path: str | Path | None = None) -> frozenset[str]:
        """Models whose whole registry class a module's dependency edges cover.

        Those named by ``env[...]`` or comodel literals in the file, and the comodels of
        the relational fields that its classes' chains declare (``self.partner_id``).

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
            models = set(self.referenced_models(source))
            for contribution in scan.contributions if scan else ():
                models |= self.chain_comodels(contribution)
            self._covered[module] = frozenset(models)
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
        needed = {self._module_addon(name) for name in self.view_modules(model, addon_name)}
        reachable = self._edge_addons(addon.name)
        if needed <= reachable:
            return True
        # A file the addon scan did not reach (tests/...): its own edges count too.
        source = path if path is not None else self._module_path(module)
        edges = self.dependency_modules(module, source) if source is not None else frozenset()
        return needed <= reachable | {self._module_addon(name) for name in edges}

    def _edge_addons(self, addon_name: str) -> frozenset[str]:
        """The addon, and the addons its modules' dependency edges point into."""
        if addon_name not in self._edge_addon_memo:
            addons = {addon_name}
            for scan in self._scans_of(addon_name):
                edges = self.dependency_modules(scan.module, scan.path)
                addons.update(self._module_addon(name) for name in edges)
            self._edge_addon_memo[addon_name] = frozenset(addons)
        return self._edge_addon_memo[addon_name]

    def _scans_of(self, addon_name: str) -> list[ModuleScan]:
        self._models_of(addon_name)
        prefix = self.addons.addons[addon_name].module_prefix
        return [
            scan
            for name, scan in self._by_module.items()
            if name == prefix or name.startswith(prefix + ".")
        ]

    def _module_addon(self, module: str) -> str:
        addon = self.addons.addon_for_module(module)
        return addon.name if addon else ""

    def _module_path(self, module: str) -> Path | None:
        scan = self.module_scan(module)
        return scan.path if scan else None

    # -- linearization, as BaseModel._build_model and Python's C3 build it ------------------

    def linearization(self, contribution: Contribution) -> tuple[Contribution, ...]:
        """The classes of a contribution's MRO, itself first, as its addon sees them.

        This is the MRO of Odoo's registry class for the model right after the class is
        registered: the earlier classes of the model, then the registry classes of its
        ``_inherit`` parents, ``base`` last. A parent's registry class holds *all* its
        classes in the addon's view, also later ones: Odoo extends it in place.
        """
        nodes = self._registry_mro(contribution.model, contribution.addon, contribution)
        return tuple(node for node in nodes if isinstance(node, Contribution))

    def view_class(self, model: str, addon_name: str) -> ModelView | None:
        """Describe the registry class of a model as an addon sees it.

        Returns:
            ``None`` if no class in the addon's view defines the model.
        """
        key = (model, addon_name)
        if key not in self._views:
            # An _inherits cycle comes back here (through _same_delegates): no view yet.
            self._views[key] = None
            self._views[key] = self._compute_view(model, addon_name)
        return self._views[key]

    def _compute_view(self, model: str, addon_name: str) -> ModelView | None:
        if not self._candidates(model, addon_name):
            return None
        nodes = self._registry_mro(model, addon_name, None)
        chain = tuple(node for node in nodes if isinstance(node, Contribution))
        tips = self.model_tips(model, addon_name)
        exact = (
            len(tips) == 1
            and set(self.linearization(tips[0])) == set(chain)
            and self._same_delegates(chain, model, (tips[0].addon, addon_name))
        )
        return ModelView(
            model=model,
            addon=addon_name,
            chain=chain,
            tip=tips[0] if exact else None,
            bases=self._direct_bases(model, addon_name, None),
        )

    def bases(self, contribution: Contribution) -> tuple[Contribution, ...]:
        """Return the direct bases mypy gets for a contribution.

        These are the tips of its model's earlier classes, then of each registry class
        among its bases (its ``_inherit`` parents and ``base``). Every class of
        :meth:`linearization` is one of them or in their MROs.
        """
        return self._direct_bases(contribution.model, contribution.addon, contribution)

    def _direct_bases(
        self, model: str, addon_name: str, through: Contribution | None
    ) -> tuple[Contribution, ...]:
        nodes = self._registry_bases(model, addon_name, through)
        result: dict[Contribution, None] = {}
        if any(isinstance(node, Contribution) and node is not through for node in nodes):
            result.update(dict.fromkeys(self.model_tips(model, addon_name, before=through)))
        for node in nodes:
            if isinstance(node, str):
                result.update(dict.fromkeys(self.model_tips(node, addon_name)))
        return tuple(result)

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
            self._view_modules[key] = frozenset(
                tip.module
                for node in nodes
                if isinstance(node, str)
                for tip in self.model_tips(node, addon_name)
            ) | self._delegate_modules(chain, model, addon_name)
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

    def log_access(self, contribution: Contribution) -> bool:
        """Whether Odoo adds ``create_uid``/``create_date``/... to the model's class.

        The most derived class of the same model that sets ``_log_access`` (or
        ``_auto``) decides; ``_inherit`` parents are not followed, so a mixin itself
        (an ``AbstractModel``, whose ``_auto`` is ``False``) gets the fields that every
        model using it has. Default: on. Never for ``base``: its class is in the MRO of
        every model, which would give the fields to models without them too.
        """
        return _chain_log_access(self.linearization(contribution), contribution.model)

    # -- the field index: env[model]._fields, statically -------------------------------------

    def fields(self, model: str, addon_name: str) -> Mapping[str, FieldInfo]:
        """The fields of a model's registry class as an addon sees it (``_fields``).

        Merged from the definitions of the classes of :meth:`view_class`, then the
        fields delegated through ``_inherits`` (transitively) and the service fields
        (see :mod:`odoo_typing.model_fields`). Fields of Odoo's own Python classes
        (``BaseModel``) are not in it, except for the service fields.

        Returns:
            Field name to field; empty if no class in the addon's view defines the model.
        """
        key = (model, addon_name)
        if key not in self._fields:
            self._fields[key] = {}  # a model in a delegation cycle sees no fields there
            self._fields[key] = self._compute_fields(model, addon_name)
        return self._fields[key]

    def concrete(self, model: str, addon_name: str) -> bool:
        """Whether the class that defines a model (as the addon sees it) is not abstract.

        ``False`` for mixins and ``base``, whose methods run on records of models with
        more fields, and when the scan cannot tell (a custom base class).
        """
        definition = self.definition(model, addon_name)
        return definition is not None and definition.abstract is False

    def methods(self, model: str, addon_name: str) -> frozenset[str]:
        """Names of the methods the classes of a model's view define (not BaseModel's)."""
        key = (model, addon_name)
        if key not in self._methods:
            view = self.view_class(model, addon_name)
            chain = view.chain if view is not None else ()
            self._methods[key] = frozenset(name for item in chain for name in item.methods)
        return self._methods[key]

    def _compute_fields(self, model: str, addon_name: str) -> Mapping[str, FieldInfo]:
        view = self.view_class(model, addon_name)
        if view is None:
            return {}
        definitions = {
            info.name: [info] for info in service_definitions(_chain_log_access(view.chain, model))
        }
        for name, found in chain_definitions(view.chain).items():
            definitions.setdefault(name, []).extend(found)
        fields = {name: merge_definitions(found) for name, found in definitions.items()}
        inherited: dict[str, FieldInfo] = {}
        for delegate, field_name in delegation_fields(view.chain, fields).items():
            if delegate != model:
                for name, info in self.fields(delegate, addon_name).items():
                    inherited[name] = delegated_field(info, field_name)
        for name, info in inherited.items():
            fields.setdefault(name, info)
        return fields

    # -- _inherits delegation ----------------------------------------------------------------

    def delegated_models(self, chain: Iterable[Contribution], model: str) -> tuple[str, ...]:
        """Models whose fields a registry class delegates to (merged ``_inherits``).

        ``_build_model`` merges the ``_inherits`` of every class of the registry class
        (also of its ``_inherit`` parents); ``Many2one(delegate=True)`` adds an entry.

        Args:
            chain: The classes of the registry class (a linearization or a view chain).
            model: The model itself, never its own delegate.
        """
        found = dict.fromkeys(name for item in chain for name in item.delegates)
        found.pop(model, None)
        return tuple(found)

    def delegate_closure(
        self, chain: Iterable[Contribution], model: str, addon_name: str
    ) -> tuple[str, ...]:
        """Every model a registry class delegates to, also through its delegates.

        Odoo's ``_add_inherited_fields`` copies a parent's ``_fields``, which already
        hold the fields that parent delegates, so the order is depth-first: a direct
        delegate, then what it delegates, then the next direct delegate. A cycle stops
        at a model already met (the model itself included).
        """
        found: dict[str, None] = {}

        def visit(delegates: Iterable[str]) -> None:
            for delegate in delegates:
                if delegate == model or delegate in found:
                    continue
                found[delegate] = None
                view = self.view_class(delegate, addon_name)
                if view is not None:
                    visit(self.delegated_models(view.chain, delegate))

        visit(self.delegated_models(chain, model))
        return tuple(found)

    def delegate_chain(self, model: str, addon_name: str) -> tuple[Contribution, ...]:
        """The classes of a delegated model's registry class in an addon's view."""
        view = self.view_class(model, addon_name)
        return view.chain if view is not None else ()

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

    def _same_delegates(
        self, chain: Iterable[Contribution], model: str, addons: tuple[str, str]
    ) -> bool:
        """Whether the registry classes a model delegates to look the same from two addons."""
        first, second = addons
        chain = tuple(chain)
        delegates = self.delegate_closure(chain, model, first)
        if delegates != self.delegate_closure(chain, model, second):
            return False
        return all(
            set(self.delegate_chain(delegate, first)) == set(self.delegate_chain(delegate, second))
            for delegate in delegates
        )

    def chain_comodels(self, contribution: Contribution) -> frozenset[str]:
        """Comodels of the relational fields that the classes of a linearization declare."""
        if contribution.fullname not in self._comodels:
            self._comodels[contribution.fullname] = frozenset(
                comodel for item in self.linearization(contribution) for comodel in item.comodels
            )
        return self._comodels[contribution.fullname]

    def _registry_mro(self, model: str, addon_name: str, through: Contribution | None) -> Nodes:
        """MRO of a registry class: a model name stands for a parent's registry class."""
        key = (model, addon_name, through.fullname if through else None)
        if key not in self._mros:
            bases = self._registry_bases(model, addon_name, through)
            # The model's own classes are leaves (only Model above them) and come first;
            # the rest is the C3 merge of the parents' registry classes.
            leaves = tuple(node for node in bases if isinstance(node, Contribution))
            parents = tuple(node for node in bases if isinstance(node, str))
            self._mros[key] = (model, *leaves, *self._merge_parents(addon_name, parents))
        return self._mros[key]

    def _merge_parents(self, addon_name: str, parents: tuple[str, ...]) -> Nodes:
        key = (addon_name, parents)
        if key not in self._merged:
            mros = [self._registry_mro(parent, addon_name, None) for parent in parents]
            merged, conflict = c3_merge([*mros, parents])
            self._merged[key] = tuple(merged)
            if conflict:  # Odoo's own C3 fails here too
                self._conflicts.add(key)
        return self._merged[key]

    def mro_conflict(self, contribution: Contribution) -> bool:
        """Whether the registry classes of a contribution's parents have no C3 order.

        Python (and so Odoo) refuses such a class; :meth:`linearization` still lists
        every class, taking the first parent's order where they disagree.
        """
        bases = self._registry_bases(contribution.model, contribution.addon, contribution)
        self._registry_mro(contribution.model, contribution.addon, contribution)
        parents = tuple(node for node in bases if isinstance(node, str))
        return (contribution.addon, parents) in self._conflicts

    def _registry_bases(self, model: str, addon_name: str, through: Contribution | None) -> Nodes:
        """``__base_classes`` of a model's registry class, built like ``_build_model``.

        Each class of the model (up to ``through``) is put first, followed by the
        previous bases where its ``_inherit`` names the model itself, and by its other
        parents' registry classes; re-adding an element moves it last
        (``LastOrderedSet``). Parents on an ``_inherit`` cycle and parents that no class
        in the view defines are left out.
        """
        cyclic = self._cyclic_models(model, addon_name)
        classes = [
            item
            for item in self._candidates(model, addon_name)
            if through is None or item.position <= through.position
        ]
        if through is not None and through not in classes:
            classes.append(through)  # an extension Odoo would reject: see missing_parents
        bases: Nodes = ()
        for item in classes:
            new: dict[Contribution | str, None] = {item: None}
            for parent in _parents(item):
                if parent == model:
                    added = bases  # the registry class's previous bases
                elif parent in cyclic or not self._candidates(parent, addon_name):
                    added = ()
                else:
                    added = (parent,)
                _add_last(new, added)
            bases = tuple(new)
        return bases

    # -- _inherit cycles ---------------------------------------------------------------------

    def _cyclic_models(self, model: str, addon_name: str) -> frozenset[str]:
        """The models on an ``_inherit`` cycle through this one, in an addon's view.

        Odoo cannot build such models (a class would be its own base). They are found
        as strongly connected components of the model graph before any MRO is computed,
        and the edges inside a component are ignored, so no answer depends on which
        class is asked about first.
        """
        explored = self._explored.setdefault(addon_name, set())
        cycles = self._cycles.setdefault(addon_name, {})
        if model not in explored:
            successors = partial(self._model_parents, addon_name=addon_name)
            for component in components_from(model, successors, explored):
                if len(component) > 1:
                    cycles.update(dict.fromkeys(component, frozenset(component)))
        return cycles.get(model, frozenset())

    def _model_parents(self, model: str, addon_name: str) -> list[str]:
        """Models whose registry classes a model's registry class inherits, in a view."""
        parents = (
            parent
            for item in self._candidates(model, addon_name)
            for parent in _parents(item)
            if parent != model
        )
        return [parent for parent in dict.fromkeys(parents) if self._candidates(parent, addon_name)]

    def inherit_cycle(self, contribution: Contribution) -> tuple[Contribution, ...]:
        """Return the classes that make an ``_inherit`` cycle through this one's model.

        These are the classes, in the addon's view, whose ``_inherit`` names another
        model of the cycle, in registration order; empty if there is no cycle.
        """
        cyclic = self._cyclic_models(contribution.model, contribution.addon)
        members = {
            item
            for model in cyclic
            for item in self._candidates(model, contribution.addon)
            if any(parent in cyclic and parent != item.model for parent in item.parents)
        }
        return tuple(sorted(members, key=lambda item: item.position))

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

    def addons_outside_defining(self, model: str, addon_name: str) -> tuple[str, ...]:
        """Addons outside an addon's dependency closure that define a model.

        Asked when no addon in the view defines the model: a non-empty answer means
        the model exists, but the addon does not depend on it (often guarded code,
        ``if "x" in self.env``), rather than a misspelled name.
        """
        closure = self.addons.closure(addon_name)
        return tuple(name for name in self._defining_addons(model) if name not in closure)

    def _defining_addons(self, model: str) -> tuple[str, ...]:
        """Addons anywhere on the addons paths that define (not extend) a model.

        Only asked on an error path. Addons whose sources do not contain
        ``_name = "<model>"`` are skipped without being parsed.
        """
        if model not in self._definers:
            self._definers[model] = tuple(
                name
                for name in self._text_definers().get(model, ())
                if any(_defines(item) for item in self._models_of(name).get(model, ()))
            )
        return self._definers[model]

    def _text_definers(self) -> dict[str, list[str]]:
        """Model name -> addons whose sources contain ``_name = "<model>"``, in load order.

        One text search over every addon, done the first time any model is looked up.
        """
        if self._definition_index is None:
            index: dict[str, list[str]] = {}
            for name in sorted(self.addons.addons, key=self.addons.load_key):
                for model in _defined_names(self.addons.addons[name].path):
                    index.setdefault(model, []).append(name)
            self._definition_index = index
        return self._definition_index


def _add_last(nodes: dict[Contribution | str, None], added: Nodes) -> None:
    """``LastOrderedSet.add``: an element added again moves to the end."""
    for node in added:
        nodes.pop(node, None)
        nodes[node] = None


def _since_last_definition(classes: list[Contribution]) -> list[Contribution]:
    """The classes from the last one that (re)defines the model: Odoo drops the others."""
    start = max((index for index, item in enumerate(classes) if _defines(item)), default=0)
    return classes[start:]


def _parents(contribution: Contribution) -> tuple[str, ...]:
    """``_inherit``, plus ``base``, which ``_build_model`` appends to every other model."""
    if contribution.model == BASE or BASE in contribution.parents:
        return contribution.parents
    return (*contribution.parents, BASE)


def _chain_log_access(chain: Sequence[Contribution], model: str) -> bool:
    """Whether a model's classes leave ``_log_access`` on (see :meth:`Registry.log_access`)."""
    if model == "base":
        return False
    for item in chain:
        if item.model == model and item.log_access is not None:
            return item.log_access
    return True


def _defines(contribution: Contribution) -> bool:
    """Whether a class defines its model rather than extending an existing one."""
    return contribution.model not in contribution.parents


def _defined_names(directory: Path) -> set[str]:
    """Model names in ``_name = "..."`` assignments of an addon's files (a text search)."""
    names: set[str] = set()
    for root, subdirs, files in os.walk(directory):
        subdirs[:] = [name for name in subdirs if name not in SKIPPED_DIRS]
        for name in files:
            if name.endswith(".py"):
                found = DEFINITION_PATTERN.findall(Path(root, name).read_bytes())
                names.update(match.decode(errors="replace") for match in found)
    return names


def _warn_unparsable(error: SourceError) -> None:
    """Tell the user why the models of a file are missing (once per file and run)."""
    print(
        f"odoo-typing: warning: cannot parse {error.path}: {error.reason}; "
        "the Odoo models it defines are ignored",
        file=sys.stderr,
    )


def _warn_misnamed(module: str, addon: Addon) -> None:
    """Tell the user that mypy uses another module name than the plugin for an addon."""
    prefix = addon.module_prefix
    if prefix.startswith("odoo.addons."):
        hint = f'check the addon as "{prefix}" (mypy -p {prefix}, or its path under overlay_root)'
    else:
        hint = 'set overlay_root (see odoo-typing-root) to use "odoo.addons.<name>" names'
    print(
        f'odoo-typing: warning: mypy names module "{module}" of addon "{addon.name}", '
        f'but this configuration names the addon "{prefix}"; its files are probably '
        f"analyzed twice, and the plugin adds no dependencies for them: {hint}",
        file=sys.stderr,
    )
