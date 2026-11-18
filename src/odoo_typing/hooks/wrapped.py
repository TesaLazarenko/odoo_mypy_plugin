"""Methods decorated with ``functools.wraps(...)``, called on an instance.

Odoo 18's ``FutureResponse.set_cookie`` is ``@functools.wraps(werkzeug.Response.set_cookie)``
over ``def set_cookie(self, key, ...)``. ``wraps`` returns the decorated function, which
Python binds to an instance as any method; typeshed gives a ``_Wrapped`` object whose
``__call__`` takes the function's own parameters, ``self`` first, so
``request.future_response.set_cookie("name", max_age=0)`` gave ``"name"`` to ``self`` and
missed ``key``. Called on an instance of the class that defines the method, the
signature is bound: without its first parameter.
"""

from __future__ import annotations

from mypy.nodes import ARG_POS, CallExpr, Decorator, MemberExpr, RefExpr
from mypy.plugin import MethodSigContext
from mypy.types import CallableType, FunctionLike, Instance, get_proper_type

from odoo_typing.hooks.nodes import type_checker

__all__ = ["WRAPPED_CALL", "bound_wrapped_signature"]

WRAPPED_CALL = "functools._Wrapped.__call__"
WRAPS = frozenset({"functools.wraps"})


def bound_wrapped_signature(ctx: MethodSigContext) -> FunctionLike:
    """``instance.method(...)`` of a method decorated with ``functools.wraps``: bound."""
    signature = ctx.default_signature
    call = ctx.context
    if (
        not isinstance(signature, CallableType)
        or not isinstance(call, CallExpr)
        or not isinstance(call.callee, MemberExpr)
        or not signature.arg_kinds
        or signature.arg_kinds[0] != ARG_POS
    ):
        return signature
    callee = call.callee
    owner = get_proper_type(type_checker(ctx.api).lookup_type_or_none(callee.expr))
    if not isinstance(owner, Instance):
        return signature
    found = owner.type.get(callee.name)
    node = found.node if found is not None else None
    if not isinstance(node, Decorator) or node.func.is_static or node.func.is_class:
        return signature
    if not any(_is_wraps(decorator) for decorator in node.decorators):
        return signature
    return signature.copy_modified(
        arg_types=signature.arg_types[1:],
        arg_kinds=signature.arg_kinds[1:],
        arg_names=signature.arg_names[1:],
    )


def _is_wraps(decorator: object) -> bool:
    """``@functools.wraps(...)`` (also ``@wraps(...)`` imported from ``functools``)."""
    return (
        isinstance(decorator, CallExpr)
        and isinstance(decorator.callee, RefExpr)
        and decorator.callee.fullname in WRAPS
    )
