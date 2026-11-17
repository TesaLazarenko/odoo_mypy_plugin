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
from odoo_typing.computed import Call, computed_calls, recomputed_text
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
from odoo_typing.scanner.class_bodies import FORWARD

__all__ = ["ModelFields", "Passed", "chain_log_access"]

# (method, parameter) -> the (path, kind) of the containers the parameter is given.
Passed = Mapping[tuple[str, str], frozenset[tuple[tuple[str, ...], str]]]
NOTHING_PASSED: Passed = MappingProxyType({})


class ModelFields(ModelViews):
    """The fields and methods of a model's registry class as an addon sees it."""

    def __init__(
        self, settings: Settings, index: IndexLoader | None = None, scans: ScanCache | None = None
    ) -> None:
        super().__init__(settings, index, scans)
        self._fields: dict[tuple[str, str], Mapping[str, FieldInfo]] = {}
        self._unresolved_fields: dict[tuple[str, str], Mapping[str, FieldInfo]] = {}
        self._related_comodels: dict[tuple[str, str, str], str | None] = {}
        self._field_sources: dict[tuple[str, str], tuple[str, ...]] = {}
        self._field_names: dict[str, frozenset[str]] = {}  # of all models an addon sees
        # One object for equal answers: the views of a model from many addons mostly
        # see the same fields (memory: each view would hold its own copies otherwise).
        self._field_pool: dict[FieldInfo, FieldInfo] = {}
        # Equal field maps by a hash of their pooled infos (a key of the names and infos
        # themselves took more memory than the maps it pooled).
        self._fields_pool: dict[int, list[Mapping[str, FieldInfo]]] = {}
        self._passed: dict[str, Passed] = {}
        self._computed: dict[tuple[str, str, str], tuple[Call, ...] | None] = {}

    def value_keys(self, model: str, addon_name: str) -> frozenset[str]:
        """The keys the ``create`` / ``write`` of a model's classes read from their values.

        As an addon sees the model (its view's classes): an override that pops a key
        (``vals.pop("counterpart_account_id", None)``) takes it although it is no field.
        Asked on an error path only (a key that is no field).
        """
        view = self.view_class(model, addon_name)
        if view is None:
            return frozenset()
        return frozenset(key for item in view.chain for key in item.value_keys)

    def passed(self, contribution: Contribution) -> Passed:
        """The containers the parameters of a class's methods are given by its classes.

        Where the annotation of a parameter names another container than the class that
        wrote it passes (:func:`odoo_typing.scanner.class_bodies.passed_containers`): by
        the classes of the class's own registry class, so the answer is the same for
        every addon that reads the class. A parameter that a method passes on to
        another method is given what the first is, to a fixed point.
        """
        key = contribution.fullname
        if key not in self._passed:
            entries = [entry for item in self.linearization(contribution) for entry in item.passed]
            self._passed[key] = _passed(entries) if entries else NOTHING_PASSED
        return self._passed[key]

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

    def has_field_named(self, name: str, addon_name: str) -> bool:
        """Whether any model an addon sees has a field of a name.

        ``if "x" in records._fields:`` on a name none has is a test for an addon outside
        the dependencies. Asked where an error would be reported: the names of a view
        are collected at the first question about it.
        """
        if addon_name not in self._field_names:
            self._field_names[addon_name] = frozenset(
                field
                for model in self.models(addon_name)
                for field in self.fields(model, addon_name)
            )
        return name in self._field_names[addon_name]

    def field_sources(self, model: str, addon_name: str) -> tuple[str, ...]:
        """The models whose classes :meth:`fields` reads for a model, the model first.

        The model, the models it delegates to (``_inherits``) and the models the paths of
        its relational ``related=`` fields go through (their comodels come from there),
        with their delegates: what a daemon check of the fields depends on.
        """
        key = (model, addon_name)
        if key not in self._field_sources:
            found = dict.fromkeys([model])
            view = self.view_class(model, addon_name)
            if view is not None:
                found.update(dict.fromkeys(self.delegate_closure(view.chain, model, addon_name)))
            seen: set[tuple[str, str]] = set()
            for name, info in self._merged_fields(model, addon_name).items():
                if _related_only(info):
                    self._related_models(model, name, addon_name, found, seen)
            for other in list(found)[1:]:  # a step's field may be one its delegate gives
                other_view = self.view_class(other, addon_name)
                chain = other_view.chain if other_view is not None else ()
                found.update(dict.fromkeys(self.delegate_closure(chain, other, addon_name)))
            self._field_sources[key] = tuple(found)
        return self._field_sources[key]

    def _related_models(
        self,
        model: str,
        name: str,
        addon_name: str,
        found: dict[str, None],
        seen: set[tuple[str, str]],
    ) -> None:
        """Add the models a relational ``related=`` field's path goes through (recursively)."""
        if (model, name) in seen:
            return
        seen.add((model, name))
        info = self._merged_fields(model, addon_name).get(name)
        if info is None or info.related is None:
            return
        current: str | None = model
        for step in info.related.split("."):
            if current is None:
                return
            found[current] = None
            step_info = self._merged_fields(current, addon_name).get(step)
            if step_info is not None and _related_only(step_info):
                self._related_models(current, step, addon_name, found, seen)
            current = self._comodel(current, step, addon_name)
        if current is not None:
            found[current] = None

    def _shared(self, fields: dict[str, FieldInfo]) -> Mapping[str, FieldInfo]:
        """A read-only mapping equal to ``fields``, the same object for equal ones."""
        pool = self._field_pool
        infos = [pool.setdefault(info, info) for info in fields.values()]
        bucket = self._fields_pool.setdefault(hash((*fields, *map(id, infos))), [])
        for kept in bucket:
            if tuple(kept) == tuple(fields) and all(
                kept[name] is info for name, info in zip(fields, infos, strict=True)
            ):
                return kept
        made = MappingProxyType(dict(zip(fields, infos, strict=True)))
        bucket.append(made)
        return made

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
            info.name: [info]
            for info in service_definitions(
                chain_log_access(view.chain, model), self._settings.odoo_version
            )
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

    def computed_calls(
        self, model: str, addon_name: str, info: FieldInfo
    ) -> tuple[Call, ...] | None:
        """The calls whose results a field always holds, ``None`` if it may be empty.

        For a text field that each read computes, as an addon sees the model's classes
        (:func:`odoo_typing.computed.computed_calls`). The few fields that are such are
        kept; the others cost the test of their arguments.
        """
        if not recomputed_text(info):
            return None
        key = (model, addon_name, info.name)
        if key not in self._computed:
            view = self.view_class(model, addon_name)
            chain = view.chain if view is not None else ()
            self._computed[key] = computed_calls(info, chain)
        return self._computed[key]

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


def _passed(entries: Sequence[tuple[str, str, tuple[str, ...], str]]) -> Passed:
    """What each parameter is given, also through the parameters passed on to it."""
    given: dict[tuple[str, str], set[tuple[tuple[str, ...], str]]] = {}
    forwards: list[tuple[str, str, str, str]] = []  # callee, its parameter, method, parameter
    for method, parameter, path, kind in entries:
        if path[:1] == (FORWARD,):
            forwards.append((method, parameter, path[1], kind))
        else:
            given.setdefault((method, parameter), set()).add((path, kind))
    changed = bool(given)
    while changed:  # to a fixed point
        changed = False
        for callee, target, method, parameter in forwards:
            found = given.get((method, parameter), set())
            known = given.setdefault((callee, target), set())
            if not found <= known:
                known |= found
                changed = True
    return {name: frozenset(found) for name, found in given.items() if found}


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
