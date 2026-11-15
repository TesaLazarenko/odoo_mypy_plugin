"""JSON records the plugin keeps next to mypy's cache, and the stored addon index.

Records live in ``<cache_dir>/odoo_typing/<interpreter>`` (``cpython-312``): the scans
parse sources with the running interpreter's grammar, so a file one Python version
cannot parse may be fine for another. Each record says which version of the plugin
wrote it and carries the ``stat`` stamps (:mod:`odoo_typing.stamps`) of the files it was
computed from; it is used only while both still match.

This is a boundary module: records are untrusted JSON, validated when read.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from functools import cache
from pathlib import Path
from typing import TypeGuard

from odoo_typing.addons import Addon, AddonIndex
from odoo_typing.config import Settings
from odoo_typing.stamps import Stamp, Stamps, index_roots, read_index, unchanged

__all__ = [
    "Record",
    "RecordStore",
    "addon_data",
    "digest",
    "index_key",
    "interpreter_tag",
    "is_str_list",
    "load_index",
    "package_source_hash",
    "parse_stamp",
    "parse_stamps",
    "source_digest",
]

STORE_DIR = "odoo_typing"
FORMAT = 1
Record = dict[str, object]


class RecordStore:
    """JSON records in a directory (``<cache_dir>/odoo_typing/<interpreter>``), or nowhere.

    Writes are atomic (a temporary file renamed over the record), so concurrent mypy
    runs on one cache never read half a record. Unreadable records count as missing.
    """

    def __init__(self, directory: Path | None) -> None:
        """Use ``directory`` for records; ``None`` keeps nothing between runs."""
        self._directory = directory

    @classmethod
    def for_cache_dir(cls, cache_dir: str) -> RecordStore:
        """The store next to mypy's cache, or none if mypy writes no cache."""
        if not cache_dir or cache_dir == os.devnull:
            return cls(None)
        return cls(Path(cache_dir) / STORE_DIR / interpreter_tag())

    @classmethod
    def shared(cls, directory: Path | None) -> RecordStore | None:
        """The store of addon scans for every project (``scan_cache_dir``), if one is set.

        Like the one in mypy's cache, in a subdirectory for the running interpreter.
        """
        return cls(directory / interpreter_tag()) if directory is not None else None

    def load(self, key: str) -> Record | None:
        """Return a record written by this plugin version, or ``None``."""
        if self._directory is None:
            return None
        try:
            data = json.loads((self._directory / key).read_bytes())
        except (OSError, ValueError, RecursionError):  # RecursionError: nested too deeply
            return None
        if not isinstance(data, dict) or data.get("version") != source_digest():
            return None
        return data

    def save(self, key: str, record: Record) -> bool:
        """Write a record; whether it was written (a directory that cannot be is ignored)."""
        if self._directory is None:
            return False
        data = {**record, "version": source_digest()}
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            temporary = self._directory / f".{key}.{os.getpid()}.tmp"
            temporary.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            temporary.replace(self._directory / key)
        except OSError:
            return False
        return True


def interpreter_tag() -> str:
    """The running interpreter, as it names its bytecode files (``cpython-312``).

    The directory of the records it writes: the scans depend on its grammar.
    """
    implementation = sys.implementation
    version = f"{sys.version_info[0]}{sys.version_info[1]}"
    return implementation.cache_tag or f"{implementation.name}-{version}"


@cache
def source_digest() -> str:
    """Digest of the plugin's own source: a new plugin version invalidates everything.

    Every module of the package counts, in its subpackages too (``hooks``, ``registry``,
    ``scanner``), each with its path, so moving code between modules counts as well.
    """
    return digest([FORMAT, package_source_hash(Path(__file__).parent)])


