"""Attributes a class method defines on its class (``cls.partner = ...`` in ``setUpClass``).

Python binds such a name on the class, so every method reads it afterwards, on the class
and on its instances. mypy takes only ``self.name = value`` of an instance method for a
definition: the same line in a class method is ``"type[C]" has no attribute "name"``, and
so is every later read. Odoo's tests build their records this way (``setUpClass`` of a
``TransactionCase``), so nearly all errors of a test module were of this kind, and the
records read as ``Any`` behind them.

The two steps, both on what mypy already does for ``self``:

* Before mypy analyzes the bodies of a class's methods (:func:`define_through_cls`, with
  the class's MRO), the first parameter of each class method is marked as the method's
  ``self``. mypy then defines the attributes itself: it infers their types from the
  assigned values, follows ``cls.rows = []`` / ``cls.rows.append(...)``, and checks a
  later assignment against the first.
* mypy keeps such a definition for instances only, and refuses to read it on the class
  (``Cannot access instance-only attribute``). The names assigned through the first
  parameter are kept with the class (in its metadata, so in mypy's cache as well); when
  mypy looks one up on a class (:func:`readable_on_class`), the definition stops being
  instance-only.

An attribute that an instance method alone defines (``self.name = ...`` in ``setUp``)
stays as mypy has it.

A class may also name its attribute hook after another method (``__getattr__ =
__getitem__``, Odoo 20's ``TableSQL``): mypy looks such a hook up as a method, finds a
variable, and reports every attribute of the class. :func:`bind_method_aliases` makes the
hook the method it is assigned from.
"""

from __future__ import annotations

from typing import Final

from mypy.nodes import (
    MDEF,
    AssignmentStmt,
    Block,
    ClassDef,
    Decorator,
    Expression,
    ForStmt,
    FuncDef,
    IfStmt,
    ListExpr,
    MatchStmt,
    MemberExpr,
    NameExpr,
    OverloadedFuncDef,
    RefExpr,
    StarExpr,
    Statement,
    SymbolTableNode,
    TryStmt,
    TupleExpr,
    TypeInfo,
    Var,
    WhileStmt,
    WithStmt,
)
from mypy.plugin import ClassDefContext

__all__ = ["METADATA_KEY", "bind_method_aliases", "define_through_cls", "readable_on_class"]

METADATA_KEY: Final = "odoo_typing.class_attributes"
NAMES: Final = "names"
CLASS_METHOD: Final = "classmethod"
CLASS_METHOD_FULLNAME: Final = "builtins.classmethod"
# The hooks mypy looks up as methods of the class (``TypeInfo.get_method``).
ATTRIBUTE_HOOKS: Final = frozenset({"__getattr__", "__getattribute__", "__setattr__"})


def define_through_cls(ctx: ClassDefContext) -> None:
    """Let mypy read ``cls.name = value`` in the class's class methods as definitions."""
    names: set[str] = set()
    for method in _class_methods(ctx.cls):
        receiver = method.arguments[0].variable
        receiver.is_self = True
        _assigned(method.body, receiver.name, names)
    if names:
        ctx.cls.info.metadata[METADATA_KEY] = {NAMES: sorted(names)}
    else:
        ctx.cls.info.metadata.pop(METADATA_KEY, None)


def bind_method_aliases(ctx: ClassDefContext) -> None:
    """``__getattr__ = __getitem__``: the attribute hook is the method it is assigned from.

    Called once the class body is analyzed, when both names are bound.
    """
    names = ctx.cls.info.names
    for statement in ctx.cls.defs.body:
        if not isinstance(statement, AssignmentStmt) or len(statement.lvalues) != 1:
            continue
        target, value = statement.lvalues[0], statement.rvalue
        if (
            not isinstance(target, NameExpr)
            or target.name not in ATTRIBUTE_HOOKS
            or not isinstance(value, NameExpr)
        ):
            continue
        hook, method = names.get(target.name), names.get(value.name)
        if (
            hook is not None
            and method is not None
            and isinstance(hook.node, Var)
            and isinstance(method.node, (FuncDef, Decorator, OverloadedFuncDef))
        ):
            names[target.name] = SymbolTableNode(MDEF, method.node, plugin_generated=True)


