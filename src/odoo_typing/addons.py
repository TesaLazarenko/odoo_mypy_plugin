"""Discovery of Odoo addons: manifests, dependency closure and load order.

The rules follow Odoo 18's ``odoo/modules`` package: an addon is a directory with a
``__manifest__.py`` (or the deprecated ``__openerp__.py``) in an addons path, named after
its directory entry, and addons are loaded ``base`` first, then by dependency depth and
name (``graph.Graph``).
"""

from __future__ import annotations

import ast
import os
import stat
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

__all__ = [
    "CORE_PREFIX",
    "MANIFEST_NAMES",
    "UNREADABLE_MANIFEST",
    "Addon",
    "AddonIndex",
    "LoadKey",
    "find_manifest",
    "is_file",
    "literal_manifest",
    "may_be_addon",
    "read_depends",
    "search_manifest",
]

CORE_PREFIX = "odoo.addons.{}"  # how Odoo itself names every addon package
# Same names and preference as odoo.modules.module.MANIFEST_NAMES.
MANIFEST_NAMES = ("__manifest__.py", "__openerp__.py")
BASE = "base"
# What reading a manifest with ast.literal_eval raises for a file it cannot evaluate:
# an unreadable file, a syntax error, a NUL or bad UTF-8 (ValueError), an unhashable key
# (TypeError), an expression too deep for the parser (MemoryError, RecursionError).
UNREADABLE_MANIFEST = (OSError, SyntaxError, ValueError, TypeError, MemoryError, RecursionError)

LoadKey = tuple[int, str]


@dataclass(frozen=True, slots=True)
class Addon:
    """One Odoo module (addon) found on disk.

    Attributes:
        name: Technical name, e.g. ``sale``.
        path: Directory of the addon.
        module_prefix: Python module name mypy uses for the addon package:
            ``odoo.addons.<name>`` for addons inside the odoo package and, with an
            overlay root (``odoo-typing-root``), for all addons; otherwise ``<name>``
            (the addons directory is expected to be on ``mypy_path``).
        depends: Direct dependencies, as listed in the manifest.
    """

    name: str
    path: Path
    module_prefix: str
    depends: tuple[str, ...]


def may_be_addon(name: str) -> bool:
    """Whether an entry of an addons root may be an addon, judging by its name.

    Not a dot directory (``.git``, ``.idea``: tools write in them all the time) nor
    ``__pycache__``: ``odoo.addons.<name>`` could not import them.
    """
    return not name.startswith(".") and name != "__pycache__"


def find_manifest(directory: Path) -> Path | None:
    """Return the manifest file of an addon directory, like Odoo's ``module_manifest``.

    A directory that cannot be read (``lost+found`` at the root of a volume) is no addon,
    as for Odoo, whose ``os.path.isfile`` never raises.
    """
    return search_manifest(directory)[0]


def search_manifest(directory: Path) -> tuple[Path | None, bool]:
    """The manifest of an addon directory (:func:`find_manifest`), and whether it could look.

    Returns:
        The manifest, ``None`` if there is none; and ``False`` if the directory could not
        be searched (no permission): it may hold a manifest all the same.
    """
    for name in MANIFEST_NAMES:
        candidate = directory / name
        kind = _file_kind(candidate)
        if kind is None:
            return None, False
        if kind:
            return candidate, True
    return None, True


def is_file(path: Path) -> bool:
    """Whether a path is a file or a link to one (``False`` if it cannot tell)."""
    return _file_kind(path) is True


def _file_kind(path: Path) -> bool | None:
    """Whether a path is a file or a link to one; ``None``: no permission to tell."""
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except PermissionError:  # in a directory that cannot be searched
        return None
    except (OSError, ValueError):  # missing, a symlink loop, a NUL in the path
        return False


