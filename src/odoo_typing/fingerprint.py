"""What mypy's incremental cache must know about the registry: per-module config data.

mypy reuses a module's cached types, errors and dependency list as long as the module's
source and its imports are unchanged, and the plugin's ``report_config_data`` for it is
the same as when the cache was written. The plugin's results for a file depend on more:
its addon's manifest and those of its dependency closure, and the model classes every
addon of that closure contributes. So the config data of a module is a digest of
exactly that: for each addon of the closure its name, path, module prefix, ``depends``
and a digest of its scan (classes, models, ``_inherit``, positions, comodels and
referenced model names; not the method bodies, which mypy tracks itself), plus a
digest of the plugin's own source. Odoo's own modules (not its addons) get the
configured ``odoo_version``, other files outside any addon get none.

Computing these digests means reading every manifest and parsing the closure, which
would make each warm run as slow as a cold one. So they are stored
(:mod:`odoo_typing.records`) with the ``stat`` stamps of what they were computed from,
and reused while the stamps match:

* ``names-*``: the addon names and module prefixes (stamps: the addons roots, and
  their subdirectories that are not addons yet);
* ``view-<addon>-*``: an addon's digest (stamps: the manifests of its closure, the
  entries of its closure members and missing dependencies in every root);
* ``scan-<addon>-*``: a scan digest (stamps: the scanned files and their directories);
* ``outside-*``: the models a file names that its closure does not define (stamps: the
  file; and the addon's digest), see :meth:`Fingerprints.module_data`.

Each stamp is the one taken before its file was read (the addon index's, the scan's), so
a file saved during a run makes the record outdated rather than silently stale. They are
compared with :func:`odoo_typing.stamps.matches`: a file that could not be read keeps its
records valid until it can be.

A fully cached run reads these small files and calls ``stat``; it parses nothing.
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Callable
from pathlib import Path

from odoo_typing.addons import MANIFEST_NAMES, find_manifest
from odoo_typing.config import Settings
from odoo_typing.records import (
    Record,
    RecordStore,
    addon_data,
    digest,
    index_key,
    parse_stamp,
    parse_stamps,
    source_digest,
)
from odoo_typing.registry import Registry
from odoo_typing.scanner import VALUES, AddonScan
from odoo_typing.stamps import Stamp, Stamps, index_roots, matches, stamp

__all__ = ["Fingerprints", "scan_digest"]

VALUES_PREFIX = f"odoo.{VALUES}."


def scan_digest(scan: AddonScan) -> str:
    """Digest of what an addon scan found (independent of hash randomization)."""
    errors = [[str(error.path), error.reason] for error in scan.errors]
    modules = [_canonical(module) for module in scan.modules]
    return digest([modules, errors, _canonical(scan.data.xml_ids)])


class Fingerprints:
    """Per-module config data for ``report_config_data``, for one registry."""

    def __init__(self, registry: Registry, store: RecordStore, settings: Settings) -> None:
        """Answer from ``registry`` (for these settings), keeping records in ``store``."""
        self._registry = registry
        self._store = store
        self._key = index_key(settings)
        self._options = digest(settings.option_data())[:16]
        self._roots = index_roots(settings.odoo_path, settings.addons_paths)
        self._names: dict[str, str] | None = None
        self._addon_digests: dict[str, str] = {}
        self._scan_digests: dict[str, str] = {}
        self._stamps: dict[str, Stamp | None] = {}
        self._manifests: dict[str, Stamp | None] | None = None  # see _compute_view
        self._roots_digest = ""  # of the addons roots and entries, see _addon_names
        self._odoo_path = settings.odoo_path
        self._version_data = f"odoo_version={settings.odoo_version}"

    def module_data(self, module: str, path: str) -> str | None:
        """The config data of a module: its addon's digest, if it is in one.

        mypy asks for every module, the standard library's too, so the cheap test comes
        first: a module named after an addon's package (``odoo.addons.sale.x``) is in
        that addon. Otherwise the file is looked up by path, like the plugin's hooks do,
        but only if a directory on its path is named like an addon (addons are named
        after their directory): that needs the whole addon index. Odoo's own modules
        get the version (:meth:`_odoo_data`).
        """
        names = self._addon_names()
        addon_name = _module_addon(module, names)
        # A string split, not ``Path.parts``: asked for every module of a run.
        directories = path.split(os.sep)[:-1]  # noqa: PTH206
        if addon_name is None and any(part in names for part in directories):
            addon = self._registry.addons.addon_for_path(path)
            addon_name = addon.name if addon is not None else None
        if addon_name is None:
            return self._odoo_data(module)
        view = self.addon_digest(addon_name)
        data = f"{view}-{self._options}"
        if self._is_core(addon_name):
            return data  # see _outside_definers
        outside = self._outside_definers(addon_name, path, view)
        return f"{data}-{outside}" if outside else data

    def _odoo_data(self, module: str) -> str | None:
        """The config data of a module of Odoo itself (not of an addon): the version.

        The profile of ``odoo_version`` types some of Odoo's own names (such as
        ``odoo.http.request``), so a corrected version, with the same ``odoo_path``, must
        make mypy check them again. The other options are about addons. By name only:
        mypy may find Odoo through an overlay root (a directory of symlinks), in a
        checkout whose subpackages are symlinks, or in any copy of it.
        """
        return self._version_data if module == "odoo" or module.startswith("odoo.") else None

    def _is_core(self, addon_name: str) -> bool:
        """Whether an addon is one of Odoo's own (in ``<odoo_path>/addons``).

        By a stamp of its manifest there, not the addon index, which a warm run never
        loads (a core addon wins over an ``addons_path`` one of the same name).
        """
        directory = self._odoo_path / "addons" / addon_name
        return any(self._stat(directory / name) is not None for name in MANIFEST_NAMES)

    def _outside_definers(self, addon_name: str, path: str, view: str) -> str:
        """Digest for a file that names models its closure does not define.

        Whether such a name is an ``odoo-model`` (nothing defines it) or an
        ``odoo-model-depends`` error (an addon outside the closure does) depends on
        addons the closure digest does not cover. Finding those addons would scan
        addons outside the closure, so the digest takes the file's invisible models
        with the addons roots (the names record's stamps) instead: adding or removing an
        addon anywhere rechecks only the files that name such models. Stored per file,
        valid while the file and the addon's digest ``view`` (its closure) are unchanged;
        empty for the usual file.

        So do an ``odoo.model.*`` annotation that names no model of the closure and
        every ``odoo.values.*`` one: the keys of ``odoo.values.<model>`` are the fields
        every addon gives the model (:mod:`odoo_typing.registry.annotations`), so their
        digest, taken on each run, is part of the file's data
        (:meth:`odoo_typing.registry.Registry.values_digest`); otherwise a field added
        anywhere would give the file checked again other keys than a cached one.

        Not for Odoo's own addons: their files name models of other addons (often
        guarded), their errors are usually silenced (``follow_imports``), and making
        them depend on the list of addons would recheck them whenever one is added.
        """
        key = f"outside-{digest([addon_name, path])[:16]}-{self._key}.json"
        record = self._store.load(key)
        if (
            record is not None
            and self._recorded_matches(path, record.get("stamp"))
            and record.get("view") == view  # the closure decides what is outside it
            and isinstance(models := record.get("models"), list)
        ):
            found = models
        else:
            registry = self._registry
            found = sorted(
                model
                for model in registry.referenced_models(path)
                if not registry.visible(model, addon_name)
            )
            found += sorted(
                f"odoo.{kind}.{alias}"
                for kind, alias in registry.annotation_aliases(path)
                if kind == VALUES or registry.resolve_alias(alias, addon_name).model is None
            )
            read = registry.source_stamp(path)  # as the answers above were read
            stamped = list(read) if read is not None else None
            self._store.save(key, {"stamp": stamped, "view": view, "models": found})
        if not found:
            return ""
        values = [
            self._registry.values_digest(name.removeprefix(VALUES_PREFIX))
            for name in found
            if name.startswith(VALUES_PREFIX)
        ]
        return digest([found, self._roots_digest, values])[:16]

    def addon_digest(self, addon_name: str) -> str:
        """Digest of everything the plugin may use for a file of the addon."""
        if addon_name not in self._addon_digests:
            key = f"view-{addon_name}-{self._key}.json"
            record = self._store.load(key)
            value = self._stored_view(record) if record is not None else None
            if value is None:
                value, computed = self._compute_view(addon_name)
                if computed is not None:
                    self._store.save(key, computed)
            self._addon_digests[addon_name] = value
        return self._addon_digests[addon_name]

    # -- addon names ---------------------------------------------------------------------

    def _addon_names(self) -> dict[str, str]:
        """Addon names and their module prefixes."""
        if self._names is None:
            key = f"names-{self._key}.json"
            record = self._store.load(key)
            self._names = _stored_names(record, self._stat) if record is not None else None
            if self._names is not None and record is not None:
                self._roots_digest = digest(record.get("stamps"))
        if self._names is None:
            index = self._registry.addons
            self._names = {name: addon.module_prefix for name, addon in index.addons.items()}
            stamps = {
                path: value
                for path, value in self._registry.index_stamps().items()
                if Path(path).name not in MANIFEST_NAMES
            }
            self._store.save(f"names-{self._key}.json", {"stamps": stamps, "addons": self._names})
            self._roots_digest = digest(stamps)  # as the stored record's: tuples dump as lists
        return self._names

    # -- addon views ---------------------------------------------------------------------

    def _stored_view(self, record: Record) -> str | None:
        """The digest of a view record whose closure is unchanged on disk."""
        value = record.get("digest")
        members = record.get("members")
        probes = parse_stamps(record.get("probes"))
        if not isinstance(value, str) or not isinstance(members, list) or probes is None:
            return None
        if not self._unchanged(probes):
            return None
        return value if all(self._member_unchanged(member) for member in members) else None

    def _member_unchanged(self, member: object) -> bool:
        """Whether a closure member's manifest and scan are as recorded."""
        if not (isinstance(member, list) and len(member) == 5):
            return False
        name, manifest, recorded, load_key, scan_key = member
        if not (isinstance(name, str) and isinstance(manifest, str)):
            return False
        if not self._recorded_matches(manifest, recorded) or not isinstance(scan_key, str):
            return False
        return self._stored_scan(name, scan_key, load_key) is not None

    def _recorded_matches(self, path: str, recorded: object) -> bool:
        """Whether a stamp of a record (JSON, maybe malformed) still matches a file."""
        value = parse_stamp(recorded)
        if value is None and recorded is not None:
            return False
        return matches(path, value, self._stat)

    def _compute_view(self, addon_name: str) -> tuple[str, Record | None]:
        """An addon's digest from the full index, and the record that stores it.

        The record holds the stamps the index was read with, not new ones: a manifest
        edited since makes it outdated. No record if an addons root changed since (an
        entry added or removed): the probes might then describe other addons than the
        index, and the next run stores the view instead.
        """
        index = self._registry.addons
        taken = self._registry.index_stamps()
        if self._manifests is None:
            self._manifests = _manifest_stamps(taken)
        manifests = self._manifests
        members = sorted(index.closure(addon_name))
        rows: list[object] = []
        parts: list[object] = []
        for name in members:
            addon = index.addons[name]
            scan_key, scan = self._scan_digest(name)
            manifest = find_manifest(addon.path)
            location = str(manifest) if manifest is not None else ""
            load_key = list(index.load_key(name))
            manifest_stamp = manifests[name] if name in manifests else self._stat(location)
            rows.append([name, location, manifest_stamp, load_key, scan_key])
            parts.append([*addon_data(addon), scan])
        missing = {dep for name in members for dep in index.addons[name].depends}
        probed = sorted(set(members) | (missing - set(index.addons)))
        entries = [str(root / name) for name in probed for root in self._roots]
        probes = {entry: taken[entry] if entry in taken else self._stat(entry) for entry in entries}
        value = digest([source_digest(), parts])
        if not all(matches(root, taken.get(str(root))) for root in self._roots):
            return value, None
        return value, {"digest": value, "members": rows, "probes": probes}

    # -- addon scans ---------------------------------------------------------------------

    def _scan_digest(self, addon_name: str) -> tuple[str, str]:
        """An addon's scan record key and scan digest (stored, or scanned now)."""
        index = self._registry.addons
        key = f"scan-{addon_name}-{digest(addon_data(index.addons[addon_name]))[:16]}.json"
        load_key = list(index.load_key(addon_name))
        value = self._stored_scan(addon_name, key, load_key)
        if value is None:
            scan, stamps = self._registry.scan_result(addon_name)
            value = self._scan_digests[addon_name] = scan_digest(scan)
            self._store.save(key, {"load_key": load_key, "stamps": stamps, "digest": value})
        return key, value

    def _stored_scan(self, addon_name: str, key: str, load_key: object) -> str | None:
        """The digest of a scan record whose files are unchanged on disk."""
        if addon_name not in self._scan_digests:
            record = self._store.load(key)
            if record is None or record.get("load_key") != load_key:
                return None
            value = record.get("digest")
            stamps = parse_stamps(record.get("stamps"))
            if not isinstance(value, str) or stamps is None or not self._unchanged(stamps):
                return None
            self._scan_digests[addon_name] = value
        return self._scan_digests[addon_name]

    def _unchanged(self, stamps: Stamps) -> bool:
        return all(matches(path, recorded, self._stat) for path, recorded in stamps.items())

    def _stat(self, path: str | Path) -> Stamp | None:
        """A file's stamp, taken once per run (closures share most of their files)."""
        text = str(path)
        if text not in self._stamps:
            self._stamps[text] = stamp(text) if text else None
        return self._stamps[text]


