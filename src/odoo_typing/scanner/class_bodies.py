"""What the body of a class says about its model, derived like ``odoo.models.MetaModel``.

The model name and its ``_inherit`` parents, the methods, the models it delegates to
(``_inherits``), ``_log_access`` and whether the class is abstract.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from typing import Final

from odoo_typing.scanner.evaluation import UNKNOWN
from odoo_typing.scanner.syntax import RELATIONAL_FIELDS, callee_name, comodel_argument

__all__ = [
    "abstract",
    "as_names",
    "declared_comodels",
    "defined_methods",
    "delegated_models",
    "inherits_pairs",
    "log_access",
    "model_name",
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
