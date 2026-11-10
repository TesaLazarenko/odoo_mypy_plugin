"""Static scan of an addon's Python files for Odoo model classes.

Only files reachable through the addon's ``__init__.py`` imports are scanned, in
import order, which is the order in which Odoo registers the classes.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from odoo_typing.addons import Addon

__all__ = ["Contribution", "ModuleScan", "referenced_models", "scan_addon"]

RELATIONAL_FIELDS = frozenset({"Many2one", "One2many", "Many2many"})


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


def scan_addon(addon: Addon, addon_order: int) -> list[ModuleScan]:
    """Scan all Python files of an addon that its ``__init__.py`` imports.

    Args:
        addon: Addon to scan.
        addon_order: Position of the addon in the global load order.

    Returns:
        One entry per imported file, in import order.
    """
    walker = _ImportWalker(addon, addon_order)
    walker.visit(addon.path / "__init__.py", addon.module_prefix, is_package=True)
    return walker.scans


class _ImportWalker:
    def __init__(self, addon: Addon, addon_order: int) -> None:
        self.addon = addon
        self.addon_order = addon_order
        self.scans: list[ModuleScan] = []
        self._seen: set[Path] = set()

    def visit(self, path: Path, module: str, *, is_package: bool) -> None:
        if path in self._seen or not path.is_file():
            return
        self._seen.add(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
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
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_env(node.value):
            name = _string(node.slice)
        elif isinstance(node, ast.Call) and _callee_name(node.func) in RELATIONAL_FIELDS:
            name = _comodel_argument(node)
        else:
            continue
        if name:
            models.add(name)
    return models


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