def _module_addon(module: str, names: dict[str, str]) -> str | None:
    """The addon whose package a module name is in (as :class:`AddonIndex` names them)."""
    head, _, rest = module.partition(".")
    if head == "odoo" and rest.startswith("addons.") and len(rest) > len("addons."):
        name = rest[len("addons.") :].partition(".")[0]
        prefix = f"odoo.addons.{name}"
    else:
        name = prefix = head
    return name if names.get(name) == prefix else None


def _manifest_stamps(stamps: Stamps) -> dict[str, Stamp | None]:
    """Addon name -> the stamp of its manifest, from the addon index's stamps.

    The addon in the first root wins, as in :class:`~odoo_typing.addons.AddonIndex`.
    """
    found: dict[str, Stamp | None] = {}
    for path, value in stamps.items():
        manifest = Path(path)
        if manifest.name in MANIFEST_NAMES:
            found.setdefault(manifest.parent.name, value)
    return found


def _stored_names(record: Record, stat: Callable[[str], Stamp | None]) -> dict[str, str] | None:
    """The addon names of a names record whose roots are unchanged on disk."""
    addons = record.get("addons")
    stamps = parse_stamps(record.get("stamps"))
    if stamps is None or not isinstance(addons, dict):
        return None
    if not all(matches(path, recorded, stat) for path, recorded in stamps.items()):
        return None
    names = {name: prefix for name, prefix in addons.items() if isinstance(prefix, str)}
    return names if len(names) == len(addons) else None


