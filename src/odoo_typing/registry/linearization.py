"""The MRO of a model's registry class, as ``BaseModel._build_model`` and C3 build it.

The ``LastOrderedSet`` of ``_build_model`` (the model's classes, then the registry
classes of its ``_inherit`` parents, ``base`` last), linearized with C3. ``_inherit``
cycles, which Odoo cannot build, are found as strongly connected components of the
model graph first; the edges inside them are ignored. An ``_inherit`` that Odoo would
not find when it builds a class is a :class:`MissingParent`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial

from odoo_typing.addons import BASE
from odoo_typing.config import Settings
from odoo_typing.graphs import c3_merge, components_from
from odoo_typing.registry.scans import IndexLoader, ScanCache
from odoo_typing.registry.visible import TipsKey, VisibleClasses
from odoo_typing.scanner import Contribution

__all__ = ["Linearization", "MissingParent", "Nodes"]

# A registry MRO: classes, and model names standing for a parent's registry class.
Nodes = tuple["Contribution | str", ...]


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


class Linearization(VisibleClasses):
    """The bases and the MRO that Odoo gives the registry class of each model."""

    def __init__(
        self, settings: Settings, index: IndexLoader | None = None, scans: ScanCache | None = None
    ) -> None:
        super().__init__(settings, index, scans)
        self._mros: dict[TipsKey, Nodes] = {}
        self._merged: dict[tuple[str, tuple[str, ...]], Nodes] = {}
        self._conflicts: set[tuple[str, tuple[str, ...]]] = set()
        self._explored: dict[str, set[str]] = {}  # per addon: models classified for cycles
        self._cycles: dict[str, dict[str, frozenset[str]]] = {}

    def linearization(self, contribution: Contribution) -> tuple[Contribution, ...]:
        """The classes of a contribution's MRO, itself first, as its addon sees them.

        This is the MRO of Odoo's registry class for the model right after the class is
        registered: the earlier classes of the model, then the registry classes of its
        ``_inherit`` parents, ``base`` last. A parent's registry class holds *all* its
        classes in the addon's view, also later ones: Odoo extends it in place.
        """
        nodes = self._registry_mro(contribution.model, contribution.addon, contribution)
        return tuple(node for node in nodes if isinstance(node, Contribution))

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


def _add_last(nodes: dict[Contribution | str, None], added: Nodes) -> None:
    """``LastOrderedSet.add``: an element added again moves to the end."""
    for node in added:
        nodes.pop(node, None)
        nodes[node] = None


def _parents(contribution: Contribution) -> tuple[str, ...]:
    """``_inherit``, plus ``base``, which ``_build_model`` appends to every other model."""
    if contribution.model == BASE or BASE in contribution.parents:
        return contribution.parents
    return (*contribution.parents, BASE)