def readable_on_class(info: TypeInfo, name: str) -> None:
    """Make an attribute that a class method defined readable on the class too.

    Args:
        info: The class the attribute is read on (or a subclass of the defining one).
        name: The attribute.
    """
    # The class of the MRO that has the name, not the variable's own ``info``: after a
    # daemon update the variable of an unchanged method may still name a class object
    # that mypy has replaced.
    owner = next((base for base in info.mro if name in base.names), None)
    node = owner.names[name] if owner is not None else None
    if owner is None or node is None or not node.implicit or not isinstance(node.node, Var):
        return
    kept = owner.metadata.get(METADATA_KEY)
    if kept is not None and name in kept[NAMES]:
        node.implicit = False


def _class_methods(defn: ClassDef) -> list[FuncDef]:
    """The methods of a class body that are decorated with ``classmethod`` and take ``cls``."""
    found: list[FuncDef] = []
    pending: list[Statement] = list(defn.defs.body)
    while pending:
        statement = pending.pop()
        if isinstance(statement, IfStmt):  # ``if VERSION >= ...:`` around a method
            blocks: list[Block] = [*statement.body]
            if statement.else_body is not None:
                blocks.append(statement.else_body)
            for block in blocks:
                pending.extend(block.body)
        elif (
            isinstance(statement, Decorator)
            and statement.func.arguments
            # ``original_decorators``: mypy takes ``classmethod`` out of ``decorators``
            # once it has analyzed the method, and analyzes a class more than once
            # (a deferred class, a daemon update of the module's top level).
            and (
                statement.func.is_class
                or any(_is_class_method(item) for item in statement.original_decorators)
            )
        ):
            found.append(statement.func)
    return found


def _is_class_method(decorator: Expression) -> bool:
    """Whether a decorator is ``classmethod`` (the names are not bound yet at this point)."""
    if not isinstance(decorator, RefExpr):
        return False
    if decorator.fullname:
        return decorator.fullname == CLASS_METHOD_FULLNAME
    return isinstance(decorator, NameExpr) and decorator.name == CLASS_METHOD


def _assigned(block: Block | None, receiver: str, names: set[str]) -> None:
    """Add the attribute names a block assigns through a name (``cls.name = value``).

    A plain walk of the statements: compiled mypy does not let interpreted classes
    subclass its visitors. Nested functions and classes are not looked into.
    """
    for statement in block.body if block is not None else ():
        if isinstance(statement, AssignmentStmt):
            for lvalue in statement.lvalues:
                _targets(lvalue, receiver, names)
        elif isinstance(statement, IfStmt):
            for body in (*statement.body, statement.else_body):
                _assigned(body, receiver, names)
        elif isinstance(statement, (ForStmt, WhileStmt)):
            _assigned(statement.body, receiver, names)
            _assigned(statement.else_body, receiver, names)
        elif isinstance(statement, WithStmt):
            _assigned(statement.body, receiver, names)
        elif isinstance(statement, TryStmt):
            bodies = (statement.body, *statement.handlers, statement.else_body)
            for body in (*bodies, statement.finally_body):
                _assigned(body, receiver, names)
        elif isinstance(statement, MatchStmt):
            for body in statement.bodies:
                _assigned(body, receiver, names)
        elif isinstance(statement, Block):
            _assigned(statement, receiver, names)


def _targets(lvalue: Expression, receiver: str, names: set[str]) -> None:
    if isinstance(lvalue, (TupleExpr, ListExpr)):
        for item in lvalue.items:
            _targets(item, receiver, names)
    elif isinstance(lvalue, StarExpr):
        _targets(lvalue.expr, receiver, names)
    elif (
        isinstance(lvalue, MemberExpr)
        and isinstance(lvalue.expr, NameExpr)
        and lvalue.expr.name == receiver
    ):
        names.add(lvalue.name)