def literal_manifest(text: str) -> object:
    """The value of a manifest's text, read with ``ast.literal_eval`` (never executed).

    Warnings of the compiler (an invalid escape sequence in a string) are not printed,
    and are no errors under ``-W error``: what is read from a manifest is stored, and a
    later run without the option would keep the error.

    Raises:
        SyntaxError: If the text is no literal (or ``ValueError``, ``TypeError``,
            ``MemoryError``, ``RecursionError``: see :data:`UNREADABLE_MANIFEST`).
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        return ast.literal_eval(text)


def read_depends(manifest: Path) -> tuple[str, ...]:
    """Read the ``depends`` list of a manifest without executing it.

    Args:
        manifest: Path of a ``__manifest__.py`` file.

    Returns:
        Dependency names; empty if the manifest cannot be parsed.
    """
    # Strict UTF-8 text on purpose (unlike Python sources): Odoo reads manifests the
    # same way (tools.file_open + ast.literal_eval), so a BOM manifest is broken there too.
    try:
        data = literal_manifest(manifest.read_text(encoding="utf-8"))
    except UNREADABLE_MANIFEST:
        return ()
    if not isinstance(data, dict):
        return ()
    depends = data.get("depends", [])
    if not isinstance(depends, list | tuple):
        return ()
    return tuple(item for item in depends if isinstance(item, str))


class AddonIndex:
    """All addons of the configured addons paths, with dependency helpers."""

    def __init__(
        self,
        odoo_path: Path,
        addons_paths: Iterable[Path],
        custom_prefix: str = "{}",
        *,
        known: Iterable[Addon] = (),
    ) -> None:
        """Scan addons directories.

        Args:
            odoo_path: Directory of the ``odoo`` package.
            addons_paths: Extra addons directories. Earlier entries win on name clashes.
            custom_prefix: Module name template of the addons in ``addons_paths``:
                ``"{}"`` (top-level packages) or ``"odoo.addons.{}"`` (overlay root).
            known: The addons a previous scan of the same directories (with the same
                ``custom_prefix``) found, in its order (:attr:`addons`). If given, the
                directories are not read again.
        """
        self.addons: dict[str, Addon] = {}
        # Every module spelling of an addon package. An addon of addons_paths has two
        # (Odoo's ``odoo.addons.<name>`` and the top-level ``<name>``), whichever one
        # mypy is configured to use; a core addon only has Odoo's.
        self._prefixes: dict[str, Addon] = {}
        self._by_path: dict[Path, Addon] = {}
        self._closures: dict[str, frozenset[str]] = {}
        # Both keyed by a source file's directory as mypy spells it: the plugin asks
        # about every file mypy loads, often several times, and resolve() costs syscalls.
        self._real_dirs: dict[str, Path] = {}
        self._dir_addons: dict[str, Addon | None] = {}
        core_root = (odoo_path / "addons").resolve()
        for addon in known:
            # A core addon is a direct entry of odoo/addons with Odoo's spelling (a
            # symlinked core addon of an overlay setup is taken for a custom one).
            core = addon.path.parent == core_root
            odoo_spelling = addon.module_prefix == CORE_PREFIX.format(addon.name)
            self._register(addon, aliases=not (core and odoo_spelling))
        if self.addons:
            return
        self._add_root(odoo_path / "addons", prefix_template=CORE_PREFIX, aliases=False)
        for root in addons_paths:
            self._add_root(root, prefix_template=custom_prefix, aliases=True)

    def _add_root(self, root: Path, prefix_template: str, aliases: bool) -> None:
        try:
            children = sorted(child for child in root.iterdir() if may_be_addon(child.name))
        except OSError:  # missing, or it cannot be listed: no addons (see the stamps)
            return
        real_root = root.resolve()
        for child in children:
            manifest = None if child.name in self.addons else find_manifest(child)
            if manifest is None:
                continue
            addon = Addon(
                name=child.name,
                path=child.resolve() if child.is_symlink() else real_root / child.name,
                module_prefix=prefix_template.format(child.name),
                depends=read_depends(manifest),
            )
            self._register(addon, aliases=aliases)

    def _register(self, addon: Addon, *, aliases: bool) -> None:
        """Index an addon; with ``aliases``, under both module spellings."""
        self.addons[addon.name] = addon
        self._by_path.setdefault(addon.path, addon)
        spellings = (CORE_PREFIX.format(addon.name), addon.name) if aliases else ()
        for spelling in (addon.module_prefix, *spellings):
            self._prefixes[spelling] = addon

    def closure(self, name: str) -> frozenset[str]:
        """Return the addon and all its (transitive) dependencies that exist on disk.

        ``base`` is always part of it: Odoo loads it first, whatever the manifests say.
        """
        if name not in self._closures:
            self._closures[name] = self._compute_closure(name)
        return self._closures[name]

    def _compute_closure(self, name: str) -> frozenset[str]:
        seen: set[str] = set()
        stack = [name, BASE]
        while stack:
            current = stack.pop()
            if current in seen or current not in self.addons:
                continue
            seen.add(current)
            stack.extend(self.addons[current].depends)
        return frozenset(seen)

    def missing_depends(self, name: str) -> tuple[str, ...]:
        """Return the dependencies in an addon's closure that are not on any addons path."""
        missing = {
            dependency
            for member in self.closure(name)
            for dependency in self.addons[member].depends
            if dependency not in self.addons
        }
        return tuple(sorted(missing))

    def load_key(self, name: str) -> LoadKey:
        """Sort key of an addon in Odoo 18's load order.

        ``base`` comes first. The others follow ``odoo.modules.graph.Graph``: by depth
        (0 without dependencies, else one more than the deepest dependency), then by
        name. Dependencies that are not on disk do not count.
        """
        return (-1, name) if name == BASE else (self._depths[name], name)

    @cached_property
    def _depths(self) -> dict[str, int]:
        # Computed for all addons at once, in name order, so that a dependency cycle
        # (which Odoo refuses to load) still gives the same depths whatever is asked first.
        depths: dict[str, int] = {}
        visiting: set[str] = set()

        def depth(name: str) -> int:
            if name not in depths and name not in visiting:
                visiting.add(name)
                known = [dep for dep in self.addons[name].depends if dep in self.addons]
                depths[name] = 1 + max(map(depth, known)) if known else 0
                visiting.discard(name)
            return depths.get(name, 0)

        for name in sorted(self.addons):
            depth(name)
        return depths

    def addon_for_module(self, fullname: str) -> Addon | None:
        """Find the addon a Python module (as named by mypy) belongs to.

        A module of an ``addons_path`` addon is recognized under both spellings,
        ``<name>.x`` and ``odoo.addons.<name>.x``; a core addon only under the second.
        """
        return self._prefixes.get(_package_spelling(fullname))

    def resolve_module(self, fullname: str) -> tuple[Addon, str] | None:
        """Find a module's addon, and the module's name in the configured spelling.

        Args:
            fullname: A module name, or the full name of something in a module.

        Returns:
            The addon and ``fullname`` with the addon package spelled as its
            ``module_prefix`` (the name the scan indexes it under); ``None`` outside
            addons.
        """
        spelling = _package_spelling(fullname)
        addon = self._prefixes.get(spelling)
        if addon is None:
            return None
        if spelling == addon.module_prefix:
            return addon, fullname
        return addon, addon.module_prefix + fullname[len(spelling) :]

    def canonical_module(self, fullname: str) -> str:
        """Return a module name with its addon package spelled as configured."""
        found = self.resolve_module(fullname)
        return found[1] if found is not None else fullname

    def real_path(self, path: str | Path) -> Path:
        """Return a source file's path with its directory resolved (cached per directory).

        The file name itself is kept: mypy names a module after where the file is,
        not after the target of a symlinked file.
        """
        directory, name = os.path.split(path)
        return self._real_dir(directory) / name

    def addon_for_path(self, path: str | Path) -> Addon | None:
        """Find the addon a source file belongs to."""
        directory = os.path.dirname(path)  # noqa: PTH120 (a string key: asked by every hook)
        if directory not in self._dir_addons:
            self._dir_addons[directory] = self._find_addon(self._real_dir(directory))
        return self._dir_addons[directory]

    def _real_dir(self, directory: str) -> Path:
        if directory not in self._real_dirs:
            self._real_dirs[directory] = Path(directory or os.curdir).resolve()
        return self._real_dirs[directory]

    def _find_addon(self, directory: Path) -> Addon | None:
        # By resolved path, not by name: an addon may be a symlink with another name
        # (addons/t_link -> ../src/t_link-18.0), and Odoo names it after the link.
        for candidate in (directory, *directory.parents):
            addon = self._by_path.get(candidate)
            if addon is not None:
                return addon
        return None


def _package_spelling(fullname: str) -> str:
    """The part of a module name that names an addon package, if it is one."""
    parts = fullname.split(".", 3)
    if parts[:2] == ["odoo", "addons"]:
        return ".".join(parts[:3])
    return parts[0]
