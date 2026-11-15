"""``odoo.values.*``, ``odoo.model.*`` and ``odoo.domain.*`` annotations.

``Annotated[dict, "odoo.values.res_partner"]`` and the bare strings
``"odoo.values.res_partner"`` / ``"odoo.model.res_partner"`` (the PyCharm plugin "Odoo
IDE"'s) stand for the ``vals`` of a model and for its records;
``"odoo.domain.res_partner"`` / ``Annotated[list, "odoo.domain.res_partner"]`` (this
plugin's) for a domain of the model (:mod:`odoo_typing.hooks.annotations` gives the
types).

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

The same pass wraps the expressions whose domains the plugin checks, since mypy has no
hook on a ``return`` or an assignment: ``return E`` in a function annotated to return a
domain, the value of an annotated assignment ``x: "odoo.domain.x" = E`` (in a module, a
function or the body of an Odoo model class) and ``return E`` in the method a field
names in ``search=`` become ``__build_class__(E, "<kind>", "<alias or model>")``. A
function hook on ``builtins.__build_class__`` (which Python calls itself, never code)
checks ``E``, and a signature hook makes the call an identity, so mypy types the
statement as before (:mod:`odoo_typing.hooks.domains`). The name is bound to the
builtin by hand: the statements of a module above its first class are analyzed before
the rewrite.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from mypy.nodes import (
    ARG_POS,
    GDEF,
    AssignmentStmt,
    Block,
    CallExpr,
    ClassDef,
    ConditionalExpr,
    Decorator,
    Expression,
    ExpressionStmt,
    ForStmt,
    FuncDef,
    IfStmt,
    ListExpr,
    MatchStmt,
    MemberExpr,
    MypyFile,
    NameExpr,
    OperatorAssignmentStmt,
    OpExpr,
    OverloadedFuncDef,
    ReturnStmt,
    StarExpr,
    Statement,
    StrExpr,
    SymbolNode,
    TempNode,
    TryStmt,
    TupleExpr,
    UnaryExpr,
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

from odoo_typing.registry.annotations import UNCHECKED, SearchMethod
from odoo_typing.scanner import DOMAIN, KINDS, AnnotationAlias
from odoo_typing.vals import FALSE, NEW_ID, NONE, RECORDS, VALUE_TYPES

__all__ = [
    "ANNOTATED_NAMES",
    "ASSIGN",
    "ASSIGN_BUILT",
    "RETURN",
    "SEARCH",
    "SEARCH_ALIAS",
    "TRIGGER",
    "TRIGGER_FULLNAME",
    "ItemTypes",
    "Wrapping",
    "annotation_alias",
    "domain_annotation_alias",
    "rewrite_annotations",
    "trigger_alias",
]

ANNOTATED_NAMES: Final = ("typing.Annotated", "typing_extensions.Annotated")
TRIGGER: Final = "__build_class__"
TRIGGER_FULLNAME: Final = f"builtins.{TRIGGER}"
PREFIXES: Final = {f"odoo.{kind}.": kind for kind in KINDS}
STR: Final = "builtins.str"
# What a wrapped expression is (the second argument of the wrapper).
RETURN: Final = "return"  # returned by a function annotated to return a domain
ASSIGN: Final = "assign"  # the value of an annotated assignment
ASSIGN_BUILT: Final = "assign-built"  # the same, of a variable changed in place later
SEARCH: Final = "search"  # returned by a field's search method (third argument: the model)
SEARCH_ALIAS: Final = "search-alias"  # the same, of a search method annotated with a domain
# The methods that change a list in place: a variable built with them is not a whole
# domain where it is assigned. (A name-based walk at rewrite time: some statements of the
# module are analyzed already, others not, so LocalValues's keys would split a variable.)
IN_PLACE_METHODS: Final = frozenset({"append", "extend", "insert"})


@dataclass(frozen=True, slots=True)
class Wrapping:
    """What the rewrite of a module wraps for the domain checks (see the module docstring).

    Attributes:
        wrapper: The node of ``builtins.__build_class__``, which the wrapper's name is
            bound to.
        model_classes: The names of the module's top-level Odoo model classes (their
            annotated class attributes are wrapped; those of other classes, such as
            dataclasses, are not: their plugins read the value).
        search_methods: Class name to the methods named in ``search=`` of the fields
            of the class's model (:class:`SearchMethod`).
    """

    wrapper: SymbolNode
    model_classes: frozenset[str] = frozenset()
    # A factory: Python 3.11 rejects an unhashable default (mappingproxy has no hash before 3.12).
    search_methods: Mapping[str, Mapping[str, SearchMethod]] = field(
        default_factory=lambda: MappingProxyType({})
    )


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
    if kind not in KINDS or not isinstance(alias, str):
        return None
    return str(kind), alias


def rewrite_annotations(tree: MypyFile, wrapping: Wrapping | None = None) -> None:
    """Rewrite the ``odoo.<kind>.*`` annotations of a parsed module; wrap its domains.

    Called during mypy's semantic analysis (see the module docstring); the annotations
    analyzed already keep their types, and rewriting a module again changes nothing.
    With ``wrapping``, the expressions whose domains are checked are wrapped too.
    """
    _rewrite_statements(tree.defs)
    if wrapping is not None:
        _Wrapper(wrapping).block(tree.defs, None, tree.defs, wrap_assignments=True)


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


def domain_annotation_alias(typ: Type | None) -> str | None:
    """The alias of an unanalyzed annotation that is a domain at its top level, if it is one.

    ``"odoo.domain.x"`` (or its rewritten form), ``Annotated[..., "odoo.domain.x"]``, and
    either with ``| None`` / ``Optional[...]``; by syntax (the names are not looked up).
    """
    if isinstance(typ, UnionType):
        found = [domain_annotation_alias(item) for item in typ.items if not _is_none(item)]
        return found[0] if len(found) == 1 else None
    if not isinstance(typ, UnboundType):
        return None
    if typ.name == TRIGGER:
        alias = trigger_alias(typ)
        return alias[1] if alias is not None and alias[0] == DOMAIN else None
    if not typ.args:
        alias = annotation_alias(typ.name)
        return alias[1] if alias is not None and alias[0] == DOMAIN else None
    last = typ.name.rpartition(".")[2]
    if last == "Optional" and len(typ.args) == 1:
        return domain_annotation_alias(typ.args[0])
    if last != "Annotated":
        return None
    for meta in typ.args[1:]:
        if isinstance(meta, UnboundType):
            named = trigger_alias(meta) if meta.name == TRIGGER else annotation_alias(meta.name)
            if named is not None:
                return named[1] if named[0] == DOMAIN else None
    return None


def _is_none(typ: Type) -> bool:
    return isinstance(typ, NoneType) or (isinstance(typ, UnboundType) and typ.name == "None")


@dataclass(frozen=True, slots=True)
class _Returns:
    """How the ``return`` statements of a function are wrapped: kind and alias (or model)."""

    kind: str
    alias: str


class _Wrapper:
    """Wrap the domain expressions of a module's statements (see the module docstring)."""

    def __init__(self, wrapping: Wrapping) -> None:
        self._wrapping = wrapping

    def block(
        self,
        statements: list[Statement],
        returns: _Returns | None,
        scope: list[Statement],
        wrap_assignments: bool,
    ) -> None:
        """Wrap in a list of statements (``scope``: those of its module, class or function)."""
        for statement in statements:
            if isinstance(statement, AssignmentStmt):
                if wrap_assignments:
                    self._assignment(statement, scope)
            elif isinstance(statement, ReturnStmt):
                if returns is not None and statement.expr is not None:
                    statement.expr = self._wrap(statement.expr, returns.kind, returns.alias)
            elif isinstance(statement, Decorator):
                self.function(statement.func, None)
            elif isinstance(statement, FuncDef):
                self.function(statement, None)
            elif isinstance(statement, OverloadedFuncDef):
                if statement.impl is not None:  # the @overload items have no body
                    impl = statement.impl
                    self.function(impl.func if isinstance(impl, Decorator) else impl, None)
            elif isinstance(statement, ClassDef):
                self._class(statement)
            else:
                for block in _blocks(statement):
                    self.block(block.body, returns, scope, wrap_assignments)

    def function(self, function: FuncDef, search: SearchMethod | None) -> None:
        """Wrap the returns of a function annotated to return a domain, or of a search method.

        A search method's returns are checked as Odoo reads them (an internal domain that
        a search runs), against its annotation's model if it has one (also for a mixin's
        method). Without one, it is wrapped only for a concrete model and when it has no
        return annotation: the wrapper passes no type context, so a precise one
        (``-> DomainType``) would reject a literal.
        """
        signature = function.unanalyzed_type
        returned = signature.ret_type if isinstance(signature, CallableType) else None
        alias = domain_annotation_alias(returned)
        returns: _Returns | None = None
        if search is not None and alias is not None:
            returns = _Returns(SEARCH_ALIAS, alias)
        elif alias is not None:
            returns = _Returns(RETURN, alias)
        elif (
            search is not None
            and search.concrete
            and (
                signature is None
                or (isinstance(returned, AnyType) and returned.type_of_any == TypeOfAny.unannotated)
            )
        ):
            returns = _Returns(SEARCH, search.model)
        body = function.body.body
        self.block(body, returns, body, wrap_assignments=True)

    def _class(self, cls: ClassDef) -> None:
        model_class = cls.name in self._wrapping.model_classes
        search = self._wrapping.search_methods.get(cls.name, {}) if model_class else {}
        body = cls.defs.body
        for statement in body:
            func = statement.func if isinstance(statement, Decorator) else statement
            if isinstance(func, FuncDef):
                self.function(func, search.get(func.name))
            elif isinstance(statement, AssignmentStmt):
                if model_class:
                    self._assignment(statement, body)
            else:
                self.block([statement], None, body, wrap_assignments=model_class)

    def _assignment(self, statement: AssignmentStmt, scope: list[Statement]) -> None:
        alias = domain_annotation_alias(statement.unanalyzed_type)
        if alias is None or isinstance(statement.rvalue, TempNode) or len(statement.lvalues) != 1:
            return
        target = statement.lvalues[0]
        whole = isinstance(target, NameExpr) and not _changed_later(
            target.name, statement.line, scope
        )
        kind = ASSIGN if whole else ASSIGN_BUILT
        statement.rvalue = self._wrap(statement.rvalue, kind, alias)

    def _wrap(self, expression: Expression, kind: str, alias: str) -> Expression:
        """``__build_class__(expression, kind, alias)``, placed where ``expression`` is."""
        if _is_wrapped(expression):
            return expression
        callee = NameExpr(TRIGGER)
        callee.node = self._wrapping.wrapper
        callee.fullname = TRIGGER_FULLNAME
        callee.kind = GDEF
        args: list[Expression] = [expression, StrExpr(kind), StrExpr(alias)]
        call = CallExpr(callee, args, [ARG_POS] * 3, [None] * 3)
        for node in (call, callee, *args[1:]):
            node.set_line(expression)
        return call


