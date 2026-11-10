"""Static scan of an addon's Python files for Odoo model classes.

Only files reachable through the addon's ``__init__.py`` imports are scanned. The
scan follows Python's execution order (imports run where they appear, also inside
``try``/``if`` blocks), which is the order in which Odoo registers the classes, and
derives model names the way ``odoo.models.MetaModel`` does.

Sources are parsed from bytes, like Python and mypy do, so a UTF-8 BOM or a PEP 263
coding cookie is honoured.
"""

from __future__ import annotations

import ast
from collections import ChainMap
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from odoo_typing.addons import Addon, LoadKey

__all__ = [
    "AddonScan",
    "Contribution",
    "ModuleScan",
    "SourceError",
    "parse_source",
    "referenced_models",
    "scan_addon",
]

UNKNOWN: Final = object()  # value of an expression the scan cannot evaluate
RELATIONAL_FIELDS = frozenset({"Many2one", "One2many", "Many2many"})
# Concrete node classes that have no child nodes worth visiting (``ast.Load`` etc.).
LEAF_NODES: frozenset[type] = frozenset(
    {ast.Name, ast.Constant, ast.alias}
    | {
        cls
        for base in (ast.expr_context, ast.operator, ast.boolop, ast.unaryop, ast.cmpop)
        for cls in base.__subclasses__()
    }
)


@dataclass(frozen=True, slots=True)
class Contribution:
    """One Python class that defines or extends an Odoo model.

    Attributes:
        addon: Name of the addon that contains the class.
        module: Python module name, as mypy sees it.
        class_name: Name of the class.
        model: Odoo model name (``_name``, or the first ``_inherit``).
        parents: Models listed in ``_inherit``.
        position: Registration order: the addon's load key, then the class's
            creation index within the addon.
        comodels: Comodels of the relational fields the class body declares
            (``x = fields.Many2one("res.partner")``), in source order.
    """

    addon: str
    module: str
    class_name: str
    model: str
    parents: tuple[str, ...]
    position: tuple[int, str, int]
    comodels: tuple[str, ...] = ()

    @property
    def fullname(self) -> str:
        """Fully qualified class name."""
        return f"{self.module}.{self.class_name}"


@dataclass(frozen=True, slots=True)
class ModuleScan:
    """What one Python file of an addon contributes.

    Attributes:
        module: Python module name.
        path: Source file.
        contributions: Model classes defined in the file, in source order.
        referenced_models: Model names used as string literals in ``env[...]``
            and as relational field comodels.
    """

    module: str
    path: Path
    contributions: tuple[Contribution, ...]
    referenced_models: frozenset[str]


