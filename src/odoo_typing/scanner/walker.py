"""The scan of an addon: its ``__init__.py`` imports replayed in Python's execution order.

Only files reachable through the addon's ``__init__.py`` imports are scanned. Imports
run where they appear, also inside ``try``/``if`` blocks, which is the order in which
Odoo registers the classes.

The scan stamps (:mod:`odoo_typing.stamps`) every file and directory it reads or looks
into, right before it does: the stored scan is valid while they are unchanged. One it
cannot read or search is :data:`~odoo_typing.stamps.UNREADABLE`: the scan stays valid
while it still cannot be, and is redone once it can (a permission fix keeps the mtime).
"""

from __future__ import annotations

import ast
from collections import ChainMap
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from stat import S_ISREG

from odoo_typing.addons import Addon, LoadKey, find_manifest
from odoo_typing.frozen import frozen
from odoo_typing.guards import model_guards
from odoo_typing.node_index import index_nodes
from odoo_typing.scanner.annotations import annotation_aliases
from odoo_typing.scanner.class_bodies import (
    abstract,
    as_names,
    declared_comodels,
    defined_methods,
    delegated_models,
    inherits_pairs,
    log_access,
    model_name,
)
from odoo_typing.scanner.evaluation import UNKNOWN, assign, class_attributes
from odoo_typing.scanner.field_classes import FieldKind, class_bases, field_kind, origin_of
from odoo_typing.scanner.fields import declared_fields
from odoo_typing.scanner.referenced import referenced_models
from odoo_typing.scanner.results import (
    AddonScan,
    Contribution,
    FieldClass,
    ModuleScan,
    SourceError,
)
from odoo_typing.scanner.syntax import parse_bytes, read_source
from odoo_typing.stamps import UNREADABLE, Stamps, stamp
from odoo_typing.xmlids import scan_data

__all__ = ["scan_addon", "scan_file_classes"]

ADDONS_PREFIX = "odoo.addons."


def scan_addon(addon: Addon, load_key: LoadKey, stamps: Stamps | None = None) -> AddonScan:
    """Scan all Python files of an addon that its ``__init__.py`` imports, and its data.

    Args:
        addon: Addon to scan.
        load_key: Position of the addon in Odoo's load order
            (:meth:`odoo_typing.addons.AddonIndex.load_key`).
        stamps: Filled with the stamps of what the scan reads or looks into (the
            manifest too), each taken before; a file that could not be read gets
            :data:`~odoo_typing.stamps.UNREADABLE`.

    Returns:
        The scanned files, and the files that could not be parsed.
    """
    walker = _ImportWalker(addon, load_key, stamps)
    walker.visit_module(addon.module_prefix, addon.path)
    data = scan_data(addon.name, addon.path, find_manifest(addon.path), walker.stamps)
    return AddonScan(modules=tuple(walker.scans), errors=tuple(walker.errors), data=data)


def scan_file_classes(addon: Addon, path: Path) -> tuple[Contribution, ...]:
    """The model classes of one file of an addon, read on its own (no import followed).

    For questions about every addon on the addons paths at once (the keys of
    ``odoo.values.*``, :mod:`odoo_typing.registry.annotations`), where scanning each
    whole addon would cost seconds. Without the other files of the addon, a model name
    or argument taken from another module's constant is unknown, and the classes'
    positions mean nothing. A file that cannot be parsed has no classes.

    Raises:
        SourceError: If the file cannot be read: its classes are not known.
    """
    source = read_source(path)
    walker = _FileWalker(addon, (0, addon.name))
    try:
        tree = parse_bytes(source, path)
    except SourceError:
        return ()
    file = _File(module=addon.module_prefix, package=addon.module_prefix)
    walker._walk(tree.body, file)
    return tuple(file.contributions)


@dataclass(slots=True)
class _File:
    """State of one module while its top-level statements are walked."""

    module: str
    package: str  # what relative imports are relative to
    names: dict[str, object] = field(default_factory=dict)  # module-level constants
    contributions: list[Contribution] = field(default_factory=list)
    # What the module's names are bound to (see odoo_typing.scanner.field_classes).
    origins: dict[str, str] = field(default_factory=dict)
    classes: dict[str, FieldClass] = field(default_factory=dict)  # by class name


