"""The files of every addon whose ``_name`` / ``_inherit`` name a model (a text index).

For the keys of ``odoo.values.*`` (:mod:`odoo_typing.registry.annotations`): they come
from every addon on the addons paths, and so does the config data of a file that uses
them (:mod:`odoo_typing.fingerprint`), which mypy asks for on every run. Reading every
source file takes about 0.4 s on Odoo's addons, so the index is kept between runs with
the stamps of the files and directories it read. A run stats them (adding, removing or
renaming a file changes its directory's stamp) and reads only what changed: about
0.08 s for Odoo's 6,000 files when nothing did. A file that cannot be read, and a
directory that cannot be listed, are kept as :data:`~odoo_typing.stamps.UNREADABLE`:
not read again while they still cannot be, read again once they can
(:func:`~odoo_typing.stamps.matches`).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from odoo_typing.addons import Addon
from odoo_typing.frozen import frozen
from odoo_typing.records import Record, digest, parse_stamp
from odoo_typing.registry.visible import SKIPPED_DIRS
from odoo_typing.scanner import SourceError, read_source, scan_file_classes
from odoo_typing.scanner.native import native_class_walk, native_named
from odoo_typing.stamps import UNREADABLE, Stamp, is_directory, matches, stamp, stat_result

__all__ = ["ClassFileIndex", "RecordKeeper", "module_of"]

# Assignments to ``_name`` / ``_inherit``. Group 2 is the assigned string literal(s), if
# that is what they assign: ``_name = "x"``, ``_name: str = "x"``, ``_inherit = ["x", "y"]``
# (also over lines), ``_name = _description = "x"``. Another one (``_name = MODEL``,
# ``_inherit = _name``, group 1 for ``_inherit += [...]``) needs the scanner.
CLASS_MODELS_PATTERN: Final = re.compile(
    rb"""\b_(?:name|inherit)\s*(?::[^=\n]+)?(\+?)=(?!=)\s*(?:\w+\s*=\s*)*"""
    rb"""(\[[^\]]*\]|\([^)]*\)|[rRuU]?['"][^'"\\\n]+['"])?"""
)
QUOTED_PATTERN: Final = re.compile(rb"""['"]([^'"\\\n]+)['"]""")
# Directories that hold no model classes of the addon (tests define their own models).
NO_MODEL_DIRS: Final = SKIPPED_DIRS | {"tests", "migrations", "upgrades"}
_Identity = tuple[int, int]  # a directory's (st_dev, st_ino)
_File = tuple[Stamp | None, frozenset[str]]  # a file's recorded stamp and the models it names


class RecordKeeper(Protocol):
    """Where the index is kept between runs (:class:`odoo_typing.scan_store.ScanStore`)."""

    def load_record(self, key: str) -> Record | None:
        """A record kept under a key, if any."""
        ...

    def save_record(self, key: str, record: Record) -> None:
        """Keep a record under a key."""
        ...


@dataclass(frozen=True, slots=True)
class _Directory:
    """What the index read in one directory: its stamp, ``.py`` files and subdirectories."""

    stamp: Stamp | None
    files: dict[str, _File]  # name -> (stamp, models)
    subdirs: tuple[str, ...]


class ClassFileIndex:
    """Model name -> the files that name it in ``_name`` / ``_inherit``, in load order.

    Args:
        addons: Every addon on the addons paths, in load order.
        keeper: Where the index is kept between runs (``None``: built each run).
    """

    def __init__(self, addons: Sequence[Addon], keeper: RecordKeeper | None) -> None:
        self._addons = addons
        self._keeper = keeper
        self._files: dict[str, tuple[tuple[str, Path], ...]] | None = None
        self._stamps: dict[str, Stamp | None] = {}  # of the files, as recorded
        self._directories: dict[str, _Directory] = {}
        self._directory_stamps: dict[str, Stamp | None] = {}  # as recorded

    @property
    def key(self) -> str:
        """A digest of the addons (names and directories) the index covers."""
        return digest([[addon.name, str(addon.path)] for addon in self._addons])[:16]

    def files(self, model: str) -> tuple[tuple[str, Path], ...]:
        """The (addon name, file) pairs whose ``_name`` / ``_inherit`` name a model."""
        return self._index().get(model, ())

    def model_names(self) -> frozenset[str]:
        """Every model name some file names."""
        return frozenset(self._index())

    def changed(self) -> bool:
        """Whether a directory or file the index read this run changed on disk since."""
        if self._files is None:
            return False
        if not all(matches(path, recorded) for path, recorded in self._directory_stamps.items()):
            return True
        return not all(matches(path, recorded) for path, recorded in self._stamps.items())

    def stamp(self, path: str | Path) -> Stamp | None:
        """A file's stamp as the index recorded it this run (``stat`` again if it did not)."""
        self._index()
        text = str(path)
        return self._stamps[text] if text in self._stamps else stamp(text)

    def _index(self) -> dict[str, tuple[tuple[str, Path], ...]]:
        if self._files is None:
            key = f"class-files-{self.key}.json"
            record = self._keeper.load_record(key) if self._keeper is not None else None
            native = self._native_index(key, record)
            if native is not None:
                self._files = native
                return native
            known = _parse(record)
            self._files = self._walk_addons(known)
            self._directory_stamps = {path: item.stamp for path, item in self._directories.items()}
            if self._keeper is not None and self._directories != known:
                self._keeper.save_record(key, _encode(self._directories))
        return self._files

    def _native_index(
        self, key: str, record: Record | None
    ) -> dict[str, tuple[tuple[str, Path], ...]] | None:
        """The index read in Rust (record, walk, record kept); ``None`` where Python reads it."""
        native = native_class_walk(
            [(addon.name, str(addon.path), addon.module_prefix) for addon in self._addons],
            record,
            lambda index, path: _named(self._addons[index], path),
        )
        if native is None:
            return None
        found_files, self._stamps, self._directory_stamps, changed = native
        found: dict[str, list[tuple[str, Path]]] = {}
        for index, path, models in found_files:
            name, where = self._addons[index].name, Path(path)
            for model in models:
                found.setdefault(model, []).append((name, where))
        if self._keeper is not None and changed is not None:
            self._keeper.save_record(key, changed)
        return {model: tuple(files) for model, files in found.items()}

    def _walk_addons(self, known: dict[str, _Directory]) -> dict[str, tuple[tuple[str, Path], ...]]:
        """The files of every addon that name each model (addon and path), addons in order."""
        found: dict[str, list[tuple[str, Path]]] = {}
        for addon in self._addons:
            for path, models in self._walk(addon, str(addon.path), known, frozenset()):
                for model in sorted(models):
                    found.setdefault(model, []).append((addon.name, Path(path)))
        return {model: tuple(files) for model, files in found.items()}

    def _walk(
        self,
        addon: Addon,
        directory: str,
        known: dict[str, _Directory],
        ancestors: frozenset[_Identity],
    ) -> Iterator[tuple[str, frozenset[str]]]:
        """The files under a directory that name models, read again only if changed.

        Paths are strings here: ``pathlib`` would cost more than the ``stat`` calls.
        A directory that is one of its own ancestors (``ancestors``) is not walked
        again: a symlink back to an ancestor would otherwise repeat its files up to the
        kernel's limit of 40 links, or never end with two such links. Another alias of
        a directory (``compat -> models``) is walked as a directory of its own.
        """
        current, identity = _directory_stamp(directory)
        if identity in ancestors:
            return
        previous = known.get(directory)
        if previous is not None and matches(directory, previous.stamp, lambda _: current):
            recorded, names, subdirs = previous.stamp, sorted(previous.files), previous.subdirs
        else:
            listed = _entries(directory)  # None: cannot be listed
            recorded = current if listed is not None else UNREADABLE
            names, subdirs = listed if listed is not None else ([], ())
        files: dict[str, _File] = {}
        for name in names:
            path = os.path.join(directory, name)  # noqa: PTH118 (see the docstring)
            before = previous.files.get(name) if previous is not None else None
            files[name] = self._file(addon, path, before)
            if files[name][1]:
                yield path, files[name][1]
        self._directories[directory] = _Directory(recorded, files, subdirs)
        inner = ancestors if identity is None else ancestors | {identity}
        for name in subdirs:
            yield from self._walk(addon, os.path.join(directory, name), known, inner)  # noqa: PTH118

    def _file(self, addon: Addon, path: str, before: _File | None) -> _File:
        """A file's recorded stamp and the models it names, read again only if changed.

        One that cannot be read is kept as :data:`UNREADABLE`, naming no model.
        """
        now = stamp(path)
        if before is not None and matches(path, before[0], lambda _: now):
            found = before
        else:
            models = _named(addon, path)
            found = (now, models) if models is not None else (UNREADABLE, frozen(()))
        self._stamps[path] = found[0]
        return found