_FINGERPRINT_FIELDS: dict[type, tuple[str, ...] | None] = {}


def _fingerprint_fields(kind: type) -> tuple[str, ...] | None:
    """A dataclass's fields that count for fingerprints, in order; ``None``: no dataclass."""
    if kind not in _FINGERPRINT_FIELDS:
        names = None
        if dataclasses.is_dataclass(kind):  # the class of an instance
            fields = dataclasses.fields(kind)
            names = tuple(item.name for item in fields if item.metadata.get("fingerprint", True))
        _FINGERPRINT_FIELDS[kind] = names
    return _FINGERPRINT_FIELDS[kind]


def _canonical(value: object) -> object:
    """A JSON value for a scan result: dataclass fields in order, sets sorted.

    A field with ``metadata={"fingerprint": False}`` is left out.
    """
    kind = type(value)
    if kind is str or kind is int or kind is bool or kind is float or value is None:
        return value
    names = _fingerprint_fields(kind)
    if names is not None:
        return [kind.__name__, *(_canonical(getattr(value, name)) for name in names)]
    if isinstance(value, frozenset | set):
        return sorted((_canonical(item) for item in value), key=json.dumps)
    if isinstance(value, tuple | list):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return sorted(
            ([_canonical(key), _canonical(item)] for key, item in value.items()), key=json.dumps
        )
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, str | int | float):
        return value
    raise TypeError(f"cannot fingerprint {type(value).__name__}")