class _ImportWalker:
    """Replays an addon's import-time execution, statement by statement.

    Odoo registers model classes in creation order (``MetaModel.module_to_models``),
    which is Python's execution order: an import runs the imported module's classes
    at the import statement, before the importing module's later classes.
    """

    def __init__(self, addon: Addon, load_key: LoadKey, stamps: Stamps | None = None) -> None:
        self.stamps: Stamps = {} if stamps is None else stamps  # see scan_addon
        self.addon = addon
        self.load_key = load_key
        self.scans: list[ModuleScan] = []
        self.errors: list[SourceError] = []
        self._seen: set[Path] = set()
        self._classes: set[str] = set()
        self._names: dict[str, dict[str, object]] = {}  # module-level names of each module
        self._origins: dict[str, dict[str, str]] = {}  # what each module's names are bound to
        # The classes of the addon that may be field classes, by dotted name.
        self._field_classes: dict[str, FieldClass] = {}
        # Whether every class of the addon that a file can name was walked before it.
        self._whole_addon = True
        # Absolute spellings of the addon's own package: Odoo's, and the one mypy uses.
        self._roots = {f"odoo.addons.{addon.name}", addon.module_prefix}

    def look(self, path: Path) -> None:
        """Stamp a file or directory before it is read or looked into (the first time).

        A missing one is not stamped: creating it changes the stamp of its directory,
        which was looked into before.
        """
        key = str(path)
        if key not in self.stamps and (current := stamp(path)) is not None:
            self.stamps[key] = current

    def visit_module(self, module: str, base: Path) -> None:
        """Execute a module or package of the addon, the first time it is imported."""
        if base != self.addon.path:  # never the addons root: any new addon changes it
            self.look(base.parent)  # where ``base.py`` and ``base/`` are looked up
        self.look(base)  # a package directory, also one without ``__init__.py`` yet
        init = base / "__init__.py"
        if self._is_file(init):
            self._visit(init, module, package=module)
        else:
            self._visit(base.with_suffix(".py"), module, package=module.rpartition(".")[0])

    def _is_file(self, path: Path) -> bool:
        """Whether a file exists; one in a directory that cannot be searched is an error."""
        try:
            return S_ISREG(path.stat().st_mode)
        except PermissionError as error:
            if path not in self._seen:
                self._seen.add(path)
                self.errors.append(SourceError(path, error.strerror or str(error)))
                self.stamps[str(path.parent)] = UNREADABLE  # until it can be searched
            return False
        except (OSError, ValueError):
            return False

    def _visit(self, path: Path, module: str, package: str) -> None:
        if path in self._seen:
            return
        self.look(path)
        if not self._is_file(path):
            return
        self._seen.add(path)
        try:
            source = read_source(path)
        except SourceError as error:
            self.stamps[str(path)] = UNREADABLE  # until it can be read
            self.errors.append(error)
            return
        try:
            tree = parse_bytes(source, path)
        except SourceError as error:
            self.errors.append(error)
            return
        nodes = index_nodes(tree)
        file = _File(module=module, package=package)
        self._names[module] = file.names
        self._origins[module] = file.origins
        self._walk(tree.body, file)
        self.scans.append(
            ModuleScan(
                module=module,
                path=path,
                contributions=tuple(file.contributions),
                referenced_models=frozen(referenced_models(tree, nodes)),
                guards=model_guards(nodes),
                annotations=annotation_aliases(source),
                classes=self._exported_classes(file),
            )
        )

    def _exported_classes(self, file: _File) -> tuple[FieldClass, ...]:
        """The classes of a module that may be field classes, and those it re-exports.

        A name the module imports from another module of the addon is exported as a
        class whose base is the class it names: ``from odoo.addons.x.models import Big``
        in another addon then finds what ``models/__init__.py`` imports from ``.fields``.
        """
        exported = dict(file.classes)
        for name, origin in file.origins.items():
            if name not in exported and origin in self._field_classes:
                exported[name] = FieldClass(name, (origin,))
        return tuple(exported.values())

    def _walk(self, body: list[ast.stmt], file: _File) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                self._add_class(node, file)
            elif isinstance(node, ast.ImportFrom):
                self._import_from(node, file)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self._import(alias.name.split("."))
                    self._bind_import(alias, file)
            elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
                assign(node, file.names)
                _bind_alias(node, file.origins)
            else:
                for block in _executed_blocks(node):
                    self._walk(block, file)

    def _import_from(self, node: ast.ImportFrom, file: _File) -> None:
        package = file.package.split(".") if node.level else []
        dotted = package[: len(package) - node.level + 1] if node.level else []
        dotted += node.module.split(".") if node.module else []
        parts = self._inner_parts(dotted)
        if parts is None:
            for alias in node.names:  # ``from odoo import fields``
                if alias.name != "*":
                    file.origins[alias.asname or alias.name] = ".".join([*dotted, alias.name])
            return
        self._import(dotted)
        module = ".".join([self.addon.module_prefix, *parts])
        source = self._names.get(module, {})
        origins = self._origins.get(module, {})
        for alias in node.names:
            self._import([*dotted, alias.name])  # runs only if it is a submodule
            name = alias.asname or alias.name
            if alias.name in source:  # ``from .consts import STATES``: a known value
                file.names[name] = source[alias.name]
            if alias.name != "*":
                # What the module binds the name to, or the name in it (a submodule).
                file.origins[name] = origins.get(alias.name, f"{module}.{alias.name}")

    def _bind_import(self, alias: ast.alias, file: _File) -> None:
        """Bind what ``import a.b`` (the name ``a``) or ``import a.b as c`` names."""
        if alias.asname is not None:
            file.origins[alias.asname] = alias.name
        else:
            top = alias.name.partition(".")[0]
            file.origins[top] = top

    def _import(self, dotted: list[str]) -> None:
        """Run ``import a.b.c`` if it names this addon: each package, then the module."""
        parts = self._inner_parts(dotted)
        module, base = self.addon.module_prefix, self.addon.path
        for part in parts or ():
            module, base = f"{module}.{part}", base / part
            self.visit_module(module, base)

    def _inner_parts(self, dotted: list[str]) -> list[str] | None:
        for root in self._roots:
            root_parts = root.split(".")
            if dotted[: len(root_parts)] == root_parts:
                return dotted[len(root_parts) :]
        return None

    def _add_class(self, node: ast.ClassDef, file: _File) -> None:
        attrs = class_attributes(node, file.names)
        file.names[node.name] = UNKNOWN
        model = model_name(node.name, attrs)
        fullname = f"{file.module}.{node.name}"
        # A second definition under the same name (if/else alternatives) is not the
        # class mypy knows by that name; mypy keeps the first one.
        if model is None or not attrs.get("_register", True):
            self._add_field_class(node, fullname, file)
            return
        file.origins.pop(node.name, None)  # a model class is no field class
        if fullname in self._classes:
            return
        self._classes.add(fullname)
        methods = defined_methods(node)
        inherits = attrs.get("_inherits")
        scope = ChainMap(attrs, file.names)
        file.contributions.append(
            Contribution(
                addon=self.addon.name,
                module=file.module,
                class_name=node.name,
                model=model,
                parents=as_names(attrs.get("_inherit")),
                position=(*self.load_key, len(self._classes)),
                comodels=declared_comodels(node),
                fields=declared_fields(
                    node, scope, frozenset(methods), partial(self._field_class_of, file)
                ),
                methods=methods,
                delegates=delegated_models(node, inherits),
                inherits=inherits_pairs(inherits),
                log_access=log_access(attrs),
                abstract=abstract(node, attrs),
            )
        )

    def _add_field_class(self, node: ast.ClassDef, fullname: str, file: _File) -> None:
        """Remember a class that is no model class, and what may make it a field class."""
        if file.origins.get(node.name) == fullname:
            return  # a second definition: the first one is the class of that name
        file.origins[node.name] = fullname
        found = class_bases(
            node, partial(self._origin, file), self._field_classes, self._described_elsewhere
        )
        if found is not None:
            self._field_classes[fullname] = file.classes[node.name] = found

    def _origin(self, file: _File, node: ast.expr) -> str | None:
        """The dotted name an expression names, this addon's package in the scan's spelling."""
        origin = origin_of(node, file.origins)
        if origin is None:
            return None
        parts = self._inner_parts(origin.split("."))
        return ".".join([self.addon.module_prefix, *parts]) if parts is not None else origin

    def _described_elsewhere(self, origin: str) -> bool:
        """Whether a dotted name may be a class that a scan this one cannot see describes.

        A class of another addon (addons import each other as ``odoo.addons.<name>``);
        one of this addon only when its files are not all walked (:class:`_FileWalker`).
        """
        if origin.startswith(self.addon.module_prefix + "."):
            return not self._whole_addon
        return origin.startswith(ADDONS_PREFIX)

    def _field_class_of(self, file: _File, call: ast.Call) -> FieldKind | str | None:
        """The field class a call in a model's class body makes a field of.

        Returns:
            The kind of the class, if Odoo's classes and those of this addon tell it; the
            dotted name of a class that only another scan can tell about; ``None`` if the
            callee is not a field class.
        """
        origin = self._origin(file, call.func)
        if origin is None:
            return _unbound_fields_class(call.func)
        kind = field_kind(origin, self._field_classes.get)
        if kind is not None:
            return kind
        # A class of this addon that derives from another addon's, or a class of another.
        elsewhere = origin in self._field_classes or self._described_elsewhere(origin)
        return origin if elsewhere else None


