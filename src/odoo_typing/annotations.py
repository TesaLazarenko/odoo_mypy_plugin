"""``odoo.values.*`` and ``odoo.model.*`` annotations (the PyCharm plugin "Odoo IDE"'s).

``Annotated[dict, "odoo.values.res_partner"]`` and the bare strings
``"odoo.values.res_partner"`` / ``"odoo.model.res_partner"`` stand for the ``vals`` of a
model and for its records (:mod:`odoo_typing.hooks.annotations` gives the types).

mypy cannot look a bare string up: it would need a name ``odoo`` in the module and a
module ``odoo.values`` (Odoo has neither). So the annotations of a module that uses
them are rewritten (:func:`rewrite_annotations`) when mypy's semantic analysis reaches
the module's first regular top-level class (the customize-MRO hook, which mypy calls
before a class's body on every path that analyzes a module from source, though not for
a class-syntax ``TypedDict`` / ``NamedTuple`` or a class whose bases it rejects; an
annotation analyzed before it waits for the next pass, as mypy defers a name it cannot
find yet). The name
becomes ``__build_class__["values", "res_partner"]``, a name of ``builtins`` (visible in
every module, and never an annotation of its own) whose type-analysis hook the plugin
owns. A module without such a class is never rewritten: mypy reports
``Name "odoo" is not defined`` there, and the ``Annotated`` form is the one that works.
Only the annotations of statements are rewritten (functions, variables, type comments):
a string inside an expression (``cast("odoo.values.x", d)``) is turned into a type by
mypy later, so there too the ``Annotated`` form is the one that works. A type alias is
a plain ``dict`` in either form: mypy drops the metadata of ``Annotated`` there.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Final

from mypy.nodes import (
    AssignmentStmt,
    Block,
    ClassDef,
    Decorator,
    ForStmt,
    FuncDef,
    IfStmt,
    MatchStmt,
    MypyFile,
    OverloadedFuncDef,
    Statement,
    TryStmt,
    WhileStmt,
    WithStmt,
)
from mypy.types import (
    AnyType,
    CallableType,
    Instance,
    NoneType,
    RawExpressionType,
    Type,
    TypeList,
    TypeOfAny,
    UnboundType,
    UnionType,
)

from odoo_typing.registry.annotations import UNCHECKED
from odoo_typing.scanner import MODEL, VALUES, AnnotationAlias
from odoo_typing.vals import FALSE, NEW_ID, NONE, RECORDS, VALUE_TYPES

__all__ = [
    "ANNOTATED_NAMES",
    "TRIGGER",
    "TRIGGER_FULLNAME",
    "ItemTypes",
    "annotation_alias",
    "rewrite_annotations",
    "trigger_alias",
]

ANNOTATED_NAMES: Final = ("typing.Annotated", "typing_extensions.Annotated")
TRIGGER: Final = "__build_class__"
TRIGGER_FULLNAME: Final = f"builtins.{TRIGGER}"
PREFIXES: Final = {f"odoo.{VALUES}.": VALUES, f"odoo.{MODEL}.": MODEL}
STR: Final = "builtins.str"


def annotation_alias(name: str) -> AnnotationAlias | None:
    """``("values", "res_partner")`` for ``odoo.values.res_partner``; ``None`` otherwise."""
    for prefix, kind in PREFIXES.items():
        if name.startswith(prefix) and len(name) > len(prefix):
            return kind, name[len(prefix) :]
    return None


def trigger_alias(typ: UnboundType) -> AnnotationAlias | None:
    """The ``(kind, alias)`` a rewritten annotation (``__build_class__[...]``) carries."""
    if len(typ.args) != 2:
        return None
    parts = [arg.literal_value for arg in typ.args if isinstance(arg, RawExpressionType)]
    if len(parts) != 2 or not all(isinstance(part, str) for part in parts):
        return None
    kind, alias = parts
    if kind not in (VALUES, MODEL) or not isinstance(alias, str):
        return None
    return str(kind), alias


def rewrite_annotations(tree: MypyFile) -> None:
    """Rewrite the ``odoo.values.*`` / ``odoo.model.*`` annotations of a parsed module.

    Called during mypy's semantic analysis (see the module docstring); the annotations
    analyzed already keep their types, and rewriting a module again changes nothing.
    """
    _rewrite_statements(tree.defs)


def _rewrite_statements(statements: Iterable[Statement]) -> None:
    for statement in statements:
        if isinstance(statement, OverloadedFuncDef):
            _rewrite_statements(statement.items)
            if statement.impl is not None:
                _rewrite_statements([statement.impl])
        elif isinstance(statement, Decorator):
            _rewrite_function(statement.func)
        elif isinstance(statement, FuncDef):
            _rewrite_function(statement)
        elif isinstance(statement, AssignmentStmt):
            statement.type = _rewrite(statement.type)
            statement.unanalyzed_type = _rewrite(statement.unanalyzed_type)
        elif isinstance(statement, ClassDef):
            _rewrite_statements(statement.defs.body)
        else:
            _rewrite_compound(statement)


def _rewrite_function(function: FuncDef) -> None:
    for signature in (function.type, function.unanalyzed_type):
        if isinstance(signature, CallableType):
            _rewrite_callable(signature)
    _rewrite_statements(function.body.body)


def _rewrite_compound(statement: Statement) -> None:
    """The blocks of a compound statement, and the type comments of ``for`` / ``with``."""
    if isinstance(statement, ForStmt):
        statement.index_type = _rewrite(statement.index_type)
        statement.unanalyzed_index_type = _rewrite(statement.unanalyzed_index_type)
    elif isinstance(statement, WithStmt):
        statement.unanalyzed_type = _rewrite(statement.unanalyzed_type)
    for block in _blocks(statement):
        _rewrite_statements(block.body)


def _blocks(statement: Statement) -> list[Block]:
    if isinstance(statement, IfStmt):
        return [*statement.body, *([statement.else_body] if statement.else_body else [])]
    if isinstance(statement, ForStmt | WhileStmt):
        return [statement.body, *([statement.else_body] if statement.else_body else [])]
    if isinstance(statement, WithStmt):
        return [statement.body]
    if isinstance(statement, TryStmt):
        rest = [block for block in (statement.else_body, statement.finally_body) if block]
        return [statement.body, *statement.handlers, *rest]
    if isinstance(statement, MatchStmt):
        return list(statement.bodies)
    if isinstance(statement, Block):
        return [statement]
    return []


def _rewrite(typ: Type | None) -> Type | None:
    return None if typ is None else _rewrite_type(typ)


def _rewrite_type(typ: Type) -> Type:
    """A type with its ``odoo.values.*`` / ``odoo.model.*`` names rewritten.

    The parts of an unanalyzed type are changed in place; a name to rewrite is replaced.
    """
    if isinstance(typ, UnboundType):
        found = annotation_alias(typ.name)
        if found is not None:
            args: list[Type] = [
                RawExpressionType(part, STR, line=typ.line, column=typ.column) for part in found
            ]
            return UnboundType(TRIGGER, args, line=typ.line, column=typ.column)
        typ.args = tuple(_rewrite_type(arg) for arg in typ.args)
    elif isinstance(typ, CallableType):
        _rewrite_callable(typ)
    elif isinstance(typ, UnionType | TypeList):
        typ.items = [_rewrite_type(item) for item in typ.items]
    return typ


def _rewrite_callable(typ: CallableType) -> None:
    typ.arg_types = [_rewrite_type(arg) for arg in typ.arg_types]
    typ.ret_type = _rewrite_type(typ.ret_type)


class ItemTypes:
    """The types of the values a field takes in ``odoo.values.*``, by its field classes.

    The type depends on the field classes only (not on the model, its comodel or an
    addon), so the values of a model are the same ``TypedDict`` for every addon: see
    :mod:`odoo_typing.registry.annotations`. Records are ``BaseModel``; a selection is
    ``str``. ``False`` is ``bool`` (as :mod:`odoo_typing.vals` takes ``bool`` wherever
    it takes ``False``). Classes whose values are not checked (``Boolean``, ``Json``,
    custom field classes) give ``Any``, and so does a class whose types are not loaded.

    Args:
        named: Finds a class by full name (``None`` if mypy does not have it).
        base_model: Full name of ``BaseModel``.
        new_id: Full name of ``NewId``.
    """

    def __init__(
        self, named: Callable[[str], Instance | None], base_model: str, new_id: str
    ) -> None:
        self._named = named
        self._names = {RECORDS: base_model, NEW_ID: new_id, FALSE: "builtins.bool"}
        self._types: dict[frozenset[str], Type] = {}

    def items(self, fields: Mapping[str, frozenset[str]]) -> dict[str, Type]:
        """Field name to the type of the values it takes."""
        return {name: self.of(classes) for name, classes in fields.items()}

    def of(self, classes: frozenset[str]) -> Type:
        """The type of the values a field of these classes takes (shared objects)."""
        if classes not in self._types:
            self._types[classes] = self._compute(classes)
        return self._types[classes]

    def _compute(self, classes: frozenset[str]) -> Type:
        items: dict[str, Type] = {}
        for field_class in sorted(classes):
            names = VALUE_TYPES.get(field_class) if field_class != UNCHECKED else None
            if names is None:
                return AnyType(TypeOfAny.special_form)
            for name in names:
                if name == NONE:
                    items[NONE] = NoneType()
                    continue
                full = self._names.get(name, name)
                found = self._named(full)
                if found is None:
                    return AnyType(TypeOfAny.special_form)  # e.g. datetime not loaded
                items[full] = found
        return UnionType.make_union([items[key] for key in sorted(items, key=_union_order)])


def _union_order(name: str) -> int:
    """``bool`` and ``None`` last, so that messages read ``str | bool | None``."""
    return {"builtins.bool": 1, NONE: 2}.get(name, 0)
