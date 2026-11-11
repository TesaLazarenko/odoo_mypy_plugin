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
    "FieldInfo",
    "ModuleScan",
    "SourceError",
    "parse_source",
    "referenced_models",
    "scan_addon",
]

UNKNOWN: Final = object()  # value of an expression the scan cannot evaluate
RELATIONAL_FIELDS = frozenset({"Many2one", "One2many", "Many2many"})
# Bases of the classes of models that have a table (``_abstract = False``).
CONCRETE_BASES: Final = frozenset({"Model", "TransientModel"})
# Environment attributes that are records of a model (``self.env.user``).
ENV_RECORDS: Final = {"user": "res.users", "company": "res.company", "companies": "res.company"}
# Log-access fields that are res.users records (``record.create_uid``).
LOG_ACCESS_USERS: Final = frozenset({"create_uid", "write_uid"})
# Positional parameters of the field classes (Odoo 18-20); ``string`` for the others.
POSITIONAL_PARAMETERS: Final = {
    "Many2one": ("comodel_name", "string"),
    "One2many": ("comodel_name", "inverse_name", "string"),
    "Many2many": ("comodel_name", "relation", "column1", "column2", "string"),
    "Selection": ("selection", "string"),
    "Reference": ("selection", "string"),
    "Float": ("string", "digits"),
    "Monetary": ("string", "currency_field"),
}
# Field arguments naming a method of the model, by name or as the function itself.
METHOD_ARGUMENTS: Final = ("compute", "inverse", "search")
FLAG_ARGUMENTS: Final = ("readonly", "required", "store", "delegate")
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
class FieldInfo:
    """One field definition in a class body (``name = fields.Char(...)``).

    Each attribute is the argument the definition passes, ``None`` if it passes none:
    Odoo merges the definitions of a field argument by argument (see
    :mod:`odoo_typing.model_fields`). An argument whose value the scan cannot evaluate
    (an imported constant, a lambda) stays ``None`` and is named in ``dynamic``.

    Attributes:
        name: Field name.
        field_type: Name of the field class (``Char``, ``Many2one``).
        comodel: ``comodel_name`` of a relational field.
        inverse_name: ``inverse_name`` of a ``One2many``.
        selection: Keys of a ``selection`` given as a list of pairs.
        selection_method: Name of the method of a ``selection`` given as a method.
        selection_add: Keys a ``selection_add`` adds.
        related: ``related`` path.
        compute: Name of the ``compute`` method.
        inverse: Name of the ``inverse`` method.
        search: Name of the ``search`` method.
        readonly: ``readonly`` flag.
        required: ``required`` flag.
        store: ``store`` flag.
        delegate: ``delegate`` flag (``Many2one``).
        dynamic: Arguments passed with values the scan cannot evaluate.
    """

    name: str
    field_type: str
    comodel: str | None = None
    inverse_name: str | None = None
    selection: tuple[str, ...] | None = None
    selection_method: str | None = None
    selection_add: tuple[str, ...] | None = None
    related: str | None = None
    compute: str | None = None
    inverse: str | None = None
    search: str | None = None
    readonly: bool | None = None
    required: bool | None = None
    store: bool | None = None
    delegate: bool | None = None
    dynamic: frozenset[str] = frozenset()


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
        fields: The fields the class body declares (``x = fields.Char()``), in source
            order.
        methods: Names of the methods the class body defines.
        delegates: Models the class delegates fields to: the keys of ``_inherits``,
            then the comodels of ``fields.Many2one(..., delegate=True)``.
        inherits: ``_inherits`` of the class body: (model, field name) pairs.
        log_access: ``_log_access`` as the class body sets it, ``False`` if it only
            sets ``_auto = False`` (Odoo's default follows ``_auto``), ``None`` if
            neither is set.
        abstract: Whether the class is abstract: ``_abstract`` of its body, else whether
            it derives from ``AbstractModel`` rather than ``Model`` / ``TransientModel``;
            ``None`` if neither tells.
    """

    addon: str
    module: str
    class_name: str
    model: str
    parents: tuple[str, ...]
    position: tuple[int, str, int]
    comodels: tuple[str, ...] = ()
    fields: tuple[FieldInfo, ...] = ()
    methods: tuple[str, ...] = ()
    delegates: tuple[str, ...] = ()
    inherits: tuple[tuple[str, str], ...] = ()
    log_access: bool | None = None
    abstract: bool | None = None

    @property
    def fullname(self) -> str:
        """Fully qualified class name."""
        return f"{self.module}.{self.class_name}"

    @property
    def field_names(self) -> tuple[str, ...]:
        """Names of the fields the class body declares, in source order."""
        return tuple(info.name for info in self.fields)


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
        self._names: dict[str, dict[str, object]] = {}  # module-level names of each module
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
        self._names[module] = file.names
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
        parts = self._inner_parts(dotted)
        if parts is None:
            return
        self._import(dotted)
        source = self._names.get(".".join([self.addon.module_prefix, *parts]), {})
        for alias in node.names:
            self._import([*dotted, alias.name])  # runs only if it is a submodule
            if alias.name in source:  # ``from .consts import STATES``: a known value
                file.names[alias.asname or alias.name] = source[alias.name]

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
        methods = _defined_methods(node)
        inherits = attrs.get("_inherits")
        scope = ChainMap(attrs, file.names)
        file.contributions.append(
            Contribution(
                addon=self.addon.name,
                module=file.module,
                class_name=node.name,
                model=model,
                parents=_as_names(attrs.get("_inherit")),
                position=(*self.load_key, len(self._classes)),
                comodels=_declared_comodels(node),
                fields=_declared_fields(node, scope, frozenset(methods)),
                methods=methods,
                delegates=_delegated_models(node, inherits),
                inherits=_inherits_pairs(inherits),
                log_access=_log_access(attrs),
                abstract=_abstract(node, attrs),
            )
        )


def _log_access(attrs: dict[str, object]) -> bool | None:
    """``_log_access`` of a class body; MetaModel defaults it to ``_auto``."""
    value = attrs.get("_log_access", attrs.get("_auto"))
    return value if isinstance(value, bool) else None


def _abstract(node: ast.ClassDef, attrs: dict[str, object]) -> bool | None:
    """Whether a class is abstract (see :attr:`Contribution.abstract`)."""
    value = attrs.get("_abstract")
    if isinstance(value, bool):
        return value
    kinds = {_callee_name(base) for base in node.bases}
    if kinds & CONCRETE_BASES:
        return False
    return True if "AbstractModel" in kinds else None


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


def _declared_fields(
    node: ast.ClassDef, scope: Mapping[str, object], methods: frozenset[str]
) -> tuple[FieldInfo, ...]:
    """The fields assigned ``fields.<Type>(...)`` in a class body (also annotated).

    A name assigned twice keeps its last definition, as the class namespace does.
    """
    found: dict[str, FieldInfo] = {}
    for statement in node.body:
        target = _single_target(statement)
        value = statement.value if isinstance(statement, ast.Assign | ast.AnnAssign) else None
        if target is not None and isinstance(value, ast.Call) and _is_fields_call(value):
            found.pop(target, None)
            found[target] = _field_info(target, value, scope, methods)
    return tuple(found.values())


def _field_info(
    name: str, call: ast.Call, scope: Mapping[str, object], methods: frozenset[str]
) -> FieldInfo:
    """What one ``fields.<Type>(...)`` call passes, as far as the scan can evaluate it."""
    field_type = _callee_name(call.func) or ""
    arguments = _field_arguments(field_type, call)
    dynamic: set[str] = set()

    def text(key: str) -> str | None:
        return _text_argument(arguments, key, scope, dynamic)

    def method(key: str) -> str | None:
        if key not in arguments:
            return None
        node = arguments[key]
        if isinstance(node, ast.Name) and node.id in methods:
            return node.id
        return _text_argument(arguments, key, scope, dynamic)

    def flag(key: str) -> bool | None:
        if key not in arguments:
            return None
        value = _evaluate(arguments[key], scope)
        if isinstance(value, bool):
            return value
        dynamic.add(key)
        return None

    selection, selection_method = _selection(arguments, scope, methods, dynamic)
    if "comodel_name" in dynamic:  # attribute names, not argument names
        dynamic.discard("comodel_name")
        dynamic.add("comodel")
    return FieldInfo(
        name=name,
        field_type=field_type,
        comodel=text("comodel_name"),
        inverse_name=text("inverse_name"),
        selection=selection,
        selection_method=selection_method,
        selection_add=_selection_keys(arguments, "selection_add", scope, dynamic),
        related=text("related"),
        compute=method("compute"),
        inverse=method("inverse"),
        search=method("search"),
        readonly=flag("readonly"),
        required=flag("required"),
        store=flag("store"),
        delegate=flag("delegate"),
        dynamic=frozenset(dynamic),
    )


def _field_arguments(field_type: str, call: ast.Call) -> dict[str, ast.expr]:
    """Arguments of a field call by parameter name (positional ones up to a ``*args``)."""
    arguments: dict[str, ast.expr] = {}
    parameters = POSITIONAL_PARAMETERS.get(field_type, ("string",))
    for parameter, node in zip(parameters, call.args, strict=False):
        if isinstance(node, ast.Starred):
            break
        arguments[parameter] = node
    for keyword in call.keywords:
        if keyword.arg is not None:  # ``**kwargs`` cannot be read
            arguments[keyword.arg] = keyword.value
    return arguments


def _text_argument(
    arguments: Mapping[str, ast.expr], key: str, scope: Mapping[str, object], dynamic: set[str]
) -> str | None:
    if key not in arguments:
        return None
    value = _evaluate(arguments[key], scope)
    if isinstance(value, str):
        return value
    dynamic.add(key)
    return None


def _selection(
    arguments: Mapping[str, ast.expr],
    scope: Mapping[str, object],
    methods: frozenset[str],
    dynamic: set[str],
) -> tuple[tuple[str, ...] | None, str | None]:
    """``selection`` as (keys of a list, name of a method); both ``None`` if not given."""
    node = arguments.get("selection")
    if node is None:
        return None, None
    if isinstance(node, ast.Name) and node.id in methods:
        return None, node.id
    value = _evaluate(node, scope)
    if isinstance(value, str):
        return None, value
    return _selection_keys(arguments, "selection", scope, dynamic), None


def _selection_keys(
    arguments: Mapping[str, ast.expr], key: str, scope: Mapping[str, object], dynamic: set[str]
) -> tuple[str, ...] | None:
    """Keys of a list of ``(key, label)`` pairs (``selection_add`` also allows ``(key,)``)."""
    if key not in arguments:
        return None
    value = _evaluate(arguments[key], scope)
    if isinstance(value, list | tuple):
        keys = [pair[0] if isinstance(pair, list | tuple) and pair else UNKNOWN for pair in value]
        if all(isinstance(item, str) for item in keys):
            return tuple(item for item in keys if isinstance(item, str))
    dynamic.add(key)
    return None


def _defined_methods(node: ast.ClassDef) -> tuple[str, ...]:
    """Names of the functions a class body defines, in source order.

    Also ``alias = method`` and ``name = module.function`` assignments
    (``_check_company_domain = models.check_company_domain_parent_of``). Private
    names get Python's mangling (``__x`` in ``Order`` is ``_Order__x``).
    """
    names: dict[str, None] = {}
    for statement in node.body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            names[_mangled(statement.name, node.name)] = None
            continue
        if not isinstance(statement, ast.Assign):
            continue
        value = statement.value
        aliased = isinstance(value, ast.Name) and _mangled(value.id, node.name) in names
        function = isinstance(value, ast.Attribute) and value.attr[:1].islower()
        if aliased or function:  # also ``a = b = method``
            targets = (target for target in statement.targets if isinstance(target, ast.Name))
            names.update(dict.fromkeys(_mangled(target.id, node.name) for target in targets))
    return tuple(names)


def _mangled(name: str, class_name: str) -> str:
    """A name as the class namespace stores it (private name mangling)."""
    if name.startswith("__") and not name.endswith("__") and class_name.strip("_"):
        return f"_{class_name.lstrip('_')}{name}"
    return name


def _inherits_pairs(inherits: object) -> tuple[tuple[str, str], ...]:
    """The (model, field name) pairs of an ``_inherits`` dict, in order."""
    if not isinstance(inherits, dict):
        return ()
    return tuple(
        (model, name)
        for model, name in inherits.items()
        if isinstance(model, str) and isinstance(name, str)
    )


def _delegated_models(node: ast.ClassDef, inherits: object) -> tuple[str, ...]:
    """Models a class delegates to: ``_inherits`` keys, then ``delegate=True`` comodels."""
    models: dict[str, None] = {}
    if isinstance(inherits, dict):
        models.update(dict.fromkeys(key for key in inherits if isinstance(key, str)))
    for statement in node.body:
        value = statement.value if isinstance(statement, ast.Assign | ast.AnnAssign) else None
        if isinstance(value, ast.Call) and _is_delegate_field(value):
            comodel = _comodel_argument(value)
            if comodel:
                models[comodel] = None
    return tuple(models)


def _single_target(statement: ast.stmt) -> str | None:
    """The name assigned by ``x = ...`` or ``x: T = ...`` (one plain name)."""
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target: ast.expr = statement.targets[0]
    elif isinstance(statement, ast.AnnAssign):
        target = statement.target
    else:
        return None
    return target.id if isinstance(target, ast.Name) else None


def _is_fields_call(call: ast.Call) -> bool:
    """``fields.Char(...)`` and the like."""
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "fields"
        and func.attr[:1].isupper()
    )


def _is_delegate_field(call: ast.Call) -> bool:
    """``fields.Many2one(..., delegate=True)``."""
    return _callee_name(call.func) == "Many2one" and any(
        keyword.arg == "delegate"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )


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

    Handles literals, lists, tuples and dicts, names bound earlier, ``+`` of strings,
    lists or tuples and f-strings of known values: what model names, ``_inherit`` lists and
    ``_inherits`` dicts are made of.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Dict):
        return _evaluate_dict(node, scope)
    if isinstance(node, ast.Name):
        return scope.get(node.id, UNKNOWN)
    if isinstance(node, ast.List | ast.Tuple):
        items = [_evaluate(item, scope) for item in node.elts]
        return items if isinstance(node, ast.List) else tuple(items)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _concatenate(_evaluate(node.left, scope), _evaluate(node.right, scope))
    if isinstance(node, ast.JoinedStr):
        parts = [_format_part(part, scope) for part in node.values]
        strings = [part for part in parts if isinstance(part, str)]
        return "".join(strings) if len(strings) == len(parts) else UNKNOWN
    return UNKNOWN


def _concatenate(left: object, right: object) -> object:
    """``left + right`` of two strings, lists or tuples (``STATES + [("x", "X")]``)."""
    if isinstance(left, str) and isinstance(right, str):
        return left + right
    if isinstance(left, list) and isinstance(right, list):
        return left + right
    if isinstance(left, tuple) and isinstance(right, tuple):
        return left + right
    return UNKNOWN


def _evaluate_dict(node: ast.Dict, scope: Mapping[str, object]) -> object:
    """Value of a dict display whose keys are all known (``**`` unpacking is not)."""
    keys = [_evaluate(key, scope) if key is not None else UNKNOWN for key in node.keys]
    if any(key is UNKNOWN or not isinstance(key, str) for key in keys):
        return UNKNOWN
    return {key: _evaluate(value, scope) for key, value in zip(keys, node.values, strict=True)}


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
    """Model names a file uses: literals in ``env[...]`` and relational field comodels.

    ``env[NAME]`` counts when ``NAME`` is a module-level string constant, and
    ``env.user`` / ``env.company`` / ``env.companies`` name ``res.users`` and
    ``res.company``, and so do ``.create_uid`` / ``.write_uid`` (``res.users``).
    """
    constants = _string_constants(tree)
    models: set[str] = set()
    for node in _inner_nodes(tree):
        if isinstance(node, ast.Subscript) and _is_env(node.value):
            key = node.slice
            name = constants.get(key.id) if isinstance(key, ast.Name) else _string(key)
        elif isinstance(node, ast.Call) and _callee_name(node.func) in RELATIONAL_FIELDS:
            name = _comodel_argument(node)
        elif isinstance(node, ast.Attribute) and node.attr in ENV_RECORDS and _is_env(node.value):
            name = ENV_RECORDS[node.attr]
        elif isinstance(node, ast.Attribute) and node.attr in LOG_ACCESS_USERS:
            name = "res.users"  # ``record.create_uid``: a res.users record
        else:
            continue
        if name:
            models.add(name)
    return models


def _string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME: Final = "text"`` constants (mypy knows their value)."""
    constants: dict[str, str] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.AnnAssign) or not _is_final(statement.annotation):
            continue
        name = _single_target(statement)
        text = _string(statement.value) if statement.value is not None else None
        if name is not None and text is not None:
            constants[name] = text
    return constants


def _is_final(annotation: ast.expr) -> bool:
    """``Final``, ``typing.Final`` or ``Final[str]``."""
    if isinstance(annotation, ast.Subscript):
        annotation = annotation.value
    return _callee_name(annotation) == "Final"


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
