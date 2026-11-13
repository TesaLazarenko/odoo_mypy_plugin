"""The files of every addon whose ``_name`` / ``_inherit`` name a model (a text index).

For the keys of ``odoo.values.*`` (:mod:`odoo_typing.registry.annotations`): they come
from every addon on the addons paths, and so does the config data of a file that uses
them (:mod:`odoo_typing.fingerprint`), which mypy asks for on every run. Reading every
source file takes about 0.4 s on Odoo's addons, so the index is kept between runs with
the stamps of the files and directories it read. A run stats them (adding, removing or
renaming a file changes its directory's stamp) and reads only what changed: about
0.08 s for Odoo's 6,000 files when nothing did.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from odoo_typing.addons import Addon
from odoo_typing.records import Record, digest, parse_stamp
from odoo_typing.registry.visible import SKIPPED_DIRS
from odoo_typing.scanner import SourceError, read_source, scan_file_classes
from odoo_typing.stamps import Stamp, stamp

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
    files: dict[str, tuple[Stamp | None, frozenset[str]]]  # name -> (stamp, models)
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
        self._stamps: dict[str, Stamp | None] = {}
        self._directories: dict[str, _Directory] = {}

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
        if any(stamp(path) != entry.stamp for path, entry in self._directories.items()):
            return True
        return any(stamp(path) != recorded for path, recorded in self._stamps.items())

    def stamp(self, path: Path) -> Stamp | None:
        """A file's stamp as the index took it this run (``stat`` again if it did not)."""
        self._index()
        text = str(path)
        return self._stamps[text] if text in self._stamps else stamp(text)

    def _index(self) -> dict[str, tuple[tuple[str, Path], ...]]:
        if self._files is None:
            key = f"class-files-{self.key}.json"
            record = self._keeper.load_record(key) if self._keeper is not None else None
            known = _parse(record)
            found: dict[str, list[tuple[str, Path]]] = {}
            for addon in self._addons:
                for path, models in self._walk(addon, str(addon.path), known):
                    for model in sorted(models):
                        found.setdefault(model, []).append((addon.name, Path(path)))
            self._files = {model: tuple(files) for model, files in found.items()}
            if self._keeper is not None and self._directories != known:
                self._keeper.save_record(key, _encode(self._directories))
        return self._files

    def _walk(
        self, addon: Addon, directory: str, known: dict[str, _Directory]
    ) -> Iterator[tuple[str, frozenset[str]]]:
        """The files under a directory that name models, read again only if changed.

        Paths are strings here: ``pathlib`` would cost more than the ``stat`` calls.
        """
        current = stamp(directory)
        previous = known.get(directory)
        if previous is not None and previous.stamp == current:
            names, subdirs = sorted(previous.files), previous.subdirs
        else:
            names, subdirs = _entries(directory)
        files: dict[str, tuple[Stamp | None, frozenset[str]]] = {}
        for name in names:
            path = os.path.join(directory, name)
            now = self._stamps[path] = stamp(path)
            before = previous.files.get(name) if previous is not None else None
            models = before[1] if before is not None and before[0] == now else _named(addon, path)
            files[name] = (now, models)
            if models:
                yield path, models
        self._directories[directory] = _Directory(current, files, subdirs)
        for name in subdirs:
            yield from self._walk(addon, os.path.join(directory, name), known)


def module_of(addon: Addon, path: Path) -> str | None:
    """The module name of a file of an addon (``odoo.addons.sale.models.sale_order``)."""
    try:
        relative = path.relative_to(addon.path).with_suffix("")
    except ValueError:
        return None
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join([addon.module_prefix, *parts])


def _entries(directory: str) -> tuple[list[str], tuple[str, ...]]:
    """The ``.py`` files and the subdirectories (that may hold models) of a directory."""
    names: list[str] = []
    subdirs: list[str] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_dir() and entry.name not in NO_MODEL_DIRS:
                    subdirs.append(entry.name)
                elif entry.name.endswith(".py") and entry.is_file():
                    names.append(entry.name)
    except OSError:
        pass  # removed meanwhile, or not readable: no files
    return sorted(names), tuple(sorted(subdirs))


def _named(addon: Addon, path: str) -> frozenset[str]:
    """The model names in a file's ``_name`` / ``_inherit`` assignments.

    A text search for string literals; a file with another kind of assignment (a
    constant, an expression) is read by the scanner, which evaluates them as the addon
    scan does (:func:`odoo_typing.scanner.scan_file_classes`).
    """
    try:
        data = read_source(Path(path))
    except SourceError:
        return frozenset()  # a broken symlink, a file that cannot be read
    if b"_name" not in data and b"_inherit" not in data:
        return frozenset()
    names: set[str] = set()
    evaluate = False
    for extended, literal in CLASS_MODELS_PATTERN.findall(data):
        evaluate = evaluate or bool(extended) or not literal
        names.update(model.decode(errors="replace") for model in QUOTED_PATTERN.findall(literal))
    if evaluate:
        for item in scan_file_classes(addon, Path(path)):
            names.update((item.model, *item.parents))
    return frozenset(names)


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
        entries: dict[str, tuple[Stamp | None, frozenset[str]]] = {}
        for name, value in files.items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[1], list):
                models = frozenset(model for model in value[1] if isinstance(model, str))
                entries[name] = (parse_stamp(value[0]), models)
        names = tuple(name for name in subdirs if isinstance(name, str))
        found[path] = _Directory(parse_stamp(recorded), entries, names)
    return found
