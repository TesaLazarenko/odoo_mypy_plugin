"""Addon scans kept on disk between runs (roadmap [21]).

Scanning an addon parses every file its ``__init__.py`` imports. In an incremental run
after an edit, the registry needs the scans of the edited file's whole addon closure,
yet only the edited addon changed: the others are read back from the record store,
valid while every file they read (their stamps), their load order and the plugin's own
source are unchanged.

A scan is stored whole, per addon, not per file: a file's scan depends on the
constants of other files of the addon (``from .consts import STATES``). The JSON form
keeps the exact Python types: tuples, frozensets and paths are tagged, dataclasses carry
their class name (only the scan's own classes are rebuilt).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from odoo_typing.addons import Addon, LoadKey
from odoo_typing.guards import Guard
from odoo_typing.records import RecordStore, digest, is_str_list, parse_stamps, source_digest
from odoo_typing.scanner import AddonScan, Contribution, FieldInfo, ModuleScan, SourceError
from odoo_typing.stamps import Stamp, Stamps, unchanged
from odoo_typing.xmlids import DataScan, XmlId

__all__ = ["ScanStore", "dataclass_fields", "decode", "encode"]

# The classes a scan is made of, by name (the only ones ``decode`` builds).
CLASSES: Final[Mapping[str, type]] = {
    cls.__name__: cls
    for cls in (AddonScan, ModuleScan, Contribution, FieldInfo, Guard, DataScan, XmlId)
}


_FIELDS: dict[type, tuple[str, ...] | None] = {}


def dataclass_fields(kind: type) -> tuple[str, ...] | None:
    """The field names of a dataclass in order, ``None`` for another class (cached)."""
    if kind not in _FIELDS:
        is_dataclass = dataclasses.is_dataclass(kind)  # the class of an instance
        _FIELDS[kind] = tuple(f.name for f in dataclasses.fields(kind)) if is_dataclass else None
    return _FIELDS[kind]


class DecodeError(ValueError):
    """A stored scan that does not have the expected shape."""


# Leaves stored as they are (exact classes: the common case is checked first).
PLAIN: Final = frozenset({str, int, bool, float, type(None)})


def encode(value: object) -> object:
    """A JSON value for a scan (or any part of it)."""
    kind = type(value)
    if kind in PLAIN:
        return value
    names = dataclass_fields(kind)
    if names is not None:
        fields = {name: encode(getattr(value, name)) for name in names}
        return {"$": kind.__name__, **fields}
    if isinstance(value, SourceError):
        return {"$error": [str(value.path), value.reason]}
    if isinstance(value, tuple):
        return {"$tuple": [encode(item) for item in value]}
    if isinstance(value, frozenset):
        return {"$set": sorted((encode(item) for item in value), key=repr)}
    if isinstance(value, Path):
        return {"$path": str(value)}
    if isinstance(value, list):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        return {"$dict": [[encode(key), encode(item)] for key, item in value.items()]}
    if value is None or isinstance(value, bool | int | float | str):
        return value
    raise TypeError(f"cannot store {type(value).__name__} in a scan")


def decode(value: object) -> object:
    """The scan value :func:`encode` gave ``value`` for.

    Raises:
        DecodeError: If the value was not made by :func:`encode` (an older format).
    """
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "$" in value:
        cls = CLASSES.get(str(value["$"]))
        if cls is None:
            raise DecodeError(f"unknown class {value['$']!r}")
        try:
            return cls(**{key: decode(item) for key, item in value.items() if key != "$"})
        except TypeError as error:
            raise DecodeError(str(error)) from error
    if len(value) != 1:
        raise DecodeError(f"unknown value {value!r}")
    tag, payload = next(iter(value.items()))
    return _decode_tagged(tag, payload)


def _decode_tagged(tag: str, payload: object) -> object:
    if tag == "$path" and isinstance(payload, str):
        return Path(payload)
    if not isinstance(payload, list):
        raise DecodeError(f"unknown value {tag}: {payload!r}")
    if tag == "$tuple":
        return tuple(decode(item) for item in payload)
    if tag == "$set":
        return frozenset(decode(item) for item in payload)
    if tag == "$dict":
        return {decode(pair[0]): decode(pair[1]) for pair in payload if isinstance(pair, list)}
    if tag == "$error" and len(payload) == 2:
        return SourceError(Path(str(payload[0])), str(payload[1]))
    raise DecodeError(f"unknown tag {tag!r}")


class ScanStore:
    """Addon scans in :class:`~odoo_typing.records.RecordStore` s.

    A scan depends only on its addon: the files it read (their stamps), the module
    prefix mypy names the package with, and the addon's place in the load order. Keyed by
    the addon's directory and prefix, it is the same for every project and mypy cache that
    use that addon, so a shared store (``scan_cache_dir``) spares a cold run (a new mypy
    cache, another worktree or project on the same Odoo) the scans of Odoo's addons.

    Args:
        store: The store in mypy's cache (a no-op store keeps nothing).
        shared: The shared store, read first and written instead of ``store``; ``store``
            is written when it cannot be (a read-only home).
    """

    def __init__(self, store: RecordStore, shared: RecordStore | None = None) -> None:
        self._stores = (store,) if shared is None else (shared, store)
        self._version = source_digest()

    @staticmethod
    def _key(addon: Addon) -> str:
        where = digest([str(addon.path), addon.module_prefix])[:16]
        return f"addon-scan-{addon.name}-{where}.json"

    def load(self, addon: Addon, load_key: LoadKey) -> tuple[AddonScan, Stamps] | None:
        """The stored scan of an addon and its stamps, if no file it read changed."""
        for store in self._stores:
            found = self._load(store, addon, load_key)
            if found is not None:
                return found
        return None

    def _load(
        self, store: RecordStore, addon: Addon, load_key: LoadKey
    ) -> tuple[AddonScan, Stamps] | None:
        record = store.load(self._key(addon))
        if record is None or record.get("version") != self._version:
            return None
        stamps = parse_stamps(record.get("stamps"))
        if record.get("load_key") != encode(load_key) or stamps is None or not unchanged(stamps):
            return None
        try:
            scan = decode(record.get("scan"))
        except DecodeError:
            return None
        return (scan, stamps) if isinstance(scan, AddonScan) else None

    def save(self, addon: Addon, load_key: LoadKey, scan: AddonScan, stamps: Stamps) -> None:
        """Keep an addon's scan with the stamps of the files it read (first writable store)."""
        record = {
            "version": self._version,
            "load_key": encode(load_key),
            "stamps": stamps,
            "scan": encode(scan),
        }
        self._save(self._key(addon), record)

    def load_file(
        self, path: Path, current: Stamp | None
    ) -> tuple[frozenset[str], tuple[Guard, ...]] | None:
        """What a file no addon scan reaches refers to, if stored for its current stamp.

        Such files (tests, which ``__init__.py`` does not import) are parsed on their own
        (:meth:`odoo_typing.registry.scans.ScannedAddons.referenced_models`).
        """
        for store in self._stores:
            record = store.load(self._file_key(path))
            if record is None or record.get("stamp") != (list(current) if current else None):
                continue
            models, guards = record.get("models"), record.get("guards")
            try:
                found = decode(guards)
            except DecodeError:
                continue
            if is_str_list(models) and isinstance(found, tuple):
                return frozenset(models), tuple(item for item in found if isinstance(item, Guard))
        return None

    def save_file(
        self, path: Path, current: Stamp | None, models: frozenset[str], guards: tuple[Guard, ...]
    ) -> None:
        """Keep what a file no addon scan reaches refers to, with its stamp."""
        record = {
            "stamp": list(current) if current else None,
            "models": sorted(models),
            "guards": encode(guards),
        }
        self._save(self._file_key(path), record)

    @staticmethod
    def _file_key(path: Path) -> str:
        return f"file-{path.stem}-{digest([str(path)])[:16]}.json"

    def _save(self, key: str, record: dict[str, object]) -> None:
        for store in self._stores:  # the first one that can be written
            if store.save(key, record):
                return
