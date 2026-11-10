"""Static scan of an addon's Python files for Odoo model classes.

Only files reachable through the addon's ``__init__.py`` imports are scanned, in
import order, which is the order in which Odoo registers the classes.

Sources are parsed from bytes, like Python and mypy do, so a UTF-8 BOM or a PEP 263
coding cookie is honoured.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from odoo_typing.addons import Addon

__all__ = [
    "AddonScan",
    "Contribution",
    "ModuleScan",
    "SourceError",
    "parse_source",
    "referenced_models",
    "scan_addon",
]

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
        position: Sort key ``(addon load order, file order, line)``.
    """

    addon: str
    module: str
    class_name: str
    model: str
    parents: tuple[str, ...]
    position: tuple[int, int, int]

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


def scan_addon(addon: Addon, addon_order: int) -> AddonScan:
    """Scan all Python files of an addon that its ``__init__.py`` imports.

    Args:
        addon: Addon to scan.
        addon_order: Position of the addon in the global load order.

    Returns:
        The scanned files in import order, and the files that could not be parsed.
    """
    walker = _ImportWalker(addon, addon_order)
    walker.visit(addon.path / "__init__.py", addon.module_prefix, is_package=True)
    return AddonScan(modules=tuple(walker.scans), errors=tuple(walker.errors))


class _ImportWalker:
    def __init__(self, addon: Addon, addon_order: int) -> None:
        self.addon = addon
        self.addon_order = addon_order
        self.scans: list[ModuleScan] = []
        self.errors: list[SourceError] = []
        self._seen: set[Path] = set()

    def visit(self, path: Path, module: str, *, is_package: bool) -> None:
        if path in self._seen or not path.is_file():
            return
        self._seen.add(path)
        try:
            tree = parse_source(path)
        except SourceError as error:
            self.errors.append(error)
            return
        file_order = len(self.scans)
        self.scans.append(
            ModuleScan(
                module=module,
                path=path,
                contributions=tuple(self._contributions(tree, module, file_order)),
                referenced_models=frozenset(referenced_models(tree)),
            )
        )
        package = module if is_package else module.rpartition(".")[0]
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.level > 0:
                self._follow(node, package, path.parent)

    def _follow(self, node: ast.ImportFrom, package: str, directory: Path) -> None:
        for _ in range(node.level - 1):
            package, directory = package.rpartition(".")[0], directory.parent
        if node.module:
            for part in node.module.split("."):
                package, directory = f"{package}.{part}", directory / part
            self._visit_module(package, directory)
        for alias in node.names:
            self._visit_module(f"{package}.{alias.name}", directory / alias.name)

    def _visit_module(self, module: str, base: Path) -> None:
        if (base / "__init__.py").is_file():
            self.visit(base / "__init__.py", module, is_package=True)
        else:
            self.visit(base.with_suffix(".py"), module, is_package=False)

    def _contributions(self, tree: ast.Module, module: str, file_order: int) -> list[Contribution]:
        result = []
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            attrs = _class_attributes(node)
            if attrs.get("_register") is False:
                continue
            name = attrs.get("_name")
            parents = _as_names(attrs.get("_inherit"))
            model = name if isinstance(name, str) else (parents[0] if parents else None)
            if model is None:
                continue
            result.append(
                Contribution(
                    addon=self.addon.name,
                    module=module,
                    class_name=node.name,
                    model=model,
                    parents=parents,
                    position=(self.addon_order, file_order, node.lineno),
                )
            )
        return result


def _class_attributes(node: ast.ClassDef) -> dict[str, object]:
    """Literal values of simple ``_attr = <literal>`` assignments in a class body."""
    attrs: dict[str, object] = {}
    for statement in node.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target, value = statement.target, statement.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id.startswith("_"):
            try:
                attrs[target.id] = ast.literal_eval(value)
            except ValueError:
                continue
    return attrs


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
        for field in node._fields:
            value = getattr(node, field, None)
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
