"""Field classes of modules that no addon scan describes, read from their source.

A field class may come from outside the addons: Odoo 19 defines its own in
``odoo.orm.fields_*`` (``odoo.fields`` only imports them), a library may define one, and
a module may only pass a class on (``from requests import Session``). When a field of an
addon is traced to such a name (:meth:`odoo_typing.registry.scans.ScannedAddons.link_classes`),
the module is found and parsed here, without importing it: Odoo's own under the configured
``odoo`` package, any other on the interpreter's search path.

Odoo's field classes are the names ``odoo.fields`` has whose classes derive from the one
it calls ``Field``, wherever they are defined: ``odoo.orm.fields_numeric.Integer`` is
``Integer``. A class ``odoo.fields`` has that is no field class (``Command``, ``Domain``)
is known as such (:meth:`SourceClasses.is_odoo_field`).

What a module says is kept between runs with its file's stamp (in the scan store, a
record per ``odoo`` package), so a later run parses only the modules that changed; a
long-lived registry (the daemon) starts over when one of them does
(:meth:`SourceClasses.inputs_changed`).
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.machinery import ModuleSpec, PathFinder
from pathlib import Path
from typing import Final, Protocol

from odoo_typing.records import Record
from odoo_typing.scanner import FieldClass, SourceError, describe_source, parse_source
from odoo_typing.stamps import Stamps, stamp, unchanged

__all__ = ["SourceClasses"]

ODOO: Final = "odoo"
ODOO_PACKAGE: Final = "odoo."
ODOO_FIELDS: Final = "odoo.fields"
FIELD: Final = "Field"
KEPT_LENGTH: Final = 5  # path, stamp, classes, origins, names


@dataclass(frozen=True, slots=True)
class _Source:
    """What a parsed module says.

    Attributes:
        classes: Its classes (and factories) that may be field classes, by name.
        origins: The dotted names that the names it only passes on are bound to.
        names: Every name it binds at its top level.
    """

    classes: dict[str, FieldClass]
    origins: dict[str, str]
    names: frozenset[str] = frozenset()


NO_SOURCE: Final = _Source({}, {})


class RecordKeeper(Protocol):
    """Where records are kept between runs (:class:`odoo_typing.scan_store.ScanStore`)."""

    def load_record(self, key: str) -> Record | None:
        """A record kept under a key, if any."""

    def save_record(self, key: str, record: Record) -> None:
        """Keep a record under a key."""


class SourceClasses:
    """The classes of modules outside the addon scans, by dotted name (read on demand)."""

    def __init__(
        self,
        odoo_path: Path,
        addon_module: Callable[[str], Path | None],
        keeper: RecordKeeper | None = None,
    ) -> None:
        """Create a reader.

        Args:
            odoo_path: The ``odoo`` package directory.
            addon_module: The file of an addon's module by its dotted name, ``None``
                for a name that is no addon module.
            keeper: Keeps what the modules say between runs; they are parsed in every
                run without it.
        """
        self._odoo_path = odoo_path
        self._addon_module = addon_module
        self._keeper = keeper
        digest = hashlib.sha256(str(odoo_path).encode()).hexdigest()[:16]
        self._key = f"sources-{digest}.json"
        self._kept: dict[str, object] | None = None  # the record's modules, once loaded
        self._changed = False
        self._stamps: Stamps = {}  # of the files read so far
        self._sources: dict[str, _Source] = {}
        self._bound: dict[str, str] | None = None  # what odoo.fields binds -> its name there
        self._is_field: dict[str, bool] = {}  # names of odoo.fields, once asked

    def describe(self, origin: str) -> FieldClass | None:
        """What a dotted name outside the addons is: a class, or the class it passes on.

        One of Odoo's field classes, however it is reached, is described as the name
        ``odoo.fields`` has it under.
        """
        name = self._odoo_name(origin)
        if name is not None:
            return FieldClass(name, (f"{ODOO_FIELDS}.{name}",))
        module, _, attribute = origin.rpartition(".")
        return self._source(module).classes.get(attribute) or self.passed_on(origin)

    def passed_on(self, origin: str) -> FieldClass | None:
        """The class a module only passes on under a name (an import, an alias)."""
        module, _, attribute = origin.rpartition(".")
        bound = self._source(module).origins.get(attribute)
        return FieldClass(attribute, (bound,)) if bound and bound != origin else None

    def is_odoo_field(self, name: str) -> bool:
        """Whether a name of ``odoo.fields`` is a field class (not ``Command``, ``Domain``).

        A name ``odoo.fields`` does not have is taken for one: an addon may add it at run
        time (``fields.Serialized`` of ``base_sparse_field``).
        """
        known = self._is_field.get(name)
        if known is None:
            field = self._defined(f"{ODOO_FIELDS}.{FIELD}")
            defined = self._defined(f"{ODOO_FIELDS}.{name}")
            has = name in self._source(ODOO_FIELDS).names
            known = self._is_field[name] = not has or self._derives(defined, field, frozenset())
        return known

    def _odoo_name(self, origin: str) -> str | None:
        """The name ``odoo.fields`` has one of Odoo's field classes under."""
        if not origin.startswith(ODOO_PACKAGE):
            return None
        if self._bound is None:
            source = self._source(ODOO_FIELDS)
            names = [name for name in source.names if name[:1].isupper()]
            # What each name is bound to: an import of the defining module, or a class.
            self._bound = {
                source.origins.get(name, f"{ODOO_FIELDS}.{name}"): name for name in names
            }
        name = self._bound.get(origin) or self._bound.get(self._defined(origin))
        return name if name is not None and self.is_odoo_field(name) else None

    def _derives(self, origin: str, base: str, seen: frozenset[str]) -> bool:
        """Whether the class of a dotted name of Odoo's is, or derives from, another one."""
        if origin == base:
            return True
        if origin in seen or not origin.startswith(ODOO_PACKAGE):
            return False
        module, _, attribute = origin.rpartition(".")
        described = self._source(module).classes.get(attribute)
        return described is not None and any(
            self._derives(self._defined(item), base, seen | {origin}) for item in described.bases
        )

    def _defined(self, origin: str) -> str:
        """The dotted name of Odoo's module that defines what a name is bound to."""
        seen = {origin}
        while origin.startswith(ODOO_PACKAGE):
            module, _, attribute = origin.rpartition(".")
            source = self._source(module)
            bound = source.origins.get(attribute)
            if bound is None or bound in seen or attribute in source.classes:
                break
            seen.add(bound)
            origin = bound
        return origin

    def _source(self, module: str) -> _Source:
        """A module's classes and bindings; none for a module that is not found."""
        found = self._sources.get(module)
        if found is None:
            found = self._sources[module] = self._read(module)
        return found

    def inputs_changed(self) -> bool:
        """Whether a file read so far changed on disk since (see ``ScannedAddons``)."""
        return not unchanged(self._stamps)

    def save(self) -> None:
        """Keep what the modules parsed in this run say, for the next one."""
        if self._changed and self._keeper is not None and self._kept is not None:
            self._keeper.save_record(self._key, {"modules": self._kept})
            self._changed = False

    def _read(self, module: str) -> _Source:
        located = self._locate(module) if module else None
        if located is None:
            return NO_SOURCE
        path, package = located
        current = self._stamps[str(path)] = stamp(path)
        kept = self._kept_modules().get(module)
        found = _decoded(kept, path, current)
        if found is None:
            found = _parsed(path, module, package)
            self._kept_modules()[module] = _encoded(found, path, current)
            self._changed = True
        return found

    def _kept_modules(self) -> dict[str, object]:
        if self._kept is None:
            record = self._keeper.load_record(self._key) if self._keeper is not None else None
            modules = record.get("modules") if record is not None else None
            self._kept = dict(modules) if isinstance(modules, dict) else {}
        return self._kept

    def _locate(self, module: str) -> tuple[Path, str] | None:
        """A module's file and the package its relative imports are relative to."""
        addon_file = self._addon_module(module)
        if addon_file is not None:
            is_package = addon_file.name == "__init__.py"
            return addon_file, module if is_package else module.rpartition(".")[0]
        top, _, rest = module.partition(".")
        root = self._odoo_path if top == ODOO else _package_directory(top)
        if root is None:
            return _single_file(top) if not rest else None
        directory = root.joinpath(*rest.split(".")) if rest else root
        package_init = directory / "__init__.py"
        if package_init.is_file():
            return package_init, module
        file = directory.with_suffix(".py")
        return (file, module.rpartition(".")[0]) if file.is_file() else None


