"""Domains: ``search([("partner_id.name", "ilike", "x")])`` and the ``domain=`` of fields.

Odoo checks a domain only when it runs it. The checks follow Odoo's own rules
(``odoo/osv/expression.py`` and ``models.py`` in 17/18, ``odoo/orm/domains.py`` and the
field classes in 19/20) on the literal parts of a domain, which
:mod:`odoo_typing.domain_parts` finds:

* structure, in every domain of an addon file whatever its model: a term has three
  items and starts with a field name, an item is a term or one of ``&`` / ``|`` / ``!``,
  and in a whole domain each operator has its terms (:meth:`DomainChecker.structure`);
* each term whose first item is a string: the path follows the comodels (a date field
  may end it with a date part, a ``Properties`` field with a property name, a
  ``Binary`` field in Odoo 20 with ``size`` / ``filename``), the operator is one of the
  version's operators (case-sensitive except in Odoo 19, which lowers it), and the
  operator and a literal value fit the field (:meth:`DomainChecker.check_term`).

A rule is taken only where Odoo raises or silently does something else, and each
version's operators are first rewritten as Odoo rewrites them (``<>`` to ``!=``, ``=``
with a list to ``in``). Some terms fail only in Odoo's SQL search, not in
``filtered_domain`` (which runs its own Python code in 17/18): those are checked only in
domains that a search takes, with the ``odoo-domain-search`` code
(:class:`Mode`). A domain of another model than the one that takes it is an
``odoo-domain-model`` error (checked by :mod:`odoo_typing.hooks.domains`).

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
    CallExpr,
    Context,
    DictExpr,
    Expression,
    FloatExpr,
    IntExpr,
    ListExpr,
    NameExpr,
    RefExpr,
    SetExpr,
    StarExpr,
    StrExpr,
    TupleExpr,
    UnaryExpr,
)
from mypy.types import Instance, NoneType, ProperType, TupleType

from odoo_typing.compat import DomainRules, OdooProfile
from odoo_typing.model_fields import stored
from odoo_typing.references import (
    DATE_CLASSES,
    KNOWN_CLASSES,
    OPAQUE,
    RELATIONAL,
    did_you_mean,
)
from odoo_typing.scanner import FieldInfo

__all__ = [
    "DOMAIN_METHODS",
    "MISSING",
    "ODOO_DOMAIN",
    "ODOO_DOMAIN_MODEL",
    "ODOO_DOMAIN_SEARCH",
    "PARAMETER_DOMAIN_METHODS",
    "SEARCH_CONSUMERS",
    "DomainChecker",
    "DomainPath",
    "Mode",
    "Term",
    "literal_value",
    "string_domain_paths",
    "term_of",
]

ODOO_DOMAIN = ErrorCode("odoo-domain", "Check field paths and operators of Odoo domains", "Odoo")
ODOO_DOMAIN_MODEL = ErrorCode(
    "odoo-domain-model",
    "Check that a domain is a domain of the Odoo model that takes it",
    "Odoo",
    sub_code_of=ODOO_DOMAIN,
)
ODOO_DOMAIN_SEARCH = ErrorCode(
    "odoo-domain-search",
    "Check domain terms that Odoo's search rejects or ignores",
    "Odoo",
    sub_code_of=ODOO_DOMAIN,
)

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
# Methods whose domain is found by the name of its parameter only (``name_search(name,
# domain)``: Odoo 17/18 call it ``args``).
PARAMETER_DOMAIN_METHODS: Final[Mapping[str, tuple[str, ...]]] = {
    "name_search": ("domain", "args"),
    "_name_search": ("domain",),
    "_read_grouping_sets": ("domain",),
}
# The methods that run a domain in SQL (all but ``filtered_domain``).
SEARCH_CONSUMERS: Final = (DOMAIN_METHODS | set(PARAMETER_DOMAIN_METHODS)) - {"filtered_domain"}

LOGICAL_OPERATORS: Final = frozenset({"&", "|", "!"})
SUBDOMAIN_OPERATORS: Final = frozenset({"any", "not any", "any!", "not any!"})
INTERNAL_OPERATORS: Final = frozenset({"any!", "not any!"})
SELECTION_OPERATORS: Final = frozenset({"=", "!=", "in", "not in"})
INEQUALITIES: Final = frozenset({"<", ">", "<=", ">="})
LIKE_OPERATORS: Final = frozenset(
    {"like", "not like", "ilike", "not ilike", "=like", "=ilike", "not =like", "not =ilike"}
)
PATTERN_OPERATORS: Final = frozenset({"=like", "=ilike", "not =like", "not =ilike"})
HIERARCHY_OPERATORS: Final = frozenset({"child_of", "parent_of"})
# Operators that compare a field with its value (records there fail in Odoo 17/18's SQL).
COMPARISONS: Final = frozenset({"=", "!=", "in", "not in"}) | INEQUALITIES
EXISTENCE_OPERATORS: Final = frozenset({"=", "!=", "in", "not in"})
ACCESS_VALUES: Final = ("read", "write", "create", "unlink")
X2MANY: Final = frozenset({"One2many", "Many2many"})
BINARY: Final = frozenset({"Binary", "Image"})
NUMBERS: Final = {"Integer": int, "Float": float}
# ``(1, "=", 1)`` / ``(0, "=", 1)``: Odoo's TRUE_LEAF / FALSE_LEAF.
CONSTANT_TERMS: Final = frozenset({(1, "=", 1), (0, "=", 1)})
# Operators that difflib does not match to their replacement.
REPLACEMENTS: Final = {"<>": "!=", "==": "="}
SHOWN_KEYS: Final = 5
WRONG: Final = ""  # a path's tail that is wrong (reported)

Fail = Callable[[str, Context, ErrorCode], object]
FieldsOf = Callable[[str], Mapping[str, FieldInfo]]
TypeOf = Callable[[Expression], ProperType | None]


class _Missing:
    """No literal value (a variable, a call)."""


MISSING: Final = _Missing()


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


@dataclass(frozen=True, slots=True)
class Mode:
    """Which rules a domain is checked with.

    Attributes:
        search: The domain goes to Odoo's SQL search (a search method, ``domain=`` of a
            field, what a field's search method returns): the rules that only the search
            enforces apply too (``odoo-domain-search``).
        internal: Odoo reads the domain as an internal one (what a field's search method
            returns): ``any!`` / ``not any!`` may be list terms, and Odoo 17 takes
            ``inselect``.
    """

    search: bool
    internal: bool = False


@dataclass(frozen=True, slots=True)
class DomainPath:
    """What the path of a term resolves to.

    Attributes:
        steps: Each field of the path with the model it belongs to.
        tail: What follows the last field, if it is not a field: a date part, a
            property name, a ``Binary`` sub-path.
        tail_kind: ``"date"``, ``"property"`` or ``"binary"`` for a tail.
        known: Whether every step is known (not a comodel or field class the scan does
            not know).
    """

    steps: tuple[tuple[str, FieldInfo], ...]
    tail: str | None = None
    tail_kind: str | None = None
    known: bool = True

    @property
    def model(self) -> str:
        """The model of the last field."""
        return self.steps[-1][0]

    @property
    def field(self) -> FieldInfo:
        """The last field."""
        return self.steps[-1][1]


class DomainChecker:
    """Check the literal items of domains against the field index.

    Args:
        fail: Reports an error at a node, with a code.
        fields_of: The fields of a model as the checked addon sees it.
        profile: The profile of the Odoo version.
        guarded: Whether a field name is guarded on a line of the checked file
            (``if "f" in self._fields:``, :func:`odoo_typing.guards.field_guard`).
        type_of: The type mypy inferred for an expression (``None``: not known).
        describe: How a message names a type.
        subdomain: Checks the value of an ``any`` term as a domain of a model
            (the value, the model, the field, the term's model, the mode).
    """

    def __init__(
        self,
        fail: Fail,
        fields_of: FieldsOf,
        profile: OdooProfile,
        guarded: Callable[[str, int], bool],
        type_of: TypeOf,
        describe: Callable[[ProperType], str],
        subdomain: Callable[[Expression, str, FieldInfo, str, Mode], None],
    ) -> None:
        self.fail = fail
        self.fields_of = fields_of
        self.operators: Collection[str] = profile.domain_operators
        self.rules: DomainRules = profile.domain_rules
        self.version = profile.version
        self.base_model = profile.base_model
        self.domain_class = profile.domain_class
        self.guarded = guarded
        self.type_of = type_of
        self.describe = describe
        self.subdomain = subdomain

    # -- structure (any model) -------------------------------------------------------------

    def structure(self, items: Sequence[Expression], whole: bool) -> None:
        """Check the items of a literal domain list: terms, operators and their arity."""
        for item in items:
            if isinstance(item, StarExpr):
                continue  # [*terms, ...]: its items are not known
            if isinstance(item, StrExpr):
                if item.value not in LOGICAL_OPERATORS:
                    message = f'Unknown domain operator "{item.value}" (not "&", "|", "!")'
                    self.fail(message, item, ODOO_DOMAIN)
                continue
            nested = self.not_a_term(item)
            if nested is not None:
                self.fail(
                    f'Domain item of type "{nested}" is not a term or an operator',
                    item,
                    ODOO_DOMAIN,
                )
                continue
            if isinstance(item, TupleExpr | ListExpr):
                self._check_term_shape(item)
        if whole:
            self._check_arity(items)

    def not_a_term(self, item: Expression) -> str | None:
        """The type of an item that is neither a term nor an operator, as a message names it.

        ``None``, a dict, a number, or a list of terms (a domain where a term should be:
        ``[[...], ...]``); items that may be a term or an operator (a variable typed
        ``list[str]``, ``tuple[...]``, ``str``, ``Any``) are not.
        """
        if isinstance(item, StarExpr):
            return None
        if isinstance(item, ListExpr | TupleExpr) and _is_nested_domain(item):
            return "list" if isinstance(item, ListExpr) else "tuple"  # a literal domain
        found = self.type_of(item)
        if found is None:
            return None
        if isinstance(found, NoneType):
            return "None"
        if isinstance(found, TupleType):
            items = found.items
            return self.describe(found) if items and all(_is_tuple(i) for i in items) else None
        if not isinstance(found, Instance):
            return None
        name = found.type.fullname
        if name in {"builtins.dict", "builtins.int", "builtins.float"}:
            return self.describe(found)
        if name in {"builtins.list", "builtins.tuple", "builtins.set"} and found.args:
            return self.describe(found) if _is_tuple(found.args[0]) else None
        return None

    def _check_term_shape(self, item: TupleExpr | ListExpr) -> None:
        if any(isinstance(part, StarExpr) for part in item.items):
            return  # ("name", *rest): its length is not known
        if len(item.items) != 3:
            message = f"Domain term must be (field, operator, value), not {len(item.items)} items"
            self.fail(message, item, ODOO_DOMAIN)
            return
        first = item.items[0]
        value = literal_value(first)
        if isinstance(first, StrExpr) or value is MISSING:
            return
        if tuple(literal_value(part) for part in item.items) in CONSTANT_TERMS:
            return
        self.fail(
            f"Domain term must start with a field name, not {_shown(value)}", first, ODOO_DOMAIN
        )

    def _check_arity(self, items: Sequence[Expression]) -> None:
        """Each ``&`` / ``|`` of a whole domain has two terms after it, ``!`` one.

        Read from the end, as a prefix expression: a term adds an operand, an operator
        takes its operands and gives one; more than one at the end is an implicit AND.
        Only for lists of literal terms and operators.
        """
        if not all(isinstance(item, StrExpr | TupleExpr | ListExpr) for item in items):
            return
        count = 0
        for item in reversed(items):
            if not isinstance(item, StrExpr):
                count += 1
                continue
            needed = 1 if item.value == "!" else 2 if item.value in {"&", "|"} else None
            if needed is None:
                return  # an unknown operator (reported)
            if count < needed:
                terms = "term" if needed == 1 else "terms"
                message = f'Domain operator "{item.value}" needs {needed} {terms}, has {count}'
                self.fail(message, item, ODOO_DOMAIN)
                return
            count -= needed - 1

    # -- terms of a model ------------------------------------------------------------------

    def check(
        self, model: str, items: Sequence[Expression], mode: Mode, condition: bool = False
    ) -> None:
        """Check the terms of a literal domain list against a model's fields.

        ``condition``: the list is the one term of ``Domain(path, operator, value)``.
        """
        for item in items:
            if isinstance(item, StrExpr) or self.not_a_term(item) is not None:
                continue
            term = term_of(item)
            if term is not None:
                self.check_term(model, term, mode, condition)

    def check_term(self, model: str, term: Term, mode: Mode, condition: bool = False) -> None:
        """Check the path, the operator and, when it is literal, the value of a term."""
        operator = self._operator(term, mode)
        path = self._path(term.path, model)
        if path is None:
            return
        if mode.search:
            self._check_searchable(path, term.path)
        if operator is None or term.value is None:
            return
        if path.tail_kind is not None:
            self._check_tail(path, operator, term)
        elif path.known:
            self._check_field(path, operator, term, mode, condition)

    def _operator(self, term: Term, mode: Mode) -> str | None:
        """The operator as Odoo rewrites it (``<>`` to ``!=``); reported if unknown."""
        node = term.operator
        if node is None:
            return None
        written = node.value
        rules = self.rules
        operator = written.lower() if rules.lower_case_operators else written
        if mode.internal and operator in rules.internal_operators:
            return None  # inselect: an SQL query, nothing to check
        if operator not in self.operators:
            hint = REPLACEMENTS.get(written)
            if hint is None and written.lower() in self.operators:
                hint = written.lower()
            suggestion = f'; did you mean "{hint}"?' if hint else ""
            if not suggestion:
                suggestion = did_you_mean(written, sorted(self.operators))
            self.fail(f'Unknown domain operator "{written}"{suggestion}', node, ODOO_DOMAIN)
            return None
        operator = rules.renamed_operators.get(operator, operator)
        value = term.value
        if operator in {"=", "!="} and isinstance(value, ListExpr | TupleExpr):
            operator = "in" if operator == "=" else "not in"
        if (
            rules.strict_in
            and operator in {"in", "not in"}
            and isinstance(literal_value(value), bool)
        ):
            operator = "=" if operator == "in" else "!="  # normalize_leaf
        return operator

    def _path(self, node: StrExpr, model: str) -> DomainPath | None:
        """Follow a term's path (``partner_id.country_id.code``); report it if it is wrong."""
        names = node.value.split(".")
        steps: list[tuple[str, FieldInfo]] = []
        current = model
        for position, name in enumerate(names):
            fields = self.fields_of(current)
            if not fields:
                return DomainPath(tuple(steps), known=False) if steps else None
            info = fields.get(name)
            if info is None:
                if not self.guarded(name, node.line):
                    message = f'Odoo model "{current}" has no field "{name}" in domain'
                    self.fail(message + did_you_mean(name, fields), node, ODOO_DOMAIN)
                return None
            steps.append((current, info))
            rest = names[position + 1 :]
            if not rest:
                break
            found = self._tail(info, current, rest, node)
            if found == WRONG:
                return None
            if found is not None:
                return DomainPath(tuple(steps), ".".join(rest), found)
            if info.field_type not in RELATIONAL:
                if info.field_type not in KNOWN_CLASSES or info.field_type in OPAQUE:
                    return DomainPath(tuple(steps), known=False)
                message = f'Field "{name}" of Odoo model "{current}" in domain is not relational'
                if info.field_type in DATE_CLASSES and not self.rules.date_parts:
                    message += f" (Odoo {self.version} domains have no date parts)"
                self.fail(message, node, ODOO_DOMAIN)
                return None
            if info.comodel is None or "comodel" in info.dynamic:
                return DomainPath(tuple(steps), known=False)
            current = info.comodel
        return DomainPath(tuple(steps))

    def _tail(self, info: FieldInfo, model: str, rest: list[str], node: StrExpr) -> str | None:
        """What follows a field if it is not a field (``"date"``, ``"property"``, ``"binary"``).

        :data:`WRONG` (reported) for a wrong one, ``None`` when a field follows.
        """
        rules = self.rules
        if info.field_type in DATE_CLASSES and rules.date_parts and len(rest) == 1:
            part = rest[0]
            if part in rules.date_parts:
                return "date"
            message = (
                f'"{part}" of date field "{info.name}" of Odoo model "{model}" in domain is not '
                "a date part"
            )
            self.fail(message + did_you_mean(part, rules.date_parts), node, ODOO_DOMAIN)
            return WRONG
        if info.field_type == "Properties":
            if rules.properties_one_step and len(rest) > 1:
                message = (
                    f'Property path "{node.value}" of Odoo model "{model}" in domain must be '
                    f'"{info.name}.<property>"'
                )
                self.fail(message, node, ODOO_DOMAIN)
                return WRONG
            return "property"
        if info.field_type in BINARY and len(rest) == 1 and rest[0] in rules.binary_sub_paths:
            return "binary"
        return None

    def _check_tail(self, path: DomainPath, operator: str, term: Term) -> None:
        """The operator of a date part or a property path."""
        assert term.operator is not None
        if path.tail_kind == "date" and operator in self.rules.date_part_operators:
            message = (
                f'Operator "{operator}" cannot compare a date part ("{term.path.value}") in '
                f"Odoo {self.version}"
            )
            self.fail(message, term.operator, ODOO_DOMAIN)
        elif path.tail_kind == "property" and operator not in self.rules.properties_operators:
            message = f'Operator "{operator}" cannot compare a property ("{term.path.value}")'
            self.fail(message, term.operator, ODOO_DOMAIN)

    # -- operator and value against the field ----------------------------------------------

    def _check_field(
        self, path: DomainPath, operator: str, term: Term, mode: Mode, condition: bool
    ) -> None:
        """The rules of an operator and a value on the last field of a path."""
        assert term.operator is not None and term.value is not None
        field, model, value = path.field, path.model, term.value
        literal = literal_value(value)
        kind = field.field_type
        if operator == "access" and self.rules.access_operator:
            self._check_access(field, model, term.operator, value, literal)
            return
        internal = operator in INTERNAL_OPERATORS and self.rules.internal_any
        if internal and not condition and not mode.internal:
            message = (
                f'Operator "{operator}" is not allowed in a list domain (Odoo '
                f'{self.version} raises); use Domain("{term.path.value}", "{operator}", ...)'
            )
            self.fail(message, term.operator, ODOO_DOMAIN)
        if operator in SUBDOMAIN_OPERATORS:
            self._check_any(path, operator, term, mode)
            return
        if operator in HIERARCHY_OPERATORS and kind in KNOWN_CLASSES and kind not in RELATIONAL:
            if kind != "Id":
                self.fail(_needs_relational(operator, field, model), term.operator, ODOO_DOMAIN)
            return
        if operator in SELECTION_OPERATORS:
            self._check_selection(model, field, value)
        self._check_comparison(field, model, operator, term, literal, mode)
        self._check_value(field, model, operator, term, literal, mode)

    def _check_access(
        self, field: FieldInfo, model: str, node: StrExpr, value: Expression, literal: object
    ) -> None:
        """Odoo 20's ``access``: on a ``Many2one`` or ``id``, with an operation."""
        kind = field.field_type
        if kind in KNOWN_CLASSES and kind not in {"Many2one", "Id"}:
            message = (
                f'Operator "access" works only on Many2one fields and "id", not on the {kind} '
                f'field "{field.name}" of Odoo model "{model}"'
            )
            self.fail(message, node, ODOO_DOMAIN)
        if literal is not MISSING and literal not in ACCESS_VALUES:
            names = ", ".join(f'"{name}"' for name in ACCESS_VALUES)
            message = f'Operator "access" takes one of {names}, not {_shown(literal)}'
            self.fail(message, value, ODOO_DOMAIN)

    def _check_any(self, path: DomainPath, operator: str, term: Term, mode: Mode) -> None:
        """``any`` / ``not any``: a relational field (or ``id``), and its sub-domain."""
        assert term.operator is not None and term.value is not None
        field, model, value = path.field, path.model, term.value
        kind = field.field_type
        domain_like = isinstance(value, ListExpr | TupleExpr) or self._is_domain_call(value)
        if kind in RELATIONAL:
            if field.comodel and "comodel" not in field.dynamic:
                self.subdomain(value, field.comodel, field, model, self._sub_mode(mode))
        elif kind == "Id":
            if self.rules.python_filter:
                if domain_like:
                    message = (
                        f'Operator "{operator}" on "id" fails in an Odoo {self.version} search '
                        "(it takes a relational field)"
                    )
                    self._fail(message, term.operator, mode, search=True)
            else:
                self.subdomain(value, model, field, model, self._sub_mode(mode))  # its own
        elif kind in KNOWN_CLASSES and domain_like:
            self.fail(_needs_relational(operator, field, model), term.operator, ODOO_DOMAIN)

    def _sub_mode(self, mode: Mode) -> Mode:
        """The mode of an ``any`` sub-domain: internal only where Odoo passes it on (19+).

        Odoo 17/18 parse a sub-domain as a plain domain (no ``inselect``); 19+ keep the
        internal flag of the domain it is in.
        """
        return Mode(search=mode.search, internal=mode.internal and self.rules.internal_any)

    def _is_domain_call(self, value: Expression) -> bool:
        """``Domain(...)`` (Odoo 19+)."""
        return (
            self.domain_class is not None
            and isinstance(value, CallExpr)
            and isinstance(value.callee, RefExpr)
            and value.callee.fullname == self.domain_class
        )

    def _check_comparison(
        self,
        field: FieldInfo,
        model: str,
        operator: str,
        term: Term,
        literal: object,
        mode: Mode,
    ) -> None:
        """Inequalities on relational and boolean fields, ``=like`` patterns (T3-T6)."""
        assert term.operator is not None
        kind, node = field.field_type, term.operator
        python_filter = self.rules.python_filter
        if kind in X2MANY and operator in INEQUALITIES:
            message = (
                f'Operator "{operator}" cannot compare the {kind} field "{field.name}" of Odoo '
                f'model "{model}" in a search'
            )
            self._fail(message, node, mode, search=True)
        elif kind == "Many2one" and operator in INEQUALITIES and isinstance(literal, str):
            consequence = (
                f"Odoo {self.version} searches the names instead"
                if python_filter
                else f"Odoo {self.version} raises"
            )
            message = (
                f'Operator "{operator}" with a string on the Many2one field "{field.name}" of '
                f'Odoo model "{model}": {consequence}'
            )
            self._fail(message, node, mode, search=python_filter)
        if python_filter:
            return
        if kind == "Boolean" and (
            operator in INEQUALITIES or (operator in LIKE_OPERATORS and _filled(literal))
        ):
            message = (
                f'Operator "{operator}" cannot compare the Boolean field "{field.name}" of Odoo '
                f'model "{model}"'
            )
            self.fail(message, node, ODOO_DOMAIN)
        if operator in PATTERN_OPERATORS and _filled(literal) and not isinstance(literal, str):
            message = f'Operator "{operator}" needs a string pattern, not {_shown(literal)}'
            self.fail(message, node, ODOO_DOMAIN)

    def _check_value(
        self,
        field: FieldInfo,
        model: str,
        operator: str,
        term: Term,
        literal: object,
        mode: Mode,
    ) -> None:
        """The value against the field: numbers, scalars for ``in``, records, dates (V1-V4)."""
        assert term.operator is not None and term.value is not None
        kind, value = field.field_type, term.value
        if kind in BINARY:
            self._check_binary(field, model, operator, term, literal, mode)
            return
        if kind in NUMBERS and operator not in LIKE_OPERATORS:
            for item, text in _strings(value):
                if text and not _parses(NUMBERS[kind], text):
                    message = (
                        f'Value "{text}" of the {kind} field "{field.name}" of Odoo model '
                        f'"{model}" is not a number: the search raises'
                    )
                    self._fail(message, item, mode, search=True)
        if self._scalar_for_in(field, operator, value, literal):
            message = (
                f'Operator "{operator}" needs a list, not {_shown(literal, value)} (field '
                f'"{field.name}" of Odoo model "{model}"): Odoo {self.version} raises'
            )
            self._fail(message, value, mode, search=True)
        if self._records_value(field, operator, value):
            message = (
                f'Records as the value of field "{field.name}" of Odoo model "{model}": Odoo '
                f"{self.version} searches take ids (.id, .ids)"
            )
            self._fail(message, value, mode, search=True)
        if self._integer_date(field, operator, literal):
            message = (
                f'Value {literal} of the {kind} field "{field.name}" of Odoo model "{model}" is '
                "not a date"
            )
            self.fail(message, value, ODOO_DOMAIN)

    def _scalar_for_in(
        self, field: FieldInfo, operator: str, value: Expression, literal: object
    ) -> bool:
        """V2: ``in`` with a scalar (or a set) on a stored non-relational field (Odoo 17/18)."""
        scalar = literal is not MISSING and not isinstance(literal, list | tuple)
        kind = field.field_type
        return (
            self.rules.strict_in
            and operator in {"in", "not in"}
            and (scalar or isinstance(value, SetExpr))
            and kind not in RELATIONAL
            and (kind == "Id" or stored(field))
        )

    def _records_value(self, field: FieldInfo, operator: str, value: Expression) -> bool:
        """V3: records compared with a field other than an x2many (Odoo 17/18 assert it).

        Also ``id``: its term reaches the same SQL builder (``("id", "in", records)``).
        """
        kind = field.field_type
        return (
            self.rules.record_values
            and operator in COMPARISONS
            and kind not in X2MANY
            and self._is_records(value)
        )

    def _integer_date(self, field: FieldInfo, operator: str, literal: object) -> bool:
        """V4: an integer for a date field (Odoo 19+ cannot cast it).

        Not ``=?`` with ``0``: Odoo turns ``=?`` with a falsy value into a true condition.
        """
        return (
            not self.rules.python_filter
            and field.field_type in DATE_CLASSES
            and operator not in LIKE_OPERATORS
            and isinstance(literal, int)
            and not isinstance(literal, bool)
            and not (operator == "=?" and not literal)
        )

    def _check_binary(
        self,
        field: FieldInfo,
        model: str,
        operator: str,
        term: Term,
        literal: object,
        mode: Mode,
    ) -> None:
        """A stored ``Binary`` (as an attachment) takes only existence checks (F2).

        A related field (also one ``_inherits`` delegates) is searched as the field at the
        end of its path. Another field that is not stored is searched by its search method
        (Odoo sets ``attachment=False`` there), or is the searchable rule's (F1).
        """
        assert term.operator is not None and term.value is not None
        value = term.value
        target = self._searched_binary(model, field, frozenset())
        if target is None or "attachment" in target.dynamic:
            return
        if not self.rules.binary_check_all and target.attachment is False:
            return
        if operator in EXISTENCE_OPERATORS:
            if operator in {"=", "!="}:
                wrong = _filled(literal)
            else:
                items = value.items if isinstance(value, ListExpr | TupleExpr) else []
                wrong = any(_filled(literal_value(item)) for item in items)
            if not wrong:
                return
        message = (
            f'Binary field "{field.name}" of Odoo model "{model}" takes only existence checks in '
            'a search ("=" / "!=" with False)'
        )
        node = value if operator in EXISTENCE_OPERATORS else term.operator
        self._fail(message, node, mode, search=True)

    def _searched_binary(
        self, model: str, field: FieldInfo, seen: frozenset[tuple[str, str]]
    ) -> FieldInfo | None:
        """The stored ``Binary`` a term on a field searches: itself, or its related target.

        ``None`` for a field without one (a computed field, a path the scan cannot follow,
        a cycle).
        """
        if field.dynamic & {"store", "related"} or (model, field.name) in seen:
            return None
        if stored(field):
            return field
        if field.related is None:
            return None
        current, names = model, field.related.split(".")
        for position, name in enumerate(names):
            step = self.fields_of(current).get(name)
            if step is None:
                return None
            if position == len(names) - 1:
                if step.field_type not in BINARY:
                    return None
                return self._searched_binary(current, step, seen | {(model, field.name)})
            if step.field_type not in RELATIONAL or not step.comodel or "comodel" in step.dynamic:
                return None
            current = step.comodel
        return None

    def _is_records(self, value: Expression) -> bool:
        """Whether mypy types a value as the records of a model (not a union)."""
        found = self.type_of(value)
        return isinstance(found, Instance) and found.type.has_base(self.base_model)

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
                    ODOO_DOMAIN,
                )

    # -- searchable fields -----------------------------------------------------------------

    def _check_searchable(self, path: DomainPath, node: StrExpr) -> None:
        """Each field of the path must be searchable: stored, or with a search method (F1)."""
        for model, info in path.steps:
            if not self.searchable(model, info, frozenset()):
                consequence = (
                    f"Odoo {self.version} raises"
                    if self.rules.unsearchable_raises
                    else f"Odoo {self.version} ignores the condition and the search matches every "
                    "record"
                )
                message = (
                    f'Field "{info.name}" of Odoo model "{model}" cannot be searched (not stored, '
                    f"no search method): {consequence}"
                )
                self.fail(message, node, ODOO_DOMAIN_SEARCH)
                return

    def searchable(self, model: str, info: FieldInfo, seen: frozenset[tuple[str, str]]) -> bool:
        """Whether Odoo can search a field (``_description_searchable``).

        Stored, or with a search method, or (Odoo 20) with ``compute_sql``, or a related
        field every field of whose path is searchable. What the scan cannot tell (an
        argument it cannot evaluate, a path step it cannot follow, a cycle) counts as
        searchable.
        """
        if info.dynamic & {"store", "search", "related", "compute", "compute_sql"}:
            return True
        if stored(info) or info.search is not None:
            return True
        if info.compute_sql and self.rules.searchable_compute_sql:
            return True
        if info.related is None:
            return False
        key = (model, info.name)
        if key in seen:
            return True
        current, names = model, info.related.split(".")
        for position, name in enumerate(names):
            step = self.fields_of(current).get(name)
            if step is None:
                return True
            if not self.searchable(current, step, seen | {key}):
                return False
            if position < len(names) - 1:
                if (
                    step.field_type not in RELATIONAL
                    or not step.comodel
                    or "comodel" in step.dynamic
                ):
                    return True
                current = step.comodel
        return True

    def _fail(self, message: str, node: Context, mode: Mode, *, search: bool) -> None:
        """Report a rule that only the search enforces (``search``) or one everywhere."""
        if not search:
            self.fail(message, node, ODOO_DOMAIN)
        elif mode.search:
            self.fail(message, node, ODOO_DOMAIN_SEARCH)