def module_of(addon: Addon, path: Path) -> str | None:
    """The module name of a file of an addon (``odoo.addons.sale.models.sale_order``)."""
    try:
        relative = path.relative_to(addon.path).with_suffix("")
    except ValueError:
        return None
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join([addon.module_prefix, *parts])


def _directory_stamp(directory: str) -> tuple[Stamp | None, _Identity | None]:
    """A directory's stamp, and what it is on its device (``None``: cannot be stat'ed)."""
    status = stat_result(directory)
    if status is None:
        return None, None
    return (status.st_mtime_ns, status.st_size), (status.st_dev, status.st_ino)


def _entries(directory: str) -> tuple[list[str], tuple[str, ...]] | None:
    """The ``.py`` files and the subdirectories (that may hold models) of a directory.

    An entry that cannot be stat'ed (a symlink loop) is left out, not the directory.

    Returns:
        The names; ``None`` if the directory cannot be listed (no permission).
    """
    try:
        with os.scandir(directory) as iterator:
            entries = list(iterator)
    except PermissionError:
        return None
    except OSError:
        return [], ()  # removed meanwhile: no files
    subdirs = [
        entry.name for entry in entries if entry.name not in NO_MODEL_DIRS and is_directory(entry)
    ]
    names = [entry.name for entry in entries if entry.name.endswith(".py") and _is_file(entry)]
    return sorted(names), tuple(sorted(subdirs))