def _is_wrapped(expression: Expression) -> bool:
    return (
        isinstance(expression, CallExpr)
        and isinstance(expression.callee, NameExpr)
        and expression.callee.fullname == TRIGGER_FULLNAME
        and len(expression.args) == 3
    )


def _changed_later(name: str, line: int, scope: list[Statement]) -> bool:
    """Whether a variable is built further after a line (``x += [...]``, ``x = x + [...]``).

    In the statements of its scope and their blocks, not in nested functions or classes.
    """
    for statement in scope:
        if isinstance(statement, FuncDef | Decorator | OverloadedFuncDef | ClassDef):
            continue
        if statement.line > line and _changes(statement, name):
            return True
        if any(_changed_later(name, line, block.body) for block in _blocks(statement)):
            return True
    return False


def _changes(statement: Statement, name: str) -> bool:
    """Whether a statement extends a variable: in place, or bound to a list made from it."""
    if isinstance(statement, OperatorAssignmentStmt):
        target = statement.lvalue
        return isinstance(target, NameExpr) and target.name == name and statement.op in "+|&"
    if isinstance(statement, AssignmentStmt):
        return any(
            isinstance(target, NameExpr) and target.name == name for target in statement.lvalues
        ) and _reads_as_piece(statement.rvalue, name)
    if isinstance(statement, ExpressionStmt) and isinstance(statement.expr, CallExpr):
        callee = statement.expr.callee
        return (
            isinstance(callee, MemberExpr)
            and isinstance(callee.expr, NameExpr)
            and callee.expr.name == name
            and callee.name in IN_PLACE_METHODS
        )
    return False


