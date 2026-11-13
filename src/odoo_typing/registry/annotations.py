"""The models that ``odoo.values.*`` / ``odoo.model.*`` annotations name, and their keys.

An annotation names a model by its alias: the model name with ``_`` for ``.``
(``odoo.values.res_partner``, as the PyCharm plugin "Odoo IDE" spells it); a name with
dots is accepted too. The alias is resolved in the view of the annotating file's addon.

The keys of ``odoo.values.<model>`` are the fields that *any* addon on the addons paths
gives the model, not only those of the annotating addon's view: the values that
``super()._prepare_vals()`` returns in a later addon must have the type of the later
addon's annotation, and mypy compares TypedDicts by their keys. So the keys are the
same everywhere. Finding them must not scan every addon (seconds for ``res.partner``):
a text index finds the files whose ``_name`` / ``_inherit`` name a model
(:mod:`odoo_typing.registry.class_index`), and only those files are read, each on its
own (:func:`odoo_typing.scanner.scan_file_classes`), kept for their stamps.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from odoo_typing.addons import BASE
from odoo_typing.config import Settings
from odoo_typing.model_fields import service_definitions, stored
from odoo_typing.records import Record, digest, parse_stamps
from odoo_typing.registry.class_index import ClassFileIndex, module_of
from odoo_typing.registry.fields import ModelFields, chain_log_access
from odoo_typing.registry.scans import IndexLoader, ScanCache
from odoo_typing.scanner import Contribution, scan_file_classes
from odoo_typing.stamps import Stamp

__all__ = ["UNCHECKED", "AliasResolution", "AnnotatedModels", "normalized_alias"]

# The field class of a field whose values are not checked: a computed binary field that
# is not stored holds any value for a widget (see odoo_typing.vals).
UNCHECKED: Final = "*"
BINARY_CLASSES: Final = frozenset({"Binary", "Image"})


def normalized_alias(name: str) -> str:
    """The form an alias and a model name are compared in: ``res_partner``."""
    return name.replace(".", "_")


@dataclass(frozen=True, slots=True)
class AliasResolution:
    """The model an annotation alias names, as an addon sees it.

    Attributes:
        model: The model, if exactly one model of the addon's view has the alias.
        candidates: The models of the view (or, if none, anywhere) with the alias, when
            more than one has it.
        outside_model: The model, if only addons outside the addon's closure define it.
        outside: Those addons.
    """

    model: str | None
    candidates: tuple[str, ...] = ()
    outside_model: str | None = None
    outside: tuple[str, ...] = ()


class AnnotatedModels(ModelFields):
    """Resolve annotation aliases; the keys of ``odoo.values.*`` (see the module docstring)."""

    def __init__(
        self, settings: Settings, index: IndexLoader | None = None, scans: ScanCache | None = None
    ) -> None:
        super().__init__(settings, index, scans)
        self._visible_aliases: dict[str, dict[str, tuple[str, ...]]] = {}
        self._defined_aliases: dict[str, tuple[str, ...]] | None = None
        self._class_index: ClassFileIndex | None = None
        self._file_classes: dict[Path, tuple[Stamp | None, tuple[Contribution, ...]]] = {}
        self._values_fields: dict[str, Mapping[str, frozenset[str]]] = {}
        self._values_models: dict[str, frozenset[str]] = {}  # the models keys come from
        self._values_digests: dict[str, str] = {}
        self._addon_modules: dict[str, tuple[str, ...]] = {}

    def inputs_changed(self) -> bool:
        """Also the files the keys of ``odoo.values.*`` were read from (see the module)."""
        if super().inputs_changed():
            return True
        index = self._class_index
        return index is not None and index.changed()

    # -- aliases ---------------------------------------------------------------------------

    def resolve_alias(self, alias: str, addon_name: str) -> AliasResolution:
        """The model an alias names in an addon's view (see :class:`AliasResolution`)."""
        key = normalized_alias(alias)
        visible = self._aliases_of(addon_name).get(key, ())
        if len(visible) == 1:
            return AliasResolution(visible[0])
        if visible:
            return AliasResolution(None, candidates=visible)
        defined = tuple(
            model for model in self._all_aliases().get(key, ()) if self._defining_addons(model)
        )
        if len(defined) > 1:
            return AliasResolution(None, candidates=defined)
        if defined:
            outside = self.addons_outside_defining(defined[0], addon_name)
            return AliasResolution(None, outside_model=defined[0], outside=outside)
        return AliasResolution(None)

    def annotation_models(self, path: str | Path) -> frozenset[str]:
        """The models that a file's ``odoo.values.*`` / ``odoo.model.*`` names resolve to."""
        aliases = self.annotation_aliases(path)
        addon = self.addon_name_for_path(path) if aliases else None
        if addon is None:
            return frozenset()
        models = (self.resolve_alias(alias, addon).model for _, alias in aliases)
        return frozenset(model for model in models if model is not None)

    def addon_modules(self, addon_name: str) -> tuple[str, ...]:
        """The modules of an addon that its scan reached, sorted."""
        if addon_name not in self._addon_modules:
            modules = sorted(scan.module for scan in self._scans_of(addon_name))
            self._addon_modules[addon_name] = tuple(modules)
        return self._addon_modules[addon_name]

    def _aliases_of(self, addon_name: str) -> dict[str, tuple[str, ...]]:
        """The models of an addon's view by alias."""
        if addon_name not in self._visible_aliases:
            found: dict[str, list[str]] = {}
            for model in self.models(addon_name):
                found.setdefault(normalized_alias(model), []).append(model)
            self._visible_aliases[addon_name] = {key: tuple(v) for key, v in found.items()}
        return self._visible_aliases[addon_name]

    def _all_aliases(self) -> dict[str, tuple[str, ...]]:
        """The models that some addon's sources may define (a text search), by alias."""
        if self._defined_aliases is None:
            found: dict[str, list[str]] = {}
            for model in self._text_definers():
                found.setdefault(normalized_alias(model), []).append(model)
            self._defined_aliases = {key: tuple(sorted(v)) for key, v in found.items()}
        return self._defined_aliases

    def accepted_values(self, model: str, addon_name: str) -> frozenset[str]:
        """The models whose ``odoo.values.*`` a model's ``create`` / ``write`` take.

        Its own, those of the models its registry class inherits (``_inherit`` parents and
        mixins: their fields are its fields) and those of the models it delegates to
        (``_inherits``: Odoo writes their fields through to the parent record).
        """
        view = self.view_class(model, addon_name)
        chain = view.chain if view is not None else ()
        delegated = self.delegate_closure(chain, model, addon_name)
        return frozenset({model, *(item.model for item in chain), *delegated})

    # -- keys of odoo.values.<model> ----------------------------------------------------------

    def values_fields(self, model: str) -> Mapping[str, frozenset[str]]:
        """The fields any addon on the addons paths gives a model, with their field classes.

        As Odoo assembles a model's fields: the service fields (``id``, ``create_uid``...)
        and the fields its classes declare; then those of its ``_inherit`` parents and
        ``base`` (recursively, with their service fields: Odoo adds those to every
        class that defines a model); then, for the models it
        delegates to (``_inherits``, ``delegate=True``), their fields. A field of a
        parent or a delegated model counts only if the model has no field of that name.
        A field that addons declare with different classes has all of them; a computed
        binary field that is not stored has :data:`UNCHECKED`.
        """
        if model not in self._values_fields:
            self._values_fields[model] = MappingProxyType({})  # an _inherit or _inherits cycle
            files = self._class_files().files(model)
            classes = [
                item
                for addon_name, path in files
                for item in self._classes_in(addon_name, path)
                if item.model == model
            ]
            involved = {model}
            found: dict[str, set[str]] = {}
            for info in service_definitions(chain_log_access(classes, model)):
                found.setdefault(info.name, set()).add(info.field_type)
            for item in classes:
                for info in item.fields:
                    unchecked = info.field_type in BINARY_CLASSES and not stored(info)
                    kind = UNCHECKED if unchecked else info.field_type
                    found.setdefault(info.name, set()).add(kind)
            parents = {parent for item in classes for parent in item.parents} | {BASE}
            involved.update(self._add_missing(found, parents - {model}))
            delegates = {name for item in classes for name in item.delegates}
            involved.update(self._add_missing(found, delegates - {model}))
            self._values_fields[model] = MappingProxyType(
                {name: frozenset(types) for name, types in sorted(found.items())}
            )
            self._values_models[model] = frozenset(involved)
        return self._values_fields[model]

    def values_digest(self, alias: str) -> str:
        """Digest of the keys of every model that an alias may name, on any addons path.

        For the config data of a file that uses ``odoo.values.<alias>``
        (:mod:`odoo_typing.fingerprint`), asked on every run: the keys come from every
        addon, so a field added anywhere must make mypy check the file again, or its
        cached ``TypedDict`` would differ from the one of a file checked after the
        change. Kept between runs with the files of the models the keys come from.
        """
        key = normalized_alias(alias)
        if key not in self._values_digests:
            self._values_digests[key] = self._compute_values_digest(key)
        return self._values_digests[key]

    def _compute_values_digest(self, key: str) -> str:
        index = self._class_files()
        models = sorted(m for m in index.model_names() if normalized_alias(m) == key)
        record_key = f"values-{key}-{index.key}.json"
        record = self._scans.load_record(record_key) if self._scans is not None else None
        if record is not None and (kept := self._kept_digest(record, models)) is not None:
            return kept
        keys = [
            [model, [[name, sorted(types)] for name, types in self.values_fields(model).items()]]
            for model in models
        ]
        value = digest(keys)
        if self._scans is not None:
            involved = sorted({m for model in models for m in self._values_models[model]})
            files = {m: [str(path) for _, path in index.files(m)] for m in involved}
            stamps = {path: index.stamp(Path(path)) for paths in files.values() for path in paths}
            record = {"digest": value, "models": models, "files": files, "stamps": stamps}
            self._scans.save_record(record_key, record)
        return value

    def _kept_digest(self, record: Record, models: list[str]) -> str | None:
        """The digest of a kept record if its models' files are unchanged."""
        value, files, stamps = record.get("digest"), record.get("files"), record.get("stamps")
        recorded = parse_stamps(stamps)
        if not isinstance(value, str) or record.get("models") != models:
            return None
        if not isinstance(files, dict) or recorded is None:
            return None
        index = self._class_files()
        for model, paths in files.items():
            if [str(path) for _, path in index.files(model)] != paths:
                return None  # a file names the model now, or no longer
        if any(index.stamp(Path(path)) != stamp for path, stamp in recorded.items()):
            return None
        return value

    def values_sources(self, model: str) -> tuple[frozenset[str], frozenset[str]]:
        """Where the keys of a model come from (its parents' too), for the daemon.

        Returns:
            The full names of the classes, and the modules of the files that have them.
        """
        self.values_fields(model)
        addons = self.addons.addons
        classes: set[str] = set()
        modules: set[str] = set()
        for other in self._values_models[model]:
            for name, path in self._class_files().files(other):
                module = module_of(addons[name], path)
                if module is None:
                    continue
                modules.add(module)
                classes.update(
                    f"{module}.{item.class_name}"
                    for item in self._classes_in(name, path)
                    if item.model == other
                )
        return frozenset(classes), frozenset(modules)

    def _add_missing(self, found: dict[str, set[str]], models: set[str]) -> frozenset[str]:
        """Add the fields of other models that ``found`` has no field of that name for.

        Returns:
            The models those models' keys come from (themselves included).
        """
        own = set(found)
        involved: set[str] = set()
        for other in sorted(models):
            for name, types in self.values_fields(other).items():
                if name not in own:
                    found.setdefault(name, set()).update(types)
            involved.update(self._values_models.get(other, {other}))  # none yet: a cycle
        return frozenset(involved)

    def _class_files(self) -> ClassFileIndex:
        """The files of every addon that name models (kept between runs, see the module)."""
        if self._class_index is None:
            index = self.addons
            addons = [index.addons[name] for name in sorted(index.addons, key=index.load_key)]
            self._class_index = ClassFileIndex(addons, self._scans)
        return self._class_index

    def _classes_in(self, addon_name: str, path: Path) -> tuple[Contribution, ...]:
        """The model classes of one file read on its own, kept for its stamp."""
        current = self._class_files().stamp(path)
        known = self._file_classes.get(path)
        if known is not None and known[0] == current:
            return known[1]
        stored = self._scans.load_classes(path, current) if self._scans else None
        if stored is None:
            stored = scan_file_classes(self.addons.addons[addon_name], path)
            if self._scans is not None:
                self._scans.save_classes(path, current, stored)
        self._file_classes[path] = (current, stored)
        return stored
