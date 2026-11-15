"""Cheap ``stat`` stamps of the files the registry reads.

A stamp is a file's ``(st_mtime_ns, st_size)``, or ``None`` if it does not exist. The
registry records the stamps of what it reads: the addons roots, their entries and
manifests (the addon index), and the files of each scanned addon with their
directories. Comparing the recorded stamps with the files on disk tells whether
anything the registry knows may be outdated, without reading a single file.

Directories are stamped too: creating, deleting or renaming an entry changes the
mtime of its directory. That covers a new addon, a new manifest, and a module the
scan looked for but did not find.

A stamp is taken right before its file is read, never after: a file saved while it is
being read then has a newer stamp than the recorded one, and is read again next time.

A file that cannot be read (no permission) is recorded as :data:`UNREADABLE`, which
matches while it still cannot be read (:func:`matches`): what was computed without it
is reused until a permission fix, which changes neither its mtime nor its size, and is
computed again then.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from stat import S_ISDIR
from typing import Final

from odoo_typing.addons import AddonIndex, may_be_addon, search_manifest

__all__ = [
    "UNREADABLE",
    "Stamp",
    "Stamps",
    "index_roots",
    "index_stamps",
    "is_directory",
    "matches",
    "read_index",
    "stamp",
    "stat_result",
    "unchanged",
]

Stamp = tuple[int, int]
Stamps = dict[str, Stamp | None]

# The recorded stamp of a file that could not be read, or of a directory that could not
# be listed or searched. No file has it: see matches().
UNREADABLE: Final[Stamp] = (-1, -1)


def stamp(path: str | Path) -> Stamp | None:
    """Return a file's ``(mtime_ns, size)``, or ``None`` if it cannot be stat'ed."""
    result = stat_result(path)
    return None if result is None else (result.st_mtime_ns, result.st_size)


def stat_result(path: str | Path) -> os.stat_result | None:
    """Return a file's ``os.stat``, or ``None`` if it cannot be stat'ed."""
    try:
        return os.stat(path)  # noqa: PTH116 (paths are often strings: no Path to build)
    except (OSError, ValueError):  # ValueError: a NUL in the path
        return None


def matches(
    path: str | Path, recorded: Stamp | None, stat: Callable[[str], Stamp | None] = stamp
) -> bool:
    """Whether a recorded stamp still describes a file on disk.

    :data:`UNREADABLE` matches while the file still cannot be read (a directory: listed
    and searched), not once it can or is gone: what was computed without it stays valid
    until a permission fix (chmod, chown), which changes neither its mtime nor its size.
    Any other stamp matches the file's current one.

    Args:
        path: The file.
        recorded: Its recorded stamp.
        stat: Takes the current stamp of a path (:func:`stamp`, or a cache of it).
    """
    if recorded == UNREADABLE:
        return _unreadable(path)
    return stat(str(path)) == recorded


def _unreadable(path: str | Path) -> bool:
    """Whether a file exists but cannot be read, or a directory listed and searched."""
    try:
        status = os.stat(path)  # noqa: PTH116 (paths are often strings: no Path to build)
    except PermissionError:  # in a directory that cannot be searched
        return True
    except (OSError, ValueError):  # gone
        return False
    mode = os.R_OK | os.X_OK if S_ISDIR(status.st_mode) else os.R_OK
    return not os.access(path, mode)


def unchanged(stamps: Mapping[str, Stamp | None]) -> bool:
    """Whether every recorded stamp still matches the file on disk (:func:`matches`)."""
    return all(matches(path, recorded) for path, recorded in stamps.items())


def index_roots(odoo_path: Path, addons_paths: Iterable[Path]) -> list[Path]:
    """The directories :class:`~odoo_typing.addons.AddonIndex` lists, in its order."""
    return [odoo_path / "addons", *addons_paths]


def index_stamps(roots: Iterable[Path]) -> Stamps:
    """Stamps of the addons roots and their entries: an addon's manifest, other dirs.

    A root's own stamp changes when an entry is added, removed or renamed; a
    manifest's when it is edited or deleted; a plain subdirectory's when a manifest
    is created in it. A root that cannot be listed, a subdirectory that cannot be
    searched for a manifest and a manifest that cannot be read are :data:`UNREADABLE`.
    Dot directories (``.git``) and ``__pycache__`` are no addons and are left out.
    """
    stamps: Stamps = {}
    for root in roots:
        current = stamp(root)
        children = _subdirectories(root)
        stamps[str(root)] = UNREADABLE if children is None else current
        for child in children or ():
            manifest, searched = search_manifest(child)
            if manifest is not None:
                readable = os.access(manifest, os.R_OK)
                stamps[str(manifest)] = stamp(manifest) if readable else UNREADABLE
            else:
                stamps[str(child)] = stamp(child) if searched else UNREADABLE
    return stamps


def _subdirectories(root: Path) -> list[Path] | None:
    """The subdirectories of an addons root that may be addons; ``None``: cannot be listed.

    An entry that cannot be told a directory for lack of permission (a link into a
    directory that cannot be searched) counts: its manifest cannot be looked up.
    """
    try:
        with os.scandir(root) as iterator:
            entries = [entry for entry in iterator if may_be_addon(entry.name)]
    except PermissionError:
        return None
    except OSError:  # missing
        return []
    return [Path(entry.path) for entry in entries if _maybe_directory(entry)]


def _maybe_directory(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir()
    except PermissionError:
        return True
    except OSError:  # a symlink loop
        return False


def is_directory(entry: os.DirEntry[str]) -> bool:
    """Whether a directory entry is a directory or a link to one.

    ``False`` for one that cannot be stat'ed (a symlink loop, a link into a directory
    that cannot be searched), which skips that entry rather than its whole directory.
    """
    try:
        return entry.is_dir()
    except OSError:
        return False


def read_index(
    odoo_path: Path, addons_paths: tuple[Path, ...], custom_prefix: str = "{}"
) -> tuple[AddonIndex, Stamps]:
    """Build the addon index from disk, with the stamps of what it read.

    The stamps are taken first, so a file changed while the index is built makes the
    stamps outdated rather than the index silently stale.
    """
    stamps = index_stamps(index_roots(odoo_path, addons_paths))
    return AddonIndex(odoo_path, addons_paths, custom_prefix), stamps
