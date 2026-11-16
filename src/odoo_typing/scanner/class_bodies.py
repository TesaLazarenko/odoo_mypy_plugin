"""What the body of a class says about its model, derived like ``odoo.models.MetaModel``.

The model name and its ``_inherit`` parents, the methods, the models it delegates to
(``_inherits``), ``_log_access`` and whether the class is abstract.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Mapping
from typing import Final

from odoo_typing.scanner.evaluation import UNKNOWN
from odoo_typing.scanner.syntax import RELATIONAL_FIELDS, callee_name, comodel_argument

__all__ = [
    "NAME_TEST",
    "abstract",
    "as_names",
    "declared_comodels",
    "defined_methods",
    "delegated_models",
    "inherits_pairs",
    "log_access",
    "model_name",
    "read_value_keys",
    "record_dispatches",
]

# Bases of the classes of models that have a table (``_abstract = False``).
CONCRETE_BASES: Final = frozenset({"Model", "TransientModel"})


def model_name(class_name: str, attrs: Mapping[str, object]) -> str | None:
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


def as_names(value: object) -> tuple[str, ...]:
    """The model names of an ``_inherit`` value: one name, or a list of them."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def defined_methods(node: ast.ClassDef) -> tuple[str, ...]:
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


def declared_comodels(node: ast.ClassDef) -> tuple[str, ...]:
    """Comodel literals of the relational fields assigned in a class body."""
    comodels: dict[str, None] = {}
    for statement in node.body:
        value = statement.value if isinstance(statement, ast.Assign | ast.AnnAssign) else None
        if isinstance(value, ast.Call) and callee_name(value.func) in RELATIONAL_FIELDS:
            comodel = comodel_argument(value)
            if comodel:
                comodels[comodel] = None
    return tuple(comodels)


def delegated_models(node: ast.ClassDef, inherits: object) -> tuple[str, ...]:
    """Models a class delegates to: ``_inherits`` keys, then ``delegate=True`` comodels."""
    models: dict[str, None] = {}
    if isinstance(inherits, dict):
        models.update(dict.fromkeys(key for key in inherits if isinstance(key, str)))
    for statement in node.body:
        value = statement.value if isinstance(statement, ast.Assign | ast.AnnAssign) else None
        if isinstance(value, ast.Call) and _is_delegate_field(value):
            comodel = comodel_argument(value)
            if comodel:
                models[comodel] = None
    return tuple(models)


def _is_delegate_field(call: ast.Call) -> bool:
    """``fields.Many2one(..., delegate=True)``."""
    return callee_name(call.func) == "Many2one" and any(
        keyword.arg == "delegate"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )


def inherits_pairs(inherits: object) -> tuple[tuple[str, str], ...]:
    """The (model, field name) pairs of an ``_inherits`` dict, in order."""
    if not isinstance(inherits, dict):
        return ()
    return tuple(
        (model, name)
        for model, name in inherits.items()
        if isinstance(model, str) and isinstance(name, str)
    )


def log_access(attrs: dict[str, object]) -> bool | None:
    """``_log_access`` of a class body; MetaModel defaults it to ``_auto``."""
    value = attrs.get("_log_access", attrs.get("_auto"))
    return value if isinstance(value, bool) else None


def abstract(node: ast.ClassDef, attrs: dict[str, object]) -> bool | None:
    """Whether a class is abstract (see :attr:`Contribution.abstract`)."""
    value = attrs.get("_abstract")
    if isinstance(value, bool):
        return value
    kinds = {callee_name(base) for base in node.bases}
    if kinds & CONCRETE_BASES:
        return False
    return True if "AbstractModel" in kinds else None


# Methods whose first parameter is the values a caller gives (``create(vals_list)``).
VALUES_METHODS: Final = frozenset({"create", "write"})
# Calls of a dict that read a key: ``vals.pop("k", None)``, ``vals.get("k")``.
KEY_READS: Final = frozenset({"pop", "get", "setdefault"})


def read_value_keys(node: ast.ClassDef) -> tuple[str, ...]:
    """The keys the ``create`` / ``write`` of a class body read from the values given.

    ``vals.pop("counterpart_account_id", None)`` in ``account.bank.statement.line``'s
    ``create`` takes a key that is no field: the override consumes it before Odoo sees
    the values. Read are ``vals.pop("k")`` / ``.get`` / ``.setdefault``, ``vals["k"]``,
    ``"k" in vals`` and ``del vals["k"]`` of the first parameter, or of a name a ``for``
    binds to its items (``for vals in vals_list:``).
    """
    keys: dict[str, None] = {}
    for statement in node.body:
        if not isinstance(statement, ast.FunctionDef) or statement.name not in VALUES_METHODS:
            continue
        parameters = statement.args.posonlyargs + statement.args.args
        if len(parameters) < 2:
            continue
        names = {parameters[1].arg}
        nodes = list(ast.walk(statement))
        for item in nodes:
            if isinstance(item, (ast.For, ast.comprehension)) and _names_of(item.iter) & names:
                names |= _names_of(item.target)
        for item in nodes:
            key = _read_key(item, names)
            if key is not None:
                keys[key] = None
    return tuple(keys)


