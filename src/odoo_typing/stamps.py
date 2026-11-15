"""Cheap ``stat`` stamps of the files the registry reads.

A stamp is a file's ``(st_mtime_ns, st_size)``, or ``None`` if it does not exist. The
registry records the stamps of what it reads: the addons roots, their entries and
manifests (the addon index), and the files of each scanned addon with their
directories. Comparing the recorded stamps with the files on disk tells whether
anything the registry knows may be outdated, without reading a single file.

Directories are stamped too: creating, deleting or renaming an entry changes the
mtime of its directory. That covers a new addon, a new manifest, and a module the
scan looked for but did not find.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path

from odoo_typing.addons import Addon, AddonIndex, find_manifest
from odoo_typing.scanner import AddonScan

__all__ = [
    "Stamp",
    "Stamps",
    "index_roots",
    "index_stamps",
    "read_index",
    "scan_stamps",
    "stamp",
    "unchanged",
]

Stamp = tuple[int, int]
Stamps = dict[str, Stamp | None]


def stamp(path: str | Path) -> Stamp | None:
    """Return a file's ``(mtime_ns, size)``, or ``None`` if it cannot be stat'ed."""
    try:
        result = os.stat(path)  # noqa: PTH116 (paths are often strings: no Path to build)
    except OSError:
        return None
    return (result.st_mtime_ns, result.st_size)


def unchanged(stamps: Mapping[str, Stamp | None]) -> bool:
    """Whether every recorded stamp still matches the file on disk."""
    return all(stamp(path) == recorded for path, recorded in stamps.items())


def index_roots(odoo_path: Path, addons_paths: Iterable[Path]) -> list[Path]:
    """The directories :class:`~odoo_typing.addons.AddonIndex` lists, in its order."""
    return [odoo_path / "addons", *addons_paths]


def index_stamps(roots: Iterable[Path]) -> Stamps:
    """Stamps of the addons roots and their entries: an addon's manifest, other dirs.

    A root's own stamp changes when an entry is added, removed or renamed; a
    manifest's when it is edited or deleted; a plain subdirectory's when a manifest
    is created in it.
    """
    stamps: Stamps = {}
    for root in roots:
        stamps[str(root)] = stamp(root)
        for child in _subdirectories(root):
            manifest = find_manifest(child)
            entry = child if manifest is None else manifest
            stamps[str(entry)] = stamp(entry)
    return stamps


def _subdirectories(root: Path) -> list[Path]:
    try:
        with os.scandir(root) as entries:
            return [Path(entry.path) for entry in entries if entry.is_dir()]
    except OSError:
        return []


def read_index(
    odoo_path: Path, addons_paths: tuple[Path, ...], custom_prefix: str = "{}"
) -> tuple[AddonIndex, Stamps]:
    """Build the addon index from disk, with the stamps of what it read.

    The stamps are taken first, so a file changed while the index is built makes the
    stamps outdated rather than the index silently stale.
    """
    stamps = index_stamps(index_roots(odoo_path, addons_paths))
    return AddonIndex(odoo_path, addons_paths, custom_prefix), stamps


def scan_stamps(addon: Addon, scan: AddonScan) -> Stamps:
    """Stamps of the files an addon scan read (data files too), of their directories."""
    files = [module.path for module in scan.modules] + [error.path for error in scan.errors]
    directories = {addon.path, *(path.parent for path in files)}
    data = list(scan.data.files)  # listed by the manifest, whose stamp the index has
    return {str(path): stamp(path) for path in [*files, *sorted(directories), *data]}