def _needs_relational(operator: str, field: FieldInfo, model: str) -> str:
    return (
        f'Operator "{operator}" needs a relational field, not the {field.field_type} field '
        f'"{field.name}" of Odoo model "{model}"'
    )


def term_of(item: Expression) -> Term | None:
    """The term a domain item is, if its path is a literal (``("name", "=", x)``)."""
    if not isinstance(item, (TupleExpr, ListExpr)) or len(item.items) != 3:
        return None
    path, operator, value = item.items
    if not isinstance(path, StrExpr) or not path.value:
        return None
    return Term(path, operator if isinstance(operator, StrExpr) else None, value)


def literal_value(node: Expression | None) -> object:
    """The value of a literal (string, number, ``True`` / ``False`` / ``None``, a list of them).

    :data:`MISSING` for anything else (a variable, a call). A list or a set gives a list
    of its values, a tuple a tuple.
    """
    if isinstance(node, StrExpr | IntExpr | FloatExpr):
        return node.value
    if isinstance(node, NameExpr) and node.name in {"True", "False", "None"}:
        return {"True": True, "False": False, "None": None}[node.name]
    if (
        isinstance(node, UnaryExpr)
        and node.op == "-"
        and isinstance(node.expr, IntExpr | FloatExpr)
    ):
        return -node.expr.value
    if isinstance(node, ListExpr | TupleExpr | SetExpr):
        values = [literal_value(item) for item in node.items]
        if any(value is MISSING for value in values):
            return MISSING
        return tuple(values) if isinstance(node, TupleExpr) else values
    if isinstance(node, DictExpr) and not node.items:
        return {}
    return MISSING


