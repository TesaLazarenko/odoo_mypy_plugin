"""``"f" in records``: the question whether a field is there, asked before it is read.

A recordset contains the names of its model's fields, and an addon asks before it reads
a field that an addon outside its dependencies adds::

    if "phone_sanitized" in partner:        # ``sms`` adds it to res.partner
        partner.phone_sanitized

    if "optional_product_ids" in self.env["product.template"]:
        self.template.optional_product_ids = [...]

mypy answers ``hasattr(partner, "phone_sanitized")`` by giving ``partner`` the attribute,
with an unknown type; of ``in`` on records it knows nothing, and the read was an unknown
attribute. The hook of ``__contains__`` gives the question mypy's own answer to
``hasattr`` (``TypeChecker.hasattr_type_maps``): the records asked take the attribute
from the question on, in the block the question is in. ``"f" in records._fields`` asks
the same of ``records``.

The question is also about the model: the records of that model which the plugin types
later in the same function (``env["model"]``, the records a test case keeps) take the
attribute too. A variable typed before the question keeps its type.

Only a model that has no such field is given one; a field it has keeps its type.
"""

from __future__ import annotations

from dataclasses import dataclass

from mypy.nodes import ComparisonExpr, Expression, FuncItem, MemberExpr, StrExpr
from mypy.plugin import AttributeContext, FunctionContext, MethodContext
from mypy.types import (
    AnyType,
    Instance,
    NoneType,
    ProperType,
    Type,
    TypeOfAny,
    UnionType,
    get_proper_type,
)

from odoo_typing.classes import receiver_type
from odoo_typing.hooks.models import Hooks, Models
from odoo_typing.hooks.nodes import type_checker

__all__ = ["HASATTR", "AskedFieldHooks"]

HASATTR = "builtins.hasattr"

FIELDS = "_fields"


@dataclass(frozen=True, slots=True)
class _Asked:
    """A field a function asked a model for, and the line of the question."""

    model: str
    name: str
    line: int


class AskedFieldHooks(Hooks):
    """The fields a function asks its records for (``"f" in records``)."""

    def __init__(self, models: Models) -> None:
        super().__init__(models)
        # The questions of the function asked last, which is the one being checked.
        self._function: FuncItem | None = None
        self._asked: list[_Asked] = []

    def contains(self, ctx: MethodContext) -> Type:
        """``"f" in records`` and ``"f" in records._fields``: the records have ``f`` from here."""
        comparison = ctx.context
        key = ctx.args[0][0] if ctx.args and ctx.args[0] else None
        if not isinstance(comparison, ComparisonExpr) or not isinstance(key, StrExpr):
            return ctx.default_return_type
        pairs = zip(comparison.operands, comparison.operands[1:], strict=False)
        container = next((right for left, right in pairs if left is key), None)
        if container is None or not key.value.isidentifier():
            return ctx.default_return_type
        checker = type_checker(ctx.api)
        records: Expression = container
        asked: Type | None = ctx.type
        if isinstance(container, MemberExpr) and container.name == FIELDS:
            records = container.expr
            asked = checker.lookup_type_or_none(records)
        models = self._record_models(asked)
        if asked is None or not models:
            return ctx.default_return_type
        narrowed, _ = checker.hasattr_type_maps(records, asked, key.value)
        for expression, with_field in (narrowed or {}).items():
            if with_field != asked:
                # mypy narrows the right operand of ``in`` to the type it has recorded
                # for it (a union of typed dicts loses items there): the record too.
                checker.store_type(expression, with_field)
                checker.binder.put(expression, with_field, from_assignment=False)
        function = checker.scope.top_level_function()
        if function is not self._function:
            self._function = function
            self._asked = []
        if function is not None:
            self._asked += [_Asked(model, key.value, comparison.line) for model in models]
        return ctx.default_return_type

    def has_attribute(self, ctx: FunctionContext) -> Type:
        """``hasattr(records, "f")`` of a name their class lacks: ``Any`` in the block.

        The question is whether an addon outside the dependencies is installed
        (``hasattr(ResPartner, "check_vat")``: ``base_vat``); the block then uses what
        that addon adds, the asked name and others (``ResPartner.fix_eu_vat_number()``).
        mypy narrows the records in the block to the type it recorded for them, when
        that type has the attribute: recorded as a union of ``Any`` alone, they are
        ``Any`` in the block, as records of a guarded model are.
        """
        if len(ctx.args) != 2 or len(ctx.args[0]) != 1 or len(ctx.args[1]) != 1:
            return ctx.default_return_type
        records, name = ctx.args[0][0], ctx.args[1][0]
        asked = ctx.arg_types[0][0]
        if not isinstance(name, StrExpr) or not self._record_models(asked):
            return ctx.default_return_type
        checker = type_checker(ctx.api)
        if not checker.has_valid_attribute(asked, name.value):
            # A union of ``Any`` alone: mypy's ``hasattr`` narrowing keeps a type that has
            # the attribute (``Any`` itself it leaves alone), the block reads it as ``Any``.
            checker.store_type(records, UnionType([AnyType(TypeOfAny.special_form)]))
        return ctx.default_return_type

    def _record_models(self, typ: Type | None) -> list[str]:
        """The models of a type that is records and nothing else (``None`` aside)."""
        proper = receiver_type(typ)
        items = proper.items if isinstance(proper, UnionType) else [proper]
        found: list[str] = []
        for item in map(receiver_type, items):
            if isinstance(item, NoneType):
                continue
            model = self._models.model_of(item.type) if isinstance(item, Instance) else None
            if model is None:
                return []
            found.append(model)
        return found

    def with_asked(self, records: Type, ctx: AttributeContext | MethodContext) -> Type:
        """Records typed after their model was asked for a field take that field."""
        if not self._asked:
            return records
        checker = type_checker(ctx.api)
        if checker.scope.top_level_function() is not self._function:
            return records
        proper = get_proper_type(records)
        if isinstance(proper, UnionType):
            items = [self._with_asked(get_proper_type(item), ctx) for item in proper.items]
            return UnionType.make_union(items, proper.line, proper.column)
        return self._with_asked(proper, ctx)

    def _with_asked(self, records: ProperType, ctx: AttributeContext | MethodContext) -> Type:
        if not isinstance(records, Instance):
            return records
        model = self._models.model_of(records.type)
        checker = type_checker(ctx.api)
        result: Type = records
        for asked in self._asked:
            after = asked.model == model and asked.line <= ctx.context.line
            if after and not checker.has_valid_attribute(result, asked.name):
                result = checker.add_any_attribute_to_type(result, asked.name)
        return result