class _FileWalker(_ImportWalker):
    """Walks one file's top-level statements without running its imports.

    The classes of the addon's other files are then not known: a class of the addon that
    a field is made of is left for the registry to link, like one of another addon.
    """

    def __init__(self, addon: Addon, load_key: LoadKey, stamps: Stamps | None = None) -> None:
        super().__init__(addon, load_key, stamps)
        self._whole_addon = False

    def _import(self, dotted: list[str]) -> None:
        return None


def _unbound_fields_class(func: ast.expr) -> FieldKind | None:
    """Odoo's class of ``fields.<Class>`` in a file that does not bind ``fields``.

    A name the file gets from a star import (or a snippet without imports) is read as
    Odoo's ``fields`` module.
    """
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "fields"
        and func.attr[:1].isupper()
    ):
        return FieldKind(func.attr, func.attr)
    return None


def _bind_alias(node: ast.stmt, origins: dict[str, str]) -> None:
    """Follow ``Alias = Class`` (and forget what a rebound name was bound to)."""
    if not isinstance(node, ast.Assign):
        return
    origin = origin_of(node.value, origins)
    for target in node.targets:
        if not isinstance(target, ast.Name):
            continue
        if origin is not None and isinstance(node.value, ast.Name | ast.Attribute):
            origins[target.id] = origin
        else:
            origins.pop(target.id, None)


def _executed_blocks(node: ast.stmt) -> list[list[ast.stmt]]:
    """The statement lists of a compound statement that may run at import time.

    A static scan cannot tell which branch runs, so both branches of an ``if`` and
    all clauses of a ``try`` count, in source order. ``if TYPE_CHECKING:`` bodies
    never run. Function and class bodies are not module-level code, and loops are
    left out (a class created in a loop needs a computed ``_name`` anyway).
    """
    if isinstance(node, ast.If):
        return [node.orelse] if _is_type_checking(node.test) else [node.body, node.orelse]
    if isinstance(node, ast.Try | ast.TryStar):
        handlers = [handler.body for handler in node.handlers]
        return [node.body, *handlers, node.orelse, node.finalbody]
    if isinstance(node, ast.With | ast.AsyncWith):
        return [node.body]
    if isinstance(node, ast.Match):
        return [case.body for case in node.cases]
    return []


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )
