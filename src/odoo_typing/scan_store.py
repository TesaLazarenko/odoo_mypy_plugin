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

from odoo_typing.addons import LoadKey
from odoo_typing.guards import Guard
from odoo_typing.records import RecordStore, digest, parse_stamps, source_digest
from odoo_typing.scanner import AddonScan, Contribution, FieldInfo, ModuleScan, SourceError
from odoo_typing.stamps import Stamps, unchanged
from odoo_typing.xmlids import DataScan, XmlId

__all__ = ["ScanStore", "decode", "encode"]

# The classes a scan is made of, by name (the only ones ``decode`` builds).
CLASSES: Final[Mapping[str, type]] = {
    cls.__name__: cls
    for cls in (AddonScan, ModuleScan, Contribution, FieldInfo, Guard, DataScan, XmlId)
}


class DecodeError(ValueError):
    """A stored scan that does not have the expected shape."""


def encode(value: object) -> object:
    """A JSON value for a scan (or any part of it)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
        return {"$": type(value).__name__, **fields}
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
    """Addon scans in a :class:`~odoo_typing.records.RecordStore`.

    Args:
        store: Where records live (a no-op store keeps nothing).
        index_key: The key of the addons roots (scans differ per module spelling).
    """

    def __init__(self, store: RecordStore, index_key: str) -> None:
        self._store = store
        self._index_key = index_key
        self._version = source_digest()

    def _key(self, addon_name: str) -> str:
        return f"addon-scan-{addon_name}-{digest([self._index_key])[:16]}.json"

    def load(self, addon_name: str, load_key: LoadKey) -> tuple[AddonScan, Stamps] | None:
        """The stored scan of an addon and its stamps, if no file it read changed."""
        record = self._store.load(self._key(addon_name))
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

    def save(self, addon_name: str, load_key: LoadKey, scan: AddonScan, stamps: Stamps) -> None:
        """Keep an addon's scan with the stamps of the files it read."""
        record = {
            "version": self._version,
            "load_key": encode(load_key),
            "stamps": stamps,
            "scan": encode(scan),
        }
        self._store.save(self._key(addon_name), record)