def _parsed(path: Path, module: str, package: str) -> _Source:
    """What a module's source says; nothing for a file that cannot be parsed."""
    try:
        tree = parse_source(path)
    except SourceError:
        return NO_SOURCE
    classes, origins = describe_source(tree, module, package)
    # A class the module defines is bound to its own name: only passed-on names stay.
    passed = {name: bound for name, bound in origins.items() if bound != f"{module}.{name}"}
    return _Source(classes, passed, frozenset(origins))


def _encoded(source: _Source, path: Path, current: tuple[int, int] | None) -> list[object]:
    classes = {
        name: [list(item.bases), item.converts, item.arguments, list(item.passes)]
        for name, item in source.classes.items()
    }
    return [
        str(path),
        list(current) if current else None,
        classes,
        source.origins,
        sorted(source.names),
    ]


def _decoded(kept: object, path: Path, current: tuple[int, int] | None) -> _Source | None:
    """The kept description of a module, if it is of this file as it is now."""
    expected = [str(path), list(current) if current else None]
    if not isinstance(kept, list) or len(kept) != KEPT_LENGTH or kept[:2] != expected:
        return None
    classes, origins, names = kept[2:]
    if not (isinstance(classes, dict) and isinstance(origins, dict) and isinstance(names, list)):
        return None
    try:
        found = {
            str(name): FieldClass(
                str(name),
                tuple(map(str, bases)),
                bool(converts),
                bool(own),
                tuple(map(str, passes)),
            )
            for name, (bases, converts, own, passes) in classes.items()
        }
    except (TypeError, ValueError):
        return None
    passed = {str(name): str(bound) for name, bound in origins.items()}
    return _Source(found, passed, frozenset(map(str, names)))


def _package_directory(top: str) -> Path | None:
    """The directory of a top-level package on the search path (it is not imported)."""
    spec = _find(top)
    locations = spec.submodule_search_locations if spec is not None else None
    return Path(next(iter(locations))) if locations else None


def _single_file(top: str) -> tuple[Path, str] | None:
    """The file of a top-level module that is no package (``functools``)."""
    spec = _find(top)
    origin = spec.origin if spec is not None else None
    if origin is None or not origin.endswith(".py"):
        return None  # built in, frozen or an extension module
    return Path(origin), ""


def _find(top: str) -> ModuleSpec | None:
    try:
        return PathFinder.find_spec(top, sys.path)
    except (ImportError, ValueError, AttributeError, OSError):
        return None
