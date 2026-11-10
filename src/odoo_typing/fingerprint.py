"""What mypy's incremental cache must know about the registry: per-module config data.

mypy reuses a module's cached types, errors and dependency list as long as the module's
source and its imports are unchanged, and the plugin's ``report_config_data`` for it is
the same as when the cache was written. The plugin's results for a file depend on more:
its addon's manifest and those of its dependency closure, and the model classes every
addon of that closure contributes. So the config data of a module is a digest of
exactly that: for each addon of the closure its name, path, module prefix, ``depends``
and a digest of its scan (classes, models, ``_inherit``, positions, comodels and
referenced model names; not the method bodies, which mypy tracks itself), plus a
digest of the plugin's own source. Files outside any addon get none.

Computing these digests means reading every manifest and parsing the closure, which
would make each warm run as slow as a cold one. So they are stored
(:mod:`odoo_typing.records`) with the ``stat`` stamps of what they were computed from,
and reused while the stamps match:

* ``names-*``: the addon names and module prefixes (stamps: the addons roots, and
  their subdirectories that are not addons yet);
* ``view-<addon>-*``: an addon's digest (stamps: the manifests of its closure, the
  entries of its closure members and missing dependencies in every root);
* ``scan-<addon>-*``: a scan digest (stamps: the scanned files and their directories).

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
    parse_stamps,
    source_digest,
)
from odoo_typing.registry import Registry
from odoo_typing.scanner import AddonScan
from odoo_typing.stamps import Stamp, Stamps, index_roots, stamp

__all__ = ["Fingerprints", "scan_digest"]


def scan_digest(scan: AddonScan) -> str:
    """Digest of what an addon scan found (independent of hash randomization)."""
    errors = [[str(error.path), error.reason] for error in scan.errors]
    return digest([[_canonical(module) for module in scan.modules], errors])


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

    def module_data(self, module: str, path: str) -> str | None:
        """The config data of a module: its addon's digest, if it is in one.

        mypy asks for every module, the standard library's too, so the cheap test comes
        first: a module named after an addon's package (``odoo.addons.sale.x``) is in
        that addon. Otherwise the file is looked up by path, like the plugin's hooks do,
        but only if a directory on its path is named like an addon (addons are named
        after their directory): that needs the whole addon index.
        """
        names = self._addon_names()
        addon_name = _module_addon(module, names)
        if addon_name is None and any(part in names for part in path.split(os.sep)[:-1]):
            addon = self._registry.addons.addon_for_path(path)
            addon_name = addon.name if addon is not None else None
        if addon_name is None:
            return None
        return f"{self.addon_digest(addon_name)}-{self._options}"

    def addon_digest(self, addon_name: str) -> str:
        """Digest of everything the plugin may use for a file of the addon."""
        if addon_name not in self._addon_digests:
            key = f"view-{addon_name}-{self._key}.json"
            record = self._store.load(key)
            value = self._stored_view(record) if record is not None else None
            if value is None:
                value, record = self._compute_view(addon_name)
                self._store.save(key, record)
            self._addon_digests[addon_name] = value
        return self._addon_digests[addon_name]

    # -- addon names ---------------------------------------------------------------------

    def _addon_names(self) -> dict[str, str]:
        """Addon names and their module prefixes."""
        if self._names is None:
            key = f"names-{self._key}.json"
            record = self._store.load(key)
            self._names = _stored_names(record, self._stat) if record is not None else None
        if self._names is None:
            index = self._registry.addons
            self._names = {name: addon.module_prefix for name, addon in index.addons.items()}
            stamps = {
                path: value
                for path, value in self._registry.index_stamps().items()
                if os.path.basename(path) not in MANIFEST_NAMES
            }
            self._store.save(f"names-{self._key}.json", {"stamps": stamps, "addons": self._names})
        return self._names

    # -- addon views ---------------------------------------------------------------------

    def _stored_view(self, record: Record) -> str | None:
        """The digest of a view record whose closure is unchanged on disk."""
        value = record.get("digest")
        members = record.get("members")
        probes = parse_stamps(record.get("probes"))
        if not isinstance(value, str) or not isinstance(members, list) or probes is None:
            return None
        if not all(self._stat(path) == recorded for path, recorded in probes.items()):
            return None
        return value if all(self._member_unchanged(member) for member in members) else None

    def _member_unchanged(self, member: object) -> bool:
        """Whether a closure member's manifest and scan are as recorded."""
        if not (isinstance(member, list) and len(member) == 5):
            return False
        name, manifest, recorded, load_key, scan_key = member
        if not (isinstance(name, str) and isinstance(manifest, str)):
            return False
        current = list(self._stat(manifest) or ()) or None
        if current != recorded or not isinstance(scan_key, str):
            return False
        return self._stored_scan(name, scan_key, load_key) is not None

    def _compute_view(self, addon_name: str) -> tuple[str, Record]:
        """An addon's digest from the full index, and the record that stores it."""
        index = self._registry.addons
        members = sorted(index.closure(addon_name))
        rows: list[object] = []
        parts: list[object] = []
        for name in members:
            addon = index.addons[name]
            scan_key, scan = self._scan_digest(name)
            manifest = find_manifest(addon.path)
            location = str(manifest) if manifest is not None else ""
            load_key = list(index.load_key(name))
            rows.append([name, location, self._stat(location), load_key, scan_key])
            parts.append([*addon_data(addon), scan])
        missing = {dep for name in members for dep in index.addons[name].depends}
        probed = sorted(set(members) | (missing - set(index.addons)))
        probes = {
            str(root / name): self._stat(root / name) for name in probed for root in self._roots
        }
        value = digest([source_digest(), parts])
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
        return all(self._stat(path) == recorded for path, recorded in stamps.items())

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


def _stored_names(record: Record, stat: Callable[[str], Stamp | None]) -> dict[str, str] | None:
    """The addon names of a names record whose roots are unchanged on disk."""
    addons = record.get("addons")
    stamps = parse_stamps(record.get("stamps"))
    if stamps is None or not isinstance(addons, dict):
        return None
    if not all(stat(path) == recorded for path, recorded in stamps.items()):
        return None
    names = {name: prefix for name, prefix in addons.items() if isinstance(prefix, str)}
    return names if len(names) == len(addons) else None


def _canonical(value: object) -> object:
    """A JSON value for a scan result: dataclass fields in order, sets sorted."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = dataclasses.fields(value)
        return [type(value).__name__, *(_canonical(getattr(value, f.name)) for f in fields)]
    if isinstance(value, frozenset | set):
        return sorted((_canonical(item) for item in value), key=json.dumps)
    if isinstance(value, tuple | list):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return sorted(([_canonical(k), _canonical(v)] for k, v in value.items()), key=json.dumps)
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, str | int | float):
        return value
    raise TypeError(f"cannot fingerprint {type(value).__name__}")
