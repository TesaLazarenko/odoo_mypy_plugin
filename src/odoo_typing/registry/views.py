"""The registry class of a model as an addon sees it, and the models it delegates to.

``env[model]`` in an addon is a :class:`ModelView`; ``_inherits`` (and
``Many2one(delegate=True)``) add the fields of the delegated models' views.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from odoo_typing.config import Settings
from odoo_typing.registry.linearization import Linearization
from odoo_typing.registry.scans import IndexLoader, ScanCache
from odoo_typing.scanner import Contribution

__all__ = ["ModelView", "ModelViews"]


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


class ModelViews(Linearization):
    """A model's registry class in an addon's view; ``_inherits`` delegation."""

    def __init__(
        self, settings: Settings, index: IndexLoader | None = None, scans: ScanCache | None = None
    ) -> None:
        super().__init__(settings, index, scans)
        self._views: dict[tuple[str, str], ModelView | None] = {}

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
