"""Parameters that take the containers the class that annotated them passes them.

``MailTrackMixin._track_execute(..., trackings: dict[int, tuple[set[str],
list[ValuesType]]])`` (Odoo 20) is called by the mixin itself with a list where the
annotation names a set, and ``account.tax`` passes it a list too: the annotation is
narrower than the code Odoo runs, and every such call was an ``arg-type`` error. The
registry says which containers a parameter is given
(:meth:`odoo_typing.registry.fields.ModelFields.passed`); the annotation gets an
alternative with that container in the place of the one it names::

    dict[int, tuple[set[str], list[ValuesType]]] | dict[int, tuple[list[str], list[ValuesType]]]

The annotation itself is rewritten, before mypy analyzes the signature: mypy calls no
hook for ``super().m(...)``, which is how an override passes the value on. Only in a
module whose errors mypy does not show (Odoo's own, in the usual configuration): in the
code being checked an annotation is its author's word, and a call that contradicts it an
error to report.
"""

from __future__ import annotations

from mypy.nodes import Decorator, FuncDef
from mypy.plugin import ClassDefContext
from mypy.types import CallableType, EllipsisType, Type, UnboundType, UnionType

from odoo_typing.registry.fields import Passed
from odoo_typing.scanner.class_bodies import ITEM, SEQUENCES, VALUE

__all__ = ["widen_passed"]

TUPLE = "tuple"
DICT = "dict"


def widen_passed(ctx: ClassDefContext, passed: Passed) -> None:
    """Give the annotated parameters of a class's methods the containers they are passed."""
    if not passed:
        return
    for statement in ctx.cls.defs.body:
        func = statement.func if isinstance(statement, Decorator) else statement
        if not isinstance(func, FuncDef) or not isinstance(func.type, CallableType):
            continue
        arg_types = list(func.type.arg_types)
        if len(arg_types) != len(func.arg_names):
            continue
        changed = False
        for index, name in enumerate(func.arg_names):
            given = passed.get((func.name, name)) if name is not None else None
            widened = _widened(arg_types[index], given, ctx) if given else None
            if widened is not None:
                arg_types[index] = widened
                changed = True
        if changed:
            func.type = func.type.copy_modified(arg_types=arg_types)


def _widened(
    declared: Type, given: frozenset[tuple[tuple[str, ...], str]], ctx: ClassDefContext
) -> Type | None:
    """An annotation with an alternative for each container given instead of the one named.

    ``None`` if there is none to add: mypy runs the hook of a class more than once, and
    an annotation that has its alternatives is left as it is.
    """
    items = list(declared.items) if isinstance(declared, UnionType) else [declared]
    alternatives: list[Type] = []
    for item in items:
        for path, kind in sorted(given):
            alternative = _replaced(item, path, kind, ctx)
            if alternative is None or alternative in items or alternative in alternatives:
                continue
            alternatives.append(alternative)
    if not alternatives:
        return None
    return UnionType(
        [*items, *alternatives], declared.line, declared.column, uses_pep604_syntax=False
    )


def _replaced(node: Type, path: tuple[str, ...], kind: str, ctx: ClassDefContext) -> Type | None:
    """An annotation not analyzed yet, with ``kind`` for the container it names at a path."""
    if not isinstance(node, UnboundType):
        return None
    name = node.name
    if not _builtin(name, node, ctx):
        return None
    args = list(node.args)
    if not path:
        if name not in SEQUENCES or kind == name or len(args) != 1 or not _builtin(kind, node, ctx):
            return None
        items = [args[0], EllipsisType()] if kind == TUPLE else args
        return UnboundType(kind, items, line=node.line, column=node.column)
    variadic = any(isinstance(arg, EllipsisType) for arg in args)
    step = path[0]
    index: int | None = None
    if step == VALUE:
        index = 1 if name == DICT and len(args) == 2 else None
    elif step == ITEM:
        if (name in SEQUENCES and len(args) == 1) or (name == TUPLE and variadic):
            index = 0
    elif name == TUPLE and not variadic and int(step) < len(args):
        index = int(step)
    if index is None:
        return None
    inner = _replaced(args[index], path[1:], kind, ctx)
    if inner is None:
        return None
    args[index] = inner
    return node.copy_modified(args=args)


def _builtin(name: str, node: UnboundType, ctx: ClassDefContext) -> bool:
    """Whether a name is the builtin container it spells, where the annotation is written."""
    if name not in SEQUENCES and name not in (DICT, TUPLE):
        return False
    symbol = ctx.api.lookup_qualified(name, node, suppress_errors=True)
    return symbol is not None and symbol.fullname == f"builtins.{name}"