def package_source_hash(package: Path) -> str:
    """SHA-256 of a package's Python files and their paths inside it.

    A file that cannot be read counts by its path alone, with a marker: it runs at the
    plugin's import (``__version__``), and an editable checkout may hold a dangling
    link named like a module (``.#models.py``, an Emacs lock), which Python never imports.
    """
    hashed = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        hashed.update(path.relative_to(package).as_posix().encode() + b"\0")
        try:
            hashed.update(path.read_bytes() + b"\0")
        except OSError:
            hashed.update(b"\0unreadable\0")
    return hashed.hexdigest()


def digest(value: object) -> str:
    """SHA-256 of a JSON value."""
    text = json.dumps(value, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def index_key(settings: Settings) -> str:
    """A key that differs for each list of addons roots and module spelling."""
    roots = [str(root) for root in index_roots(settings.odoo_path, settings.addons_paths)]
    return digest([roots, settings.custom_prefix])[:16]


def load_index(
    settings: Settings,
    store: RecordStore,
    memory: dict[str, tuple[list[Addon], Stamps]] | None = None,
) -> tuple[AddonIndex, Stamps]:
    """Build the addon index, from the stored one if no root, entry or manifest changed.

    Args:
        settings: Plugin settings (the addons roots).
        store: Where the index of the previous run is kept.
        memory: The indexes this process built, by key: read first. The daemon keeps no
            mypy cache, and starts a new registry after each edit of a scanned file,
            which read the 659 manifests of Odoo again (0.1 s of a 0.22 s update).

    Returns:
        The index and the stamps of the files it describes.
    """
    key = f"index-{index_key(settings)}.json"
    known = memory.get(key) if memory is not None else None
    if known is not None and not unchanged(known[1]):
        known = None
    if known is None:
        record = store.load(key)
        known = _stored_index(record) if record is not None else None
    if known is not None:
        addons, stamps = known
        index = AddonIndex(
            settings.odoo_path, settings.addons_paths, settings.custom_prefix, known=addons
        )
    else:
        index, stamps = read_index(
            settings.odoo_path, settings.addons_paths, settings.custom_prefix
        )
        addons = list(index.addons.values())
        store.save(key, {"stamps": stamps, "addons": [addon_data(addon) for addon in addons]})
    if memory is not None:
        memory[key] = (addons, stamps)
    return index, stamps


def addon_data(addon: Addon) -> list[object]:
    """An addon as JSON: name, path, module prefix, depends."""
    return [addon.name, str(addon.path), addon.module_prefix, list(addon.depends)]


def _stored_index(record: Record) -> tuple[list[Addon], Stamps] | None:
    """The addons of an index record that still describes the roots on disk."""
    stamps = parse_stamps(record.get("stamps"))
    addons = _parse_addons(record.get("addons"))
    if stamps is None or addons is None or not unchanged(stamps):
        return None
    return addons, stamps


def _parse_addons(value: object) -> list[Addon] | None:
    if not isinstance(value, list):
        return None
    addons = []
    for item in value:
        if not (isinstance(item, list) and len(item) == 4):
            return None
        name, path, prefix, depends = item
        texts = isinstance(name, str) and isinstance(path, str) and isinstance(prefix, str)
        if not texts or not is_str_list(depends):
            return None
        addons.append(
            Addon(name=name, path=Path(path), module_prefix=prefix, depends=tuple(depends))
        )
    return addons


def is_str_list(value: object) -> TypeGuard[list[str]]:
    """Whether a JSON value is a list of strings."""
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def parse_stamps(value: object) -> Stamps | None:
    """Stamps from JSON (``{path: [mtime_ns, size] | null}``), or ``None`` if malformed."""
    if not isinstance(value, dict):
        return None
    stamps: Stamps = {}
    for path, item in value.items():
        if not isinstance(path, str) or (item is not None and parse_stamp(item) is None):
            return None
        stamps[path] = parse_stamp(item)
    return stamps


def parse_stamp(value: object) -> Stamp | None:
    """A stamp from JSON (``[mtime_ns, size]``); ``None`` for ``null`` or anything else."""
    if isinstance(value, list) and len(value) == 2 and all(type(number) is int for number in value):
        return (value[0], value[1])
    return None
