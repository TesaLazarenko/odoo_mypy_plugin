"""The classes of a model that an addon sees: its own and those of its dependency closure.

Odoo registers the classes of a model in module load order; an extension registered
before any class defines the model does not count, and a redefinition starts over.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from odoo_typing.config import Settings
from odoo_typing.registry.scans import IndexLoader, ScanCache, ScannedAddons
from odoo_typing.scanner import Contribution

__all__ = ["TipsKey", "VisibleClasses", "defines"]

TipsKey = tuple[str, str, str | None]

# `_name = "x"`, also chained (`_name = _description = "x"`); group 1 is the name. It
# also matches `partner_name = "x"`: candidates are checked by a scan, and starting with
# a literal keeps the search fast (a look-behind would make it 20 times slower).
DEFINITION_PATTERN = re.compile(rb"""_name\s*=\s*(?:\w+\s*=\s*)*[rRuU]?['"]([^'"\\\n]+)['"]""")
SKIPPED_DIRS = frozenset({"static", "i18n", "node_modules", "__pycache__"})


class VisibleClasses(ScannedAddons):
    """Which classes make up a model in an addon's view, and which addons define it at all."""

    def __init__(
        self, settings: Settings, index: IndexLoader | None = None, scans: ScanCache | None = None
    ) -> None:
        super().__init__(settings, index, scans)
        self._candidate_memo: dict[tuple[str, str], tuple[Contribution, ...]] = {}
        self._tips: dict[TipsKey, tuple[Contribution, ...]] = {}
        # Equal answers kept once (by the identity of the classes: hashing them is costly).
        self._class_tuples: dict[tuple[int, ...], tuple[Contribution, ...]] = {}
        self._definers: dict[str, tuple[str, ...]] = {}
        self._definition_index: dict[str, list[str]] | None = None

    def models(self, addon_name: str) -> list[str]:
        """The models a class of an addon's closure defines, sorted."""
        found = {
            model for name in self.addons.closure(addon_name) for model in self._models_of(name)
        }
        return sorted(model for model in found if self.visible(model, addon_name))

    def visible(self, model: str, addon_name: str) -> bool:
        """Whether a class of an addon's closure defines a model (``view_class`` is not None).

        Cheaper than :meth:`view_class`: no MRO is computed.
        """
        return bool(self._candidates(model, addon_name))

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
            first = next((i for i, item in enumerate(found) if defines(item)), len(found))
            self._candidate_memo[key] = self._same_classes(tuple(found[first:]))
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
            tips = tuple(item for item in reversed(live) if item not in covered)
            self._tips[key] = self._same_classes(tips)
        return self._tips[key]

    def _predecessors(self, contribution: Contribution) -> frozenset[Contribution]:
        """The earlier classes of a contribution's model that its addon sees."""
        if defines(contribution):
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

    def concrete(self, model: str, addon_name: str) -> bool:
        """Whether the class that defines a model (as the addon sees it) is not abstract.

        ``False`` for mixins and ``base``, whose methods run on records of models with
        more fields, and when the scan cannot tell (a custom base class).
        """
        definition = self.definition(model, addon_name)
        return definition is not None and definition.abstract is False

    def _same_classes(self, classes: tuple[Contribution, ...]) -> tuple[Contribution, ...]:
        """An equal tuple of classes the registry already keeps, if there is one."""
        return self._class_tuples.setdefault(tuple(map(id, classes)), classes)

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
                if model in self._defined_models(name)
            )
        return self._definers[model]

    def _defined_models(self, addon_name: str) -> frozenset[str]:
        """The models the classes of an addon define (not extend).

        For an addon the run does not otherwise need (asked when a model is unknown in a
        view): its scan is read, but only this answer and its stamps are kept, not the
        whole scan (an addon such as ``website`` holds thousands of fields).
        """
        if addon_name in self._by_model:
            models = self._by_model[addon_name]
            return frozenset(model for model, items in models.items() if any(map(defines, items)))
        if addon_name not in self._peeked:
            scan, stamps = self._load_scan(addon_name)
            defined = frozenset(
                item.model
                for module in scan.modules
                for item in module.contributions
                if defines(item)
            )
            self._peeked[addon_name] = (defined, stamps)
        return self._peeked[addon_name][0]

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


def defines(contribution: Contribution) -> bool:
    """Whether a class defines its model rather than extending an existing one."""
    return contribution.model not in contribution.parents


def _since_last_definition(classes: list[Contribution]) -> list[Contribution]:
    """The classes from the last one that (re)defines the model: Odoo drops the others."""
    start = max((index for index, item in enumerate(classes) if defines(item)), default=0)
    return classes[start:]


def _defined_names(directory: Path) -> set[str]:
    """Model names in ``_name = "..."`` assignments of an addon's files (a text search)."""
    names: set[str] = set()
    findall = DEFINITION_PATTERN.findall
    for root, subdirs, files in os.walk(directory):
        subdirs[:] = [name for name in subdirs if name not in SKIPPED_DIRS]
        for name in files:
            if name.endswith(".py"):
                with open(os.path.join(root, name), "rb") as source:  # every file of every addon
                    data = source.read()
                if b"_name" in data:
                    names.update(match.decode(errors="replace") for match in findall(data))
    return names
