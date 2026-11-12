"""Domains: ``search([("partner_id.name", "ilike", "x")])`` and the ``domain=`` of fields.

Odoo checks a domain only when it runs it: a field of a term's path that does not exist,
a path through a non-relational field, an unknown operator or a logical operator other
than ``&`` / ``|`` / ``!`` raise there (``expression.py`` in Odoo 18, ``Domain`` in 19+).
The checks follow those rules on the literal parts of a domain:

* each term of a literal list whose first item is a string (``(1, "=", 1)`` and terms
  built from variables are skipped);
* the path follows the comodels, a date field may end it with a date part
  (``date.year_number``), a ``Properties`` field with a property name;
* ``any`` / ``not any`` with a literal domain are checked against the comodel;
* a string value (or a list of strings) of a ``Selection`` field with literal keys
  must be one of the keys, as in :mod:`odoo_typing.vals`.

The literal lists are found in the domain expression by :func:`domain_term_lists`:
``a + b``, ``expression.AND/OR``, ``Domain`` objects and operators, and a variable built
before the call (:mod:`odoo_typing.local_values`).

The ``domain=`` of a relational field is a domain of the comodel; given as a string it
is evaluated by the client (``parent.x``, names of the record's fields as values), so
only its field paths are checked.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from mypy.errorcodes import ErrorCode
from mypy.nodes import (
    ARG_POS,
    CallExpr,
    Context,
    Expression,
    ListExpr,
    MemberExpr,
    NameExpr,
    OpExpr,
    RefExpr,
    StrExpr,
    TupleExpr,
    UnaryExpr,
)

from odoo_typing.local_values import MAX_DEPTH, Change, LocalValues
from odoo_typing.references import RELATIONAL, did_you_mean, resolve_path
from odoo_typing.scanner import FieldInfo

__all__ = [
    "DOMAIN_METHODS",
    "ODOO_DOMAIN",
    "DomainChecker",
    "Term",
    "string_domain_paths",
]

ODOO_DOMAIN = ErrorCode("odoo-domain", "Check field paths and operators of Odoo domains", "Odoo")

# Methods whose ``domain`` argument is a domain of the receiver's model.
DOMAIN_METHODS: Final = frozenset(
    {
        "search",
        "search_count",
        "search_read",
        "search_fetch",
        "filtered_domain",
        "read_group",
        "_read_group",
        "_search",
    }
)
LOGICAL_OPERATORS: Final = frozenset({"&", "|", "!"})
# READ_GROUP_NUMBER_GRANULARITY, the same in Odoo 18-20.
DATE_PARTS: Final = (
    "year_number",
    "quarter_number",
    "month_number",
    "iso_week_number",
    "day_of_year",
    "day_of_month",
    "day_of_week",
    "hour_number",
    "minute_number",
    "second_number",
)
SUBDOMAIN_OPERATORS: Final = frozenset({"any", "not any", "any!", "not any!"})
SELECTION_OPERATORS: Final = frozenset({"=", "!=", "in", "not in"})
# Operators that difflib does not match to their replacement.
REPLACEMENTS: Final = {"<>": "!=", "==": "="}
SHOWN_KEYS: Final = 5

Fail = Callable[[str, Context], object]
FieldsOf = Callable[[str], Mapping[str, FieldInfo]]


@dataclass(frozen=True, slots=True)
class Term:
    """One term of a domain: ``(path, operator, value)``.

    Attributes:
        path: The field path, where the messages about it point.
        operator: The operator, if it is a literal.
        value: The value expression, ``None`` if the term has none to check.
    """

    path: StrExpr
    operator: StrExpr | None
    value: Expression | None


# ``expression.AND([...])`` / ``OR`` (``odoo.osv.expression``, Odoo 17-19).
COMBINATORS: Final = frozenset({"odoo.osv.expression.AND", "odoo.osv.expression.OR"})
# Operators that combine two domains: lists (``+``), ``Domain`` objects (``&``, ``|``).
DOMAIN_OPERATORS: Final = frozenset({"+", "&", "|"})


def domain_term_lists(
    domain: Expression,
    domain_class: str | None,
    local: LocalValues | None = None,
    depth: int = 0,
) -> Iterator[Sequence[Expression]]:
    """The literal term lists a domain expression is made of.

    A list display; ``a + b`` of lists, ``a & b`` / ``a | b`` / ``~a`` of ``Domain``
    objects; ``Domain([...])`` / ``Domain(path, operator, value)`` and ``Domain.AND`` /
    ``OR`` (Odoo 19+, ``domain_class``); ``expression.AND([...])`` / ``OR``; and, with the
    function's :class:`LocalValues`, a variable built before (``domain = [...]``,
    ``domain += [...]``, ``domain.append(term)``, ``domain = expression.AND([domain, ...])``).
    """
    if isinstance(domain, ListExpr):
        yield domain.items
    elif isinstance(domain, OpExpr) and domain.op in DOMAIN_OPERATORS:
        yield from domain_term_lists(domain.left, domain_class, local, depth)
        yield from domain_term_lists(domain.right, domain_class, local, depth)
    elif isinstance(domain, UnaryExpr) and domain.op == "~":
        yield from domain_term_lists(domain.expr, domain_class, local, depth)
    elif isinstance(domain, CallExpr):
        yield from _call_term_lists(domain, domain_class, local, depth)
    elif isinstance(domain, NameExpr) and local is not None and depth < MAX_DEPTH:
        built = local.built(domain, domain.line)
        if built.value is not None:
            yield from domain_term_lists(built.value, domain_class, local, depth + 1)
        for change in built.changes:
            yield from _changed_term_lists(change, domain_class, local, depth + 1)


def _call_term_lists(
    call: CallExpr, domain_class: str | None, local: LocalValues | None, depth: int
) -> Iterator[Sequence[Expression]]:
    if any(kind != ARG_POS for kind in call.arg_kinds):
        return
    callee = call.callee
    if domain_class is not None and isinstance(callee, RefExpr) and callee.fullname == domain_class:
        if len(call.args) == 1:  # Domain([...]), Domain(other)
            yield from domain_term_lists(call.args[0], domain_class, local, depth)
        elif len(call.args) == 3:  # Domain(path, operator, value)
            yield [TupleExpr(list(call.args))]
        return
    combinator = isinstance(callee, RefExpr) and callee.fullname in COMBINATORS
    if (
        domain_class is not None
        and isinstance(callee, MemberExpr)
        and callee.name in {"AND", "OR"}
        and isinstance(callee.expr, RefExpr)
        and callee.expr.fullname == domain_class
    ):
        combinator = True  # Domain.AND([...])
    if combinator and len(call.args) == 1 and isinstance(call.args[0], (ListExpr, TupleExpr)):
        for item in call.args[0].items:
            yield from domain_term_lists(item, domain_class, local, depth)


def _changed_term_lists(
    change: Change, domain_class: str | None, local: LocalValues, depth: int
) -> Iterator[Sequence[Expression]]:
    """The terms an in-place change adds to a domain variable."""
    args = change.args
    if change.method in {"__iadd__", "__ior__", "__iand__", "extend"} and len(args) == 1:
        yield from domain_term_lists(args[0], domain_class, local, depth)
    elif change.method == "append" and len(args) == 1:
        yield [args[0]]
    elif change.method == "insert" and len(args) == 2:
        yield [args[1]]


class DomainChecker:
    """Check the literal terms of domains against the field index.

    Args:
        fail: Reports an error at a node (with the ``odoo-domain`` code).
        fields_of: The fields of a model as the checked addon sees it.
        operators: The term operators of the Odoo version.
        guarded: Whether a field name is guarded on a line of the checked file
            (``if "f" in self._fields:``, :func:`odoo_typing.guards.field_guard`).
    """

    def __init__(
        self,
        fail: Fail,
        fields_of: FieldsOf,
        operators: Collection[str],
        guarded: Callable[[str, int], bool],
    ) -> None:
        self.fail = fail
        self.fields_of = fields_of
        self.operators = operators
        self.guarded = guarded

    def check(self, model: str, items: Sequence[Expression]) -> None:
        """Check the items of a literal domain list of a model."""
        for item in items:
            if isinstance(item, StrExpr):
                if item.value not in LOGICAL_OPERATORS:
                    self.fail(f'Unknown domain operator "{item.value}" (not "&", "|", "!")', item)
                continue
            term = term_of(item)
            if term is not None:
                self.check_term(model, term)

    def check_term(self, model: str, term: Term) -> None:
        """Check the path, the operator and, when it is literal, the value of a term."""
        operator = term.operator.value.lower() if term.operator is not None else None
        if term.operator is not None and operator not in self.operators:
            hint = REPLACEMENTS.get(term.operator.value)
            suggestion = f'; did you mean "{hint}"?' if hint else ""
            if not suggestion:
                suggestion = did_you_mean(term.operator.value, sorted(self.operators))
            self.fail(f'Unknown domain operator "{term.operator.value}"{suggestion}', term.operator)
        result = resolve_path(term.path.value, model, self.fields_of, "domain", DATE_PARTS)
        if result.problem is not None:
            if result.missing is None or not self.guarded(result.missing, term.path.line):
                self.fail(result.problem, term.path)
            return
        field = result.field
        if field is None or operator is None or term.value is None:
            return
        if operator in SUBDOMAIN_OPERATORS and isinstance(term.value, ListExpr):
            if field.field_type in RELATIONAL and field.comodel and "comodel" not in field.dynamic:
                self.check(field.comodel, term.value.items)
        elif operator in SELECTION_OPERATORS:
            self._check_selection(result.model or model, field, term.value)

    def _check_selection(self, model: str, field: FieldInfo, value: Expression) -> None:
        selection = field.selection
        if field.field_type != "Selection" or selection is None or "selection" in field.dynamic:
            return
        values = value.items if isinstance(value, (ListExpr, TupleExpr)) else [value]
        for item in values:
            if isinstance(item, StrExpr) and item.value and item.value not in selection:
                shown = ", ".join(f'"{key}"' for key in selection[:SHOWN_KEYS])
                more = ", ..." if len(selection) > SHOWN_KEYS else ""
                self.fail(
                    f'Value "{item.value}" is not in the selection of field "{field.name}" '
                    f'of Odoo model "{model}" ({shown}{more})',
                    item,
                )


def term_of(item: Expression) -> Term | None:
    """The term a domain item is, if its path is a literal (``("name", "=", x)``)."""
    if not isinstance(item, (TupleExpr, ListExpr)) or len(item.items) != 3:
        return None
    path, operator, value = item.items
    if not isinstance(path, StrExpr) or not path.value:
        return None
    return Term(path, operator if isinstance(operator, StrExpr) else None, value)


def string_domain_paths(source: str) -> Iterator[tuple[str, str | None]]:
    """The paths and operators of the terms of a domain written as a string.

    The client evaluates it: values may name fields of the record (``company_id``) or of
    the parent record, so they are not returned; a path of the parent (``parent.x``)
    is skipped. Nothing is returned for a string that is not a literal list.
    """
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError:
        return
    if not isinstance(tree.body, ast.List):
        return
    for item in tree.body.elts:
        if not isinstance(item, (ast.Tuple, ast.List)) or len(item.elts) != 3:
            continue
        path, operator, _ = item.elts
        if not isinstance(path, ast.Constant) or not isinstance(path.value, str):
            continue
        if not path.value or path.value.startswith("parent."):
            continue
        literal = operator.value if isinstance(operator, ast.Constant) else None
        yield path.value, literal if isinstance(literal, str) else None