def _filled(literal: object) -> bool:
    """Whether a value is a literal that is not empty (``"x"``, ``1``, ``True``, ``[0]``)."""
    return literal is not MISSING and bool(literal)


def _shown(literal: object, node: Expression | None = None) -> str:
    """A literal as a message shows it."""
    if isinstance(node, SetExpr):
        return "a set"
    if literal is MISSING:
        return "this value"
    if isinstance(literal, str):
        return f'"{literal}"'
    if isinstance(literal, list | tuple):
        return "a list"
    return repr(literal)


def _strings(value: Expression) -> Iterator[tuple[Expression, str]]:
    """The string literals a value is or has (``"x"``, ``["x", "y"]``)."""
    items = value.items if isinstance(value, ListExpr | TupleExpr | SetExpr) else [value]
    for item in items:
        if isinstance(item, StrExpr):
            yield item, item.value


def _parses(kind: type[int] | type[float], text: str) -> bool:
    try:
        kind(text)
    except ValueError:
        return False
    return True


def _is_tuple(typ: object) -> bool:
    return isinstance(typ, TupleType) or (
        isinstance(typ, Instance) and typ.type.fullname == "builtins.tuple"
    )


def _is_nested_domain(item: ListExpr | TupleExpr) -> bool:
    """Whether a literal item is a domain itself: its first item is a term."""
    first = item.items[0] if item.items else None
    if isinstance(first, ListExpr | TupleExpr):
        return True
    second = item.items[1] if len(item.items) > 1 else None
    return (
        isinstance(first, StrExpr)
        and first.value in LOGICAL_OPERATORS
        and isinstance(second, ListExpr | TupleExpr)
    )


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
