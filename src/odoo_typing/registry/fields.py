"""The field index: ``env[model]._fields`` and the method names, statically.

The definitions of a view's classes are merged by :mod:`odoo_typing.model_fields`; this
layer adds what needs other models: delegated fields and the comodels of ``related=``
fields.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType

from odoo_typing.addons import BASE
from odoo_typing.config import Settings
from odoo_typing.model_fields import (
    chain_definitions,
    delegated_field,
    delegation_fields,
    merge_definitions,
    service_definitions,
)
from odoo_typing.references import RELATIONAL
from odoo_typing.registry.scans import IndexLoader, ScanCache
from odoo_typing.registry.views import ModelViews
from odoo_typing.registry.visible import defines
from odoo_typing.scanner import Contribution, FieldInfo

__all__ = ["ModelFields", "chain_log_access"]


class ModelFields(ModelViews):
    """The fields and methods of a model's registry class as an addon sees it."""

    def __init__(
        self, settings: Settings, index: IndexLoader | None = None, scans: ScanCache | None = None
    ) -> None:
        super().__init__(settings, index, scans)
        self._fields: dict[tuple[str, str], Mapping[str, FieldInfo]] = {}
        self._unresolved_fields: dict[tuple[str, str], Mapping[str, FieldInfo]] = {}
        self._related_comodels: dict[tuple[str, str, str], str | None] = {}
        # One object for equal answers: the views of a model from many addons mostly
        # see the same fields (memory: each view would hold its own copies otherwise).
        self._field_pool: dict[FieldInfo, FieldInfo] = {}
        self._fields_pool: dict[tuple[tuple[str, FieldInfo], ...], Mapping[str, FieldInfo]] = {}

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
            merged = self._merged_fields(model, addon_name)
            resolved = {
                name: replace(info, comodel=self._comodel(model, name, addon_name))
                for name, info in merged.items()
                if _related_only(info)
            }
            self._fields[key] = self._shared({**merged, **resolved}) if resolved else merged
        return self._fields[key]

    def _shared(self, fields: dict[str, FieldInfo]) -> Mapping[str, FieldInfo]:
        """A read-only mapping equal to ``fields``, the same object for equal ones."""
        pool = self._field_pool
        items = tuple((name, pool.setdefault(info, info)) for name, info in fields.items())
        if items not in self._fields_pool:
            self._fields_pool[items] = MappingProxyType(dict(items))
        return self._fields_pool[items]

    def _merged_fields(self, model: str, addon_name: str) -> Mapping[str, FieldInfo]:
        """:meth:`fields` before the comodels of ``related=`` fields are known."""
        key = (model, addon_name)
        if key not in self._unresolved_fields:
            self._unresolved_fields[key] = {}  # a model in a delegation cycle sees no fields there
            self._unresolved_fields[key] = self._shared(self._compute_fields(model, addon_name))
        return self._unresolved_fields[key]

    def _comodel(self, model: str, name: str, addon_name: str) -> str | None:
        """The comodel of a field; of a relational ``related=`` field, its path's last field's.

        As Odoo's ``_setup_related_full``; the steps of the path may be such fields too
        (``company_id`` related to ``move_id.company_id``). Each field is resolved once,
        whatever the order :meth:`fields` is called in; a path the scan cannot follow, or
        one that leads back to the field, gives ``None``.
        """
        info = self._merged_fields(model, addon_name).get(name)
        if info is None or "comodel" in info.dynamic:
            return None
        if not _related_only(info):
            return info.comodel
        key = (model, name, addon_name)
        if key not in self._related_comodels:
            self._related_comodels[key] = None  # a cycle of related fields
            assert info.related is not None
            current: str | None = model
            for step in info.related.split("."):
                if current is None:
                    break
                current = self._comodel(current, step, addon_name)
            self._related_comodels[key] = current
        return self._related_comodels[key]

    def _compute_fields(self, model: str, addon_name: str) -> dict[str, FieldInfo]:
        view = self.view_class(model, addon_name)
        if view is None:
            return {}
        definitions = {
            info.name: [info] for info in service_definitions(chain_log_access(view.chain, model))
        }
        for name, found in chain_definitions(view.chain).items():
            definitions.setdefault(name, []).extend(found)
        fields = {name: merge_definitions(found) for name, found in definitions.items()}
        inherited: dict[str, FieldInfo] = {}
        for delegate, field_name in delegation_fields(view.chain, fields).items():
            if delegate != model:
                for name, info in self._merged_fields(delegate, addon_name).items():
                    inherited[name] = delegated_field(info, field_name)
        for name, info in inherited.items():
            fields.setdefault(name, info)
        return fields

    def has_method(self, model: str, addon_name: str, name: str) -> bool:
        """Whether a class of a model's view defines a method (not BaseModel's).

        Looked up in the classes each time: a set of every view's method names would be
        the registry's largest cache, for one membership test per field definition.
        """
        view = self.view_class(model, addon_name)
        return view is not None and any(name in item.methods for item in view.chain)

    def methods(self, model: str, addon_name: str) -> frozenset[str]:
        """Names of the methods the classes of a model's view define (for messages)."""
        view = self.view_class(model, addon_name)
        chain = view.chain if view is not None else ()
        return frozenset(name for item in chain for name in item.methods)

    def log_access(self, contribution: Contribution) -> bool:
        """Whether Odoo adds ``create_uid``/``create_date``/... to the model's class.

        For a concrete model, as Odoo: whether some class of its MRO that defines a
        model (not abstract; also of an ``_inherit`` parent model) adds them, which it
        does unless its body turns ``_log_access`` (or ``_auto``) off. For a mixin (an
        ``AbstractModel``, whose ``_auto`` is ``False``) the most derived class of the
        same model that sets ``_log_access`` decides, default on: the mixin itself gets
        the fields that every model using it has. Never for ``base``: its class is in
        the MRO of every model, which would give the fields to models without them too.
        """
        return chain_log_access(self.linearization(contribution), contribution.model)


def _related_only(info: FieldInfo) -> bool:
    """Whether a relational field takes its comodel from its ``related=`` path."""
    return info.field_type in RELATIONAL and info.comodel is None and bool(info.related)


def chain_log_access(chain: Sequence[Contribution], model: str) -> bool:
    """Whether a model's classes leave ``_log_access`` on (see :meth:`Registry.log_access`).

    As ``MetaModel`` does: each class that defines a model (of any model in the chain,
    not abstract) adds the fields unless its body turns ``_log_access`` (or ``_auto``)
    off, and the registry class gets those of every such class in its MRO. A model
    that is abstract (or whose definition the scan cannot tell) gets the fields unless
    its own most derived setting turns them off, like the models that use the mixin.
    """
    if model == "base":
        return False
    definition = next((item for item in chain if item.model == model and defines(item)), None)
    if definition is not None and definition.abstract is False:
        return any(
            item.log_access is not False
            for item in chain
            if defines(item) and item.model != BASE and item.abstract is not True
        )
    for item in chain:
        if item.model == model and item.log_access is not None:
            return item.log_access
    return True