def _is_file(entry: os.DirEntry[str]) -> bool:
    """Whether a directory entry is a file or a link to one (not if it cannot tell)."""
    try:
        return entry.is_file()
    except OSError:
        return False


def _named(addon: Addon, path: str) -> frozenset[str] | None:
    """The model names in a file's ``_name`` / ``_inherit`` assignments.

    A text search for string literals; a file with another kind of assignment (a
    constant, an expression) is read by the scanner, which evaluates them as the addon
    scan does (:func:`odoo_typing.scanner.scan_file_classes`).

    Returns:
        The names; ``None`` if the file cannot be read (a broken symlink, no permission).
    """
    native = native_named(addon, path)
    if native is not None:
        return native
    try:
        data = read_source(Path(path))
    except SourceError:
        return None
    if b"_name" not in data and b"_inherit" not in data:
        return frozen(())
    names: set[str] = set()
    evaluate = False
    for extended, literal in CLASS_MODELS_PATTERN.findall(data):
        evaluate = evaluate or bool(extended) or not literal
        names.update(model.decode(errors="replace") for model in QUOTED_PATTERN.findall(literal))
    if not evaluate:
        return frozen(names)
    try:
        classes = scan_file_classes(addon, Path(path))
    except SourceError:
        return None
    names.update(name for item in classes for name in (item.model, *item.parents))
    return frozen(names)


def _encode(directories: dict[str, _Directory]) -> Record:
    rows = {
        path: [
            list(entry.stamp) if entry.stamp else None,
            {
                name: [list(file_stamp) if file_stamp else None, sorted(models)]
                for name, (file_stamp, models) in entry.files.items()
            },
            list(entry.subdirs),
        ]
        for path, entry in directories.items()
    }
    return {"directories": rows}


def _parse(record: Record | None) -> dict[str, _Directory]:
    """The directories of a kept index (what does not have the expected shape is dropped)."""
    rows = record.get("directories") if record is not None else None
    if not isinstance(rows, dict):
        return {}
    found: dict[str, _Directory] = {}
    for path, row in rows.items():
        if not (isinstance(row, list) and len(row) == 3):
            continue
        recorded, files, subdirs = row
        if not isinstance(files, dict) or not isinstance(subdirs, list):
            continue
        entries: dict[str, _File] = {}
        for name, value in files.items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[1], list):
                models = frozen(model for model in value[1] if isinstance(model, str))
                entries[name] = (parse_stamp(value[0]), models)
        names = tuple(name for name in subdirs if isinstance(name, str))
        found[path] = _Directory(parse_stamp(recorded), entries, names)
    return found