class SourceError(Exception):
    """A Python source file that cannot be read or parsed.

    Attributes:
        path: The file.
        reason: Short description, e.g. ``invalid syntax (line 3)``.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AddonScan:
    """Result of scanning one addon.

    Attributes:
        modules: One entry per imported file, in import order.
        errors: Imported files that could not be parsed (their classes are missing).
    """

    modules: tuple[ModuleScan, ...]
    errors: tuple[SourceError, ...]


def parse_source(path: Path) -> ast.Module:
    """Parse a Python file the way the interpreter reads it.

    The file is passed to the parser as bytes, so a UTF-8 BOM and coding cookies
    (``# -*- coding: latin-1 -*-``) are honoured.

    Args:
        path: Source file.

    Returns:
        The module's syntax tree.

    Raises:
        SourceError: If the file cannot be read or is not valid Python.
    """
    try:
        return ast.parse(path.read_bytes(), filename=str(path))
    except OSError as exc:
        raise SourceError(path, exc.strerror or str(exc)) from exc
    except SyntaxError as exc:
        raise SourceError(path, f"{exc.msg} (line {exc.lineno})") from exc
    except ValueError as exc:  # e.g. null bytes on older Python versions
        raise SourceError(path, str(exc)) from exc


def scan_addon(addon: Addon, load_key: LoadKey) -> AddonScan:
    """Scan all Python files of an addon that its ``__init__.py`` imports.

    Args:
        addon: Addon to scan.
        load_key: Position of the addon in Odoo's load order
            (:meth:`odoo_typing.addons.AddonIndex.load_key`).

    Returns:
        The scanned files, and the files that could not be parsed.
    """
    walker = _ImportWalker(addon, load_key)
    walker.visit_module(addon.module_prefix, addon.path)
    return AddonScan(modules=tuple(walker.scans), errors=tuple(walker.errors))


@dataclass(slots=True)
class _File:
    """State of one module while its top-level statements are walked."""

    module: str
    package: str  # what relative imports are relative to
    names: dict[str, object] = field(default_factory=dict)  # module-level constants
    contributions: list[Contribution] = field(default_factory=list)


class _ImportWalker:
    """Replays an addon's import-time execution, statement by statement.

    Odoo registers model classes in creation order (``MetaModel.module_to_models``),
    which is Python's execution order: an import runs the imported module's classes
    at the import statement, before the importing module's later classes.
    """

    def __init__(self, addon: Addon, load_key: LoadKey) -> None:
        self.addon = addon
        self.load_key = load_key
        self.scans: list[ModuleScan] = []
        self.errors: list[SourceError] = []
        self._seen: set[Path] = set()
        self._classes: set[str] = set()
        # Absolute spellings of the addon's own package: Odoo's, and the one mypy uses.
        self._roots = {f"odoo.addons.{addon.name}", addon.module_prefix}

    def visit_module(self, module: str, base: Path) -> None:
        """Execute a module or package of the addon, the first time it is imported."""
        init = base / "__init__.py"
        if init.is_file():
            self._visit(init, module, package=module)
        else:
            self._visit(base.with_suffix(".py"), module, package=module.rpartition(".")[0])

    def _visit(self, path: Path, module: str, package: str) -> None:
        if path in self._seen or not path.is_file():
            return
        self._seen.add(path)
        try:
            tree = parse_source(path)
        except SourceError as error:
            self.errors.append(error)
            return
        file = _File(module=module, package=package)
        self._walk(tree.body, file)
        self.scans.append(
            ModuleScan(
                module=module,
                path=path,
                contributions=tuple(file.contributions),
                referenced_models=frozenset(referenced_models(tree)),
            )
        )

    def _walk(self, body: list[ast.stmt], file: _File) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                self._add_class(node, file)
            elif isinstance(node, ast.ImportFrom):
                self._import_from(node, file)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self._import(alias.name.split("."))
            elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
                _assign(node, file.names)
            else:
                for block in _executed_blocks(node):
                    self._walk(block, file)

    def _import_from(self, node: ast.ImportFrom, file: _File) -> None:
        package = file.package.split(".") if node.level else []
        dotted = package[: len(package) - node.level + 1] if node.level else []
        dotted += node.module.split(".") if node.module else []
        if self._inner_parts(dotted) is None:
            return
        self._import(dotted)
        for alias in node.names:
            self._import([*dotted, alias.name])  # runs only if it is a submodule

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
        attrs = _class_attributes(node, file.names)
        file.names[node.name] = UNKNOWN
        model = _model_name(node.name, attrs)
        fullname = f"{file.module}.{node.name}"
        # A second definition under the same name (if/else alternatives) is not the
        # class mypy knows by that name; mypy keeps the first one.
        if model is None or not attrs.get("_register", True) or fullname in self._classes:
            return
        self._classes.add(fullname)
        file.contributions.append(
            Contribution(
                addon=self.addon.name,
                module=file.module,
                class_name=node.name,
                model=model,
                parents=_as_names(attrs.get("_inherit")),
                position=(*self.load_key, len(self._classes)),
                comodels=_declared_comodels(node),
            )
        )


def _declared_comodels(node: ast.ClassDef) -> tuple[str, ...]:
    """Comodel literals of the relational fields assigned in a class body."""
    comodels: dict[str, None] = {}
    for statement in node.body:
        value = statement.value if isinstance(statement, ast.Assign | ast.AnnAssign) else None
        if isinstance(value, ast.Call) and _callee_name(value.func) in RELATIONAL_FIELDS:
            comodel = _comodel_argument(value)
            if comodel:
                comodels[comodel] = None
    return tuple(comodels)


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


def _class_attributes(node: ast.ClassDef, module_names: dict[str, object]) -> dict[str, object]:
    """Values of the names a class body assigns, evaluated in order.

    Like Python, the body sees its own earlier assignments first, then the module's
    names: ``_inherit = _name`` and ``_name = PREFIX + ".x"`` work.
    """
    attrs: dict[str, object] = {}
    scope = ChainMap(attrs, module_names)  # assignments go to the first map
    for statement in node.body:
        if isinstance(statement, ast.Assign | ast.AnnAssign | ast.AugAssign):
            _assign(statement, scope)
    return attrs


def _assign(
    statement: ast.Assign | ast.AnnAssign | ast.AugAssign, scope: MutableMapping[str, object]
) -> None:
    """Bind the names assigned by a statement (also ``a = b = value``) in a namespace."""
    if isinstance(statement, ast.Assign):
        targets, value = statement.targets, _evaluate(statement.value, scope)
    elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
        targets, value = [statement.target], _evaluate(statement.value, scope)
    else:  # augmented, or annotation only
        targets, value = [statement.target], UNKNOWN
    for target in targets:
        names = target.elts if isinstance(target, ast.Tuple | ast.List) else [target]
        for name in names:
            if isinstance(name, ast.Name):
                scope[name.id] = value if name is target else UNKNOWN


def _evaluate(node: ast.expr, scope: Mapping[str, object]) -> object:
    """Value of a constant expression, or :data:`UNKNOWN`.

    Handles literals, lists and tuples, names bound earlier, ``+`` of strings and
    f-strings of known values: what model names and ``_inherit`` lists are made of.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return scope.get(node.id, UNKNOWN)
    if isinstance(node, ast.List | ast.Tuple):
        items = [_evaluate(item, scope) for item in node.elts]
        return items if isinstance(node, ast.List) else tuple(items)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _evaluate(node.left, scope), _evaluate(node.right, scope)
        return left + right if isinstance(left, str) and isinstance(right, str) else UNKNOWN
    if isinstance(node, ast.JoinedStr):
        parts = [_format_part(part, scope) for part in node.values]
        strings = [part for part in parts if isinstance(part, str)]
        return "".join(strings) if len(strings) == len(parts) else UNKNOWN
    return UNKNOWN


def _format_part(node: ast.expr, scope: Mapping[str, object]) -> object:
    if isinstance(node, ast.FormattedValue):
        if node.conversion != -1 or node.format_spec is not None:
            return UNKNOWN
        value = _evaluate(node.value, scope)
        return value if isinstance(value, str) else UNKNOWN
    return _evaluate(node, scope)


def _model_name(class_name: str, attrs: Mapping[str, object]) -> str | None:
    """The model a class defines or extends, derived like ``MetaModel.__new__``.

    ``_name`` if the class sets it; otherwise the single ``_inherit`` entry, or the
    class name when ``_inherit`` lists several models. ``None`` when that cannot be
    known statically, and for classes that set neither attribute: MetaModel would
    name a model after the class, but such classes are usually plain Python bases.
    """
    if "_name" in attrs:
        name = attrs["_name"]
        return name if isinstance(name, str) else None
    inherit = attrs.get("_inherit", UNKNOWN)
    if isinstance(inherit, str):
        return inherit
    if not isinstance(inherit, list | tuple):
        return None
    if len(inherit) == 1:
        return inherit[0] if isinstance(inherit[0], str) else None
    return class_name


def _as_names(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def referenced_models(tree: ast.Module) -> set[str]:
    """Model names used as literals in ``env[...]`` and relational field comodels."""
    models: set[str] = set()
    for node in _inner_nodes(tree):
        if isinstance(node, ast.Subscript) and _is_env(node.value):
            name = _string(node.slice)
        elif isinstance(node, ast.Call) and _callee_name(node.func) in RELATIONAL_FIELDS:
            name = _comodel_argument(node)
        else:
            continue
        if name:
            models.add(name)
    return models


def _inner_nodes(tree: ast.AST) -> list[ast.AST]:
    """Return every node except the leaves (names, constants, contexts, operators).

    This runs on every file mypy loads. ``ast.walk`` also visits the leaves, about
    half of all nodes (each ``Name`` has its own ``Load()`` child), and its nested
    generators cost more than the work itself; no leaf can be or contain a
    subscript or a call. The checks are inlined on purpose (hot loop).
    """
    nodes: list[ast.AST] = []
    stack: list[ast.AST] = [tree]
    node_type, leaves = ast.AST, LEAF_NODES
    while stack:
        node = stack.pop()
        nodes.append(node)
        for name in node._fields:
            value = getattr(node, name, None)
            if isinstance(value, list):
                stack += [x for x in value if isinstance(x, node_type) and type(x) not in leaves]
            elif isinstance(value, node_type) and type(value) not in leaves:
                stack.append(value)
    return nodes


def _is_env(node: ast.expr) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "env") or (
        isinstance(node, ast.Name) and node.id == "env"
    )


def _callee_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _comodel_argument(call: ast.Call) -> str | None:
    if call.args:
        return _string(call.args[0])
    for keyword in call.keywords:
        if keyword.arg == "comodel_name":
            return _string(keyword.value)
    return None


def _string(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None
