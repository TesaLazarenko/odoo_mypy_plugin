"""What the registry reads from disk: the addon index, each addon's scan, its XML ids.

The bottom layer of :class:`odoo_typing.registry.Registry`. The manifests are read on the
first question, and an addon's files are scanned only when a question needs the addon.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import cached_property, partial
from pathlib import Path
from typing import Protocol

from odoo_typing.addons import Addon, AddonIndex, LoadKey
from odoo_typing.config import Settings
from odoo_typing.frozen import frozen
from odoo_typing.guards import Guard, is_guarded, model_guards
from odoo_typing.node_index import index_nodes
from odoo_typing.records import Record
from odoo_typing.scanner import (
    AddonScan,
    AnnotationAlias,
    Contribution,
    ModuleScan,
    SourceError,
    annotation_aliases,
    parse_bytes,
    read_source,
    referenced_models,
    scan_addon,
)
from odoo_typing.stamps import UNREADABLE, Stamp, Stamps, matches, read_index, stamp, unchanged

__all__ = ["NO_REFERENCES", "FileReferences", "IndexLoader", "ScanCache", "ScannedAddons"]


class ScanCache(Protocol):
    """Addon scans kept between runs (see :class:`odoo_typing.scan_store.ScanStore`)."""

    def load(self, addon: Addon, load_key: LoadKey) -> tuple[AddonScan, Stamps] | None:
        """The stored scan and its stamps, if none of its files changed."""

    def save(self, addon: Addon, load_key: LoadKey, scan: AddonScan, stamps: Stamps) -> None:
        """Keep a scan with the stamps of the files it read."""

    def load_file(self, path: Path, current: Stamp | None) -> FileReferences | None:
        """What a file no scan reaches refers to, if stored for its stamp."""

    def save_file(self, path: Path, current: Stamp | None, found: FileReferences) -> None:
        """Keep what a file no scan reaches refers to, with its stamp."""

    def load_classes(self, path: Path, current: Stamp | None) -> tuple[Contribution, ...] | None:
        """The model classes of one file read on its own, if stored for its stamp."""

    def save_classes(
        self, path: Path, current: Stamp | None, classes: tuple[Contribution, ...]
    ) -> None:
        """Keep the model classes of one file read on its own, with its stamp."""

    def load_record(self, key: str) -> Record | None:
        """A record kept under a key (the text index of model files), if any."""

    def save_record(self, key: str, record: Record) -> None:
        """Keep a record under a key."""


IndexLoader = Callable[[], tuple[AddonIndex, Stamps]]


@dataclass(frozen=True, slots=True)
class FileReferences:
    """What a file the addon scans did not reach refers to (see :class:`ModuleScan`)."""

    referenced_models: frozenset[str]
    guards: tuple[Guard, ...]
    annotations: frozenset[AnnotationAlias]


NO_REFERENCES = FileReferences(frozenset(), (), frozenset())


@dataclass(frozen=True, slots=True)
class _UnreachedFile:
    """A file the addon scans did not reach, as it was when parsed (its stamp)."""

    stamp: Stamp | None
    found: FileReferences


class ScannedAddons:
    """The addons on the addons paths and the scans of their files, read on demand."""

    def __init__(
        self, settings: Settings, index: IndexLoader | None = None, scans: ScanCache | None = None
    ) -> None:
        """Create a registry for the configured Odoo installation.

        Nothing is read from disk yet: a fully cached mypy run never asks a question.

        Args:
            settings: Plugin settings.
            index: Returns the addon index and the stamps of what it was built from
                (default: :func:`odoo_typing.stamps.read_index`).
            scans: Addon scans kept between runs (:mod:`odoo_typing.scan_store`); every
                addon is scanned again without it.
        """
        self._settings = settings
        self._scans = scans
        self._index_loader = index or partial(
            read_index, settings.odoo_path, settings.addons_paths, settings.custom_prefix
        )
        self._index_stamps: Stamps = {}
        self._scan_results: dict[str, tuple[AddonScan, Stamps]] = {}
        self._by_module: dict[str, ModuleScan] = {}
        self._by_path: dict[Path, ModuleScan] = {}
        self._by_class: dict[str, Contribution] = {}
        self._by_model: dict[str, dict[str, list[Contribution]]] = {}
        self._unreached: dict[Path, _UnreachedFile] = {}  # files the addon scans miss
        self._xml_ids: dict[str, Mapping[str, str]] = {}  # of a closure (_ClosureIds)
        self._own_xml_ids: dict[str, Mapping[str, str]] = {}  # of one addon's data files
        self._misnamed: set[str] = set()  # addons warned about by misnamed()
        self._warned: set[str] = set()  # addons whose unparsable files were reported
        # Addons read only for the models they define (see _defined_models).
        self._peeked: dict[str, tuple[frozenset[str], Stamps]] = {}

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
        if not all(unchanged(stamps) for _, stamps in self._peeked.values()):
            return True
        return not all(unchanged(stamps) for _, stamps in self._scan_results.values())

    def _models_of(self, addon_name: str) -> dict[str, list[Contribution]]:
        """Scan an addon once; return its contributions by model name."""
        if addon_name not in self._by_model:
            models = self._by_model[addon_name] = {}
            stored = self._load_scan(addon_name)
            result = stored[0]
            self._scan_results[addon_name] = stored
            self._peeked.pop(addon_name, None)
            for scan in result.modules:
                self._index(scan, models)
        return self._by_model[addon_name]

    def _load_scan(self, addon_name: str) -> tuple[AddonScan, Stamps]:
        """An addon's scan and stamps: stored, or scanned now (and stored).

        A file it cannot parse is reported once per run, however often it is loaded.
        """
        addon = self.addons.addons[addon_name]
        load_key = self.addons.load_key(addon_name)
        stored = self._scans.load(addon, load_key) if self._scans else None
        if stored is None:
            stamps: Stamps = {}
            result = scan_addon(addon, load_key, stamps)  # stamps taken before each read
            stored = (result, stamps)
            if self._scans is not None:
                self._scans.save(addon, load_key, *stored)
        if addon_name not in self._warned:
            self._warned.add(addon_name)
            for error in stored[0].errors:
                _warn_unparsable(error)
        return stored

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
        that ``__init__.py`` does not import, are parsed once here (:meth:`_unreached_file`).
        """
        addon = self.addons.addon_for_path(path)
        if addon is not None:
            self._models_of(addon.name)
        resolved = self.addons.real_path(path)
        scan = self._by_path.get(resolved)
        if scan is not None:
            return scan.referenced_models
        return self._unreached_file(resolved).found.referenced_models

    def annotation_aliases(self, path: str | Path) -> frozenset[AnnotationAlias]:
        """The ``odoo.values.*`` / ``odoo.model.*`` names a source file uses.

        See :mod:`odoo_typing.scanner.annotations`; also for files the addon does not
        import, like :meth:`referenced_models`.
        """
        addon = self.addons.addon_for_path(path)
        if addon is None:
            return frozenset()
        self._models_of(addon.name)
        resolved = self.addons.real_path(path)
        scan = self._by_path.get(resolved)
        if scan is not None:
            return scan.annotations
        return self._unreached_file(resolved).found.annotations

    def guarded(self, path: str | Path, model: str, line: int) -> bool:
        """Whether a line of a source file runs only when a model is installed.

        See :mod:`odoo_typing.guards`; a file the addon scan did not reach is parsed
        once (:meth:`_unreached_file`).
        """
        addon = self.addons.addon_for_path(path)
        if addon is not None:
            self._models_of(addon.name)  # scans the addon
        resolved = self.addons.real_path(path)
        scan = self._by_path.get(resolved)
        guards = scan.guards if scan is not None else self._unreached_file(resolved).found.guards
        return is_guarded(guards, model, line)

    def source_stamp(self, path: str | Path) -> Stamp | None:
        """A source file's stamp as the answers about it were read (scan or own parse)."""
        addon = self.addons.addon_for_path(path)
        resolved = self.addons.real_path(path)
        if addon is not None:
            _, stamps = self.scan_result(addon.name)
            if str(resolved) in stamps:
                return stamps[str(resolved)]
        known = self._unreached.get(resolved)
        return known.stamp if known is not None else stamp(path)

    def _unreached_file(self, path: Path) -> _UnreachedFile:
        """What a file the addon scans did not reach refers to; parsed once per version.

        Its stamp is checked on each use: no addon scan records it, so a daemon update
        that changes it (a test file) does not start a new registry. One that cannot be
        read is kept as :data:`~odoo_typing.stamps.UNREADABLE` until it can be.
        """
        current = stamp(path)
        known = self._unreached.get(path)
        if known is not None and matches(path, known.stamp, lambda _: current):
            return known
        stored = self._scans.load_file(path, current) if self._scans else None
        if stored is not None:
            found = _UnreachedFile(current, stored)
        else:
            found = self._parse_unreached(path, current)
        self._unreached[path] = found
        return found

    def _parse_unreached(self, path: Path, current: Stamp | None) -> _UnreachedFile:
        """Parse a file the addon scans did not reach (and store what it refers to)."""
        try:
            source = read_source(path)
        except SourceError:  # mypy reports files it cannot read itself
            return _UnreachedFile(UNREADABLE if current is not None else None, NO_REFERENCES)
        try:
            tree = parse_bytes(source, path)
        except SourceError:  # mypy reports unparsable files it checks itself
            return _UnreachedFile(current, NO_REFERENCES)
        nodes = index_nodes(tree)
        references = FileReferences(
            frozen(referenced_models(tree, nodes)),
            model_guards(nodes),
            annotation_aliases(source),
        )
        if self._scans is not None:
            self._scans.save_file(path, current, references)
        return _UnreachedFile(current, references)

    def addon_name_for_path(self, path: str | Path) -> str | None:
        """Return the name of the addon a source file belongs to."""
        addon = self.addons.addon_for_path(path)
        return addon.name if addon else None

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

    def xml_ids(self, addon_name: str) -> Mapping[str, str]:
        """The XML ids the data files of an addon's closure create, with their models.

        A later addon of the load order that writes a record of the same id keeps the
        model of the first (an override changes the record, not its model). A view over
        the ids of each addon (:meth:`own_xml_ids`), not a merged copy: every addon of a
        project would otherwise hold the ids of its whole closure (12.7 MB for 120 core
        addons).
        """
        found = self._xml_ids.get(addon_name)
        if found is None:
            names = sorted(self.addons.closure(addon_name), key=self.addons.load_key)
            own = (self.own_xml_ids(name) for name in names)
            found = self._xml_ids[addon_name] = _ClosureIds(tuple(ids for ids in own if ids))
        return found

    def own_xml_ids(self, addon_name: str) -> Mapping[str, str]:
        """The XML ids the data files of one addon create, with their models."""
        found = self._own_xml_ids.get(addon_name)
        if found is None:
            scan, _ = self.scan_result(addon_name)
            ids: dict[str, str] = {}
            for item in scan.data.xml_ids:
                ids.setdefault(item.name, sys.intern(item.model))
            found = self._own_xml_ids[addon_name] = ids
        return found

    def xml_id_outside(self, xml_id: str, addon_name: str) -> str | None:
        """The addon outside an addon's closure whose data files create an XML id.

        Only the addon the id is named after is read (``sale.x`` is ``sale``'s), and only
        its own data files, not its closure's.
        """
        module = xml_id.partition(".")[0]
        if module not in self.addons.addons or module in self.addons.closure(addon_name):
            return None
        return module if xml_id in self.own_xml_ids(module) else None


class _ClosureIds(Mapping[str, str]):
    """The XML ids of several addons, the first of the load order winning (read-only)."""

    __slots__ = ("_ids",)

    def __init__(self, ids: tuple[Mapping[str, str], ...]) -> None:
        self._ids = ids

    def __getitem__(self, key: str) -> str:
        for ids in self._ids:
            found = ids.get(key)
            if found is not None:
                return found
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        return any(key in ids for ids in self._ids)

    def __iter__(self) -> Iterator[str]:
        seen: set[str] = set()
        for ids in self._ids:
            for key in ids:
                if key not in seen:
                    seen.add(key)
                    yield key

    def __len__(self) -> int:
        return sum(1 for _ in self)


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