# Calls whose list arguments are whole domains, by their last name: ``expression.AND``,
# ``Domain.AND`` / ``OR``, ``Domain(...)``.
WHOLE_DOMAIN_CALLS: Final = frozenset({"AND", "OR", "Domain"})


def _reads_as_piece(expression: Expression, name: str) -> bool:
    """Whether an expression reads a name as a piece of a list (``x + [...]``, ``[*x, ...]``).

    By syntax. Not when it reads it as a whole domain (``AND([x, ...])``, ``Domain(x)``,
    ``x & y``, ``~x``): the variable's own value is then a whole domain. Another call
    that reads it counts (what it builds is not known).
    """
    if isinstance(expression, OpExpr):
        operands = (expression.left, expression.right)
        if expression.op == "+" and any(_is_name(item, name) for item in operands):
            return True
        return any(_reads_as_piece(item, name) for item in operands)
    if isinstance(expression, ListExpr | TupleExpr):
        return any(
            (isinstance(item, StarExpr) and _is_name(item.expr, name))
            or _reads_as_piece(item, name)
            for item in expression.items
        )
    if isinstance(expression, UnaryExpr):
        return _reads_as_piece(expression.expr, name)
    if isinstance(expression, ConditionalExpr):
        return any(
            _reads_as_piece(item, name) for item in (expression.if_expr, expression.else_expr)
        )
    if isinstance(expression, CallExpr):
        callee = expression.callee
        last = callee.name if isinstance(callee, NameExpr | MemberExpr) else None
        if last in WHOLE_DOMAIN_CALLS:
            return any(_reads_as_piece(arg, name) for arg in expression.args)
        return any(_mentions(arg, name) for arg in expression.args)
    return False


def _is_name(expression: Expression, name: str) -> bool:
    return isinstance(expression, NameExpr) and expression.name == name


def _mentions(expression: Expression, name: str) -> bool:
    """Whether an expression reads a name anywhere (by syntax, common forms only)."""
    if isinstance(expression, NameExpr):
        return expression.name == name
    if isinstance(expression, OpExpr):
        return _mentions(expression.left, name) or _mentions(expression.right, name)
    if isinstance(expression, CallExpr):
        return _mentions(expression.callee, name) or any(
            _mentions(arg, name) for arg in expression.args
        )
    if isinstance(expression, ListExpr | TupleExpr):
        return any(_mentions(item, name) for item in expression.items)
    if isinstance(expression, MemberExpr | UnaryExpr | StarExpr):
        return _mentions(expression.expr, name)
    if isinstance(expression, ConditionalExpr):
        return any(
            _mentions(part, name)
            for part in (expression.cond, expression.if_expr, expression.else_expr)
        )
    return False


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