def _names_of(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return {name for item in node.elts for name in _names_of(item)}
    return set()


def _read_key(node: ast.AST, names: set[str]) -> str | None:
    """The key a node reads from one of some dicts, if it reads one."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in KEY_READS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in names
        and node.args
    ):
        return _text(node.args[0])
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, (ast.Load, ast.Del))  # not ``vals["k"] = x``
        and isinstance(node.value, ast.Name)
        and node.value.id in names
    ):
        return _text(node.slice)
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], (ast.In, ast.NotIn))
        and isinstance(node.comparators[0], ast.Name)
        and node.comparators[0].id in names
    ):
        return _text(node.left)
    return None


def _text(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


# A comparison of a name's ``_name`` (``source_ref._name == "mail.template"``).
NAME_TEST: Final = re.compile(rb"\w\._name\s*(?:[=!]=|(?:not\s+)?in\b)")
NAME_OPERATORS: Final = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)


def record_dispatches(node: ast.ClassDef) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """The parameters of a class's methods that take records of the models they test.

    ``_get_source_from_ref(self, source_ref)`` of ``mail.thread`` tests
    ``source_ref._name == "mail.template"`` (and ``"ir.ui.view"``): the parameter takes
    records of those models, whatever it is annotated with. So does a parameter that a
    method passes on to such a parameter of a method of the class
    (``message_post_with_source(source_ref)`` calls ``self._get_source_from_ref(source_ref)``).

    Returns:
        ``(method, parameter, models)`` for each such parameter.
    """
    methods = {
        statement.name: statement
        for statement in node.body
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    positional = {
        name: [argument.arg for argument in (f.args.posonlyargs + f.args.args)[1:]]
        for name, f in methods.items()
    }
    tested: dict[tuple[str, str], set[str]] = {}
    passed: list[tuple[str, str, str, str]] = []  # method, parameter, callee, its parameter
    for name, function in methods.items():
        receiver = function.args.args[0].arg if function.args.args else None
        own = {*positional[name], *(argument.arg for argument in function.args.kwonlyargs)}
        for item in ast.walk(function):
            for parameter, models in _name_tests(item, own):
                tested.setdefault((name, parameter), set()).update(models)
            callee = _own_method(item, receiver, methods)
            if callee is not None and isinstance(item, ast.Call):
                passed += _passed(name, item, own, positional[callee])
    changed = True
    while changed:  # through the methods a method calls, to a fixed point
        changed = False
        for name, parameter, callee, target in passed:
            found = tested.get((callee, target), set())
            known = tested.setdefault((name, parameter), set())
            if not found <= known:
                known |= found
                changed = True
    return tuple(
        (method, parameter, tuple(sorted(models)))
        for (method, parameter), models in tested.items()
        if models
    )


def _own_method(node: ast.AST, receiver: str | None, methods: Mapping[str, object]) -> str | None:
    """The method of the class that a node calls on ``self``, if it is such a call."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == receiver
        and node.func.attr in methods
    ):
        return node.func.attr
    return None


def _name_tests(node: ast.AST, parameters: set[str]) -> Iterator[tuple[str, set[str]]]:
    """``p._name == "m"`` / ``"m" == p._name`` / ``p._name in ("m", "n")`` of a parameter."""
    if not isinstance(node, ast.Compare):
        return
    if len(node.ops) != 1 or not isinstance(node.ops[0], NAME_OPERATORS):
        return
    for tested, other in ((node.left, node.comparators[0]), (node.comparators[0], node.left)):
        if (
            isinstance(tested, ast.Attribute)
            and tested.attr == "_name"
            and isinstance(tested.value, ast.Name)
            and tested.value.id in parameters
        ):
            items = other.elts if isinstance(other, ast.Tuple | ast.List | ast.Set) else [other]
            models = {
                item.value
                for item in items
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            }
            if models:
                yield tested.value.id, models


def _passed(
    name: str, call: ast.Call, parameters: set[str], callee_parameters: list[str]
) -> list[tuple[str, str, str, str]]:
    """The parameters a call of a method of the class passes on, and where they go."""
    callee = call.func.attr if isinstance(call.func, ast.Attribute) else ""
    found = [
        (name, argument.id, callee, callee_parameters[position])
        for position, argument in enumerate(call.args)
        if isinstance(argument, ast.Name)
        and argument.id in parameters
        and position < len(callee_parameters)
    ]
    found += [
        (name, keyword.value.id, callee, keyword.arg)
        for keyword in call.keywords
        if keyword.arg is not None
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id in parameters
    ]
    return found
