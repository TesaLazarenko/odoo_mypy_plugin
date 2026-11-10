"""Discovery of Odoo addons: manifests, dependency closure and load order."""

from __future__ import annotations

import ast
import os
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

__all__ = ["Addon", "AddonIndex", "read_depends"]

MANIFEST = "__manifest__.py"


@dataclass(frozen=True, slots=True)
class Addon:
    """One Odoo module (addon) found on disk.

    Attributes:
        name: Technical name, e.g. ``sale``.
        path: Directory of the addon.
        module_prefix: Python module name mypy uses for the addon package:
            ``odoo.addons.<name>`` for addons inside the odoo package, otherwise
            ``<name>`` (the addons directory is expected to be on ``mypy_path``).
        depends: Direct dependencies from the manifest (``base`` is implicit).
    """

    name: str
    path: Path
    module_prefix: str
    depends: tuple[str, ...]


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
        data = ast.literal_eval(manifest.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
        return ()
    if not isinstance(data, dict):
        return ()
    depends = data.get("depends", [])
    if not isinstance(depends, list | tuple):
        return ()
    return tuple(item for item in depends if isinstance(item, str))


class AddonIndex:
    """All addons of the configured addons paths, with dependency helpers."""

    def __init__(self, odoo_path: Path, addons_paths: Iterable[Path]) -> None:
        """Scan addons directories.

        Args:
            odoo_path: Directory of the ``odoo`` package.
            addons_paths: Extra addons directories. Earlier entries win on name clashes.
        """
        self.addons: dict[str, Addon] = {}
        self._closures: dict[str, frozenset[str]] = {}
        # Both keyed by a source file's directory as mypy spells it: the plugin asks
        # about every file mypy loads, often several times, and resolve() costs syscalls.
        self._real_dirs: dict[str, Path] = {}
        self._dir_addons: dict[str, Addon | None] = {}
        self._add_root(odoo_path / "addons", prefix_template="odoo.addons.{}")
        for root in addons_paths:
            self._add_root(root, prefix_template="{}")

    def _add_root(self, root: Path, prefix_template: str) -> None:
        if not root.is_dir():
            return
        real_root = root.resolve()
        for child in sorted(root.iterdir()):
            if child.name in self.addons or not (child / MANIFEST).is_file():
                continue
            depends = read_depends(child / MANIFEST)
            if child.name != "base" and "base" not in depends:
                depends = ("base", *depends)
            self.addons[child.name] = Addon(
                name=child.name,
                path=child.resolve() if child.is_symlink() else real_root / child.name,
                module_prefix=prefix_template.format(child.name),
                depends=depends,
            )

    def closure(self, name: str) -> frozenset[str]:
        """Return the addon and all its (transitive) dependencies that exist on disk."""
        if name not in self._closures:
            self._closures[name] = self._compute_closure(name)
        return self._closures[name]

    def _compute_closure(self, name: str) -> frozenset[str]:
        seen: set[str] = set()
        stack = [name]
        while stack:
            current = stack.pop()
            if current in seen or current not in self.addons:
                continue
            seen.add(current)
            stack.extend(self.addons[current].depends)
        return frozenset(seen)

    @cached_property
    def load_order(self) -> dict[str, int]:
        """Position of every addon in a deterministic topological load order."""
        order: dict[str, int] = {}
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in order or name in visiting or name not in self.addons:
                return
            visiting.add(name)
            for dependency in sorted(self.addons[name].depends):
                visit(dependency)
            visiting.discard(name)
            order[name] = len(order)

        for name in sorted(self.addons):
            visit(name)
        return order

    def addon_for_module(self, fullname: str) -> Addon | None:
        """Find the addon a Python module (as named by mypy) belongs to."""
        parts = fullname.split(".")
        if parts[:2] == ["odoo", "addons"] and len(parts) > 2:
            addon = self.addons.get(parts[2])
            if addon is not None and addon.module_prefix == ".".join(parts[:3]):
                return addon
            return None
        addon = self.addons.get(parts[0])
        if addon is not None and addon.module_prefix == parts[0]:
            return addon
        return None

    def real_path(self, path: str | Path) -> Path:
        """Return a source file's path with its directory resolved (cached per directory).

        The file name itself is kept: mypy names a module after where the file is,
        not after the target of a symlinked file.
        """
        directory, name = os.path.split(path)
        return self._real_dir(directory) / name

    def addon_for_path(self, path: str | Path) -> Addon | None:
        """Find the addon a source file belongs to."""
        directory = os.path.dirname(path)
        if directory not in self._dir_addons:
            self._dir_addons[directory] = self._find_addon(self._real_dir(directory))
        return self._dir_addons[directory]

    def _real_dir(self, directory: str) -> Path:
        if directory not in self._real_dirs:
            self._real_dirs[directory] = Path(directory or os.curdir).resolve()
        return self._real_dirs[directory]

    def _find_addon(self, directory: Path) -> Addon | None:
        for candidate in (directory, *directory.parents):
            addon = self.addons.get(candidate.name)
            if addon is not None and addon.path == candidate:
                return addon
        return None
