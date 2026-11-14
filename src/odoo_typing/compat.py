"""Per-version profiles: the names of the Odoo classes and functions the hooks target.

mypy calls a plugin hook with the full name of the class or function where it is
*defined*, after following re-exports. Odoo 19 moved the ORM into ``odoo.orm.*``, and
``odoo.fields`` / ``odoo.models`` / ``odoo.api`` only re-export it, so hooks keyed on
``odoo.fields.Many2one`` stop firing there (Odoo 20 keeps the names of 19). Every such
name lives in a profile; the ``odoo_version`` setting selects it.

Supported versions are 17 to 20; a version without a profile is reported as not
supported yet (see ROADMAP.md, "Версії Odoo").
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

__all__ = ["SUPPORTED_VERSIONS", "DomainRules", "OdooProfile", "profile_for"]

SUPPORTED_VERSIONS = (17, 18, 19, 20)


@dataclass(frozen=True, slots=True)
class DomainRules:
    """What Odoo accepts in a domain term, where versions differ (:mod:`odoo_typing.domains`).

    Attributes:
        date_parts: Names that may follow a date field as the last step of a path
            (``date.year_number``); none in Odoo 17.
        lower_case_operators: Whether an operator in upper case is taken in lower case
            (Odoo 19, with a warning); elsewhere it is an unknown operator.
        renamed_operators: Operators each version rewrites first (``<>`` to ``!=``).
        properties_operators: The operators a ``Properties`` path takes.
        properties_one_step: Whether a ``Properties`` path is exactly ``field.property``
            (Odoo 17/18: a longer one is a wrong path).
        binary_sub_paths: Names that may follow a ``Binary`` field (Odoo 20: ``size``,
            ``filename``).
        searchable_compute_sql: Whether a field with ``compute_sql=`` is searchable (20).
        binary_check_all: Whether every ``Binary`` field takes only existence checks
            (Odoo 20); before, only those stored as attachments.
        strict_in: Whether ``in`` / ``not in`` with a scalar is an invalid term (17/18).
        record_values: Whether records as a value are an error (17/18 assert it).
        internal_any: Whether ``any!`` / ``not any!`` are rejected in a list term (19+).
        python_filter: Whether ``filtered_domain`` runs Python code of its own (17/18)
            rather than the optimized domain (19+): some terms then fail only in SQL.
        unsearchable_raises: Whether a term on an unsearchable field raises (19+) rather
            than matching every record (17/18).
        access_operator: Whether ``access`` is an operator (Odoo 20).
        date_part_operators: Operators a date part cannot take (Odoo 18 builds its SQL
            from ``SQL_OPERATORS``, which has no ``=?``, ``child_of``, ``parent_of``).
        internal_operators: Operators only an internal domain takes (Odoo 17's
            ``inselect``): what a field's search method returns.
    """

    date_parts: frozenset[str]
    lower_case_operators: bool
    renamed_operators: Mapping[str, str]
    properties_operators: frozenset[str]
    properties_one_step: bool
    binary_sub_paths: frozenset[str]
    searchable_compute_sql: bool
    binary_check_all: bool
    strict_in: bool
    record_values: bool
    internal_any: bool
    python_filter: bool
    unsearchable_raises: bool
    access_operator: bool
    date_part_operators: frozenset[str] = frozenset()
    internal_operators: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class OdooProfile:
    """Names that differ between Odoo versions.

    Attributes:
        version: Odoo major version.
        min_python: Oldest Python the Odoo sources parse with (``MIN_PY_VERSION``).
        namespace_odoo: Whether ``odoo`` and ``odoo.addons`` are namespace packages
            (no ``__init__.py``): mypy then names files by their package bases only
            with ``explicit_package_bases``.
        base_model: Full name of ``BaseModel``.
        new_id: Full name of ``NewId`` (the ids of new records: ``record.id`` is
            ``int | NewId``).
        env_getitem: Full name of ``Environment.__getitem__``.
        env_records: Full names of ``Environment`` attributes that are records
            (``env.user``), each with its model.
        relational_classes: Full names of ``Many2one``, ``One2many``, ``Many2many``.
        relational_base: Full name of their common base class.
        field_base: Full name of ``Field``, the base of every field class.
        comodel_generic: Field classes the plugin makes generic in the comodel, where
            Odoo declares them ``Field[BaseModel]`` (Odoo 19+).
        field_modules: Prefixes of the full names of field classes (``fields.Char``).
        many2one: Full name of ``fields.Many2one``.
        datetime: Full name of ``fields.Datetime``.
        identity_decorators: Decorators that return the decorated method.
        identity_factories: Decorator factories whose decorator returns the method.
        identity_template: A typed ``def [T] (T) -> T`` to give those decorators;
            ``None`` where Odoo has none (17): the plugin builds one.
        field_decorators: Full names of ``api.depends`` / ``constrains`` / ``onchange``,
            each with its short name: their arguments name fields of the model.
        domain_operators: The operators of a domain term (``"="``, ``"child_of"``...).
        domain_class: Full name of the ``Domain`` class (Odoo 19+), ``None`` before.
        domain_rules: What a domain term accepts in this version (:class:`DomainRules`).
        sorted_order: Whether ``sorted("...")`` takes an order (``"name desc, id"``,
            Odoo 19+) rather than one field name.
        request_var: Full name of ``odoo.http.request`` when Odoo leaves it untyped
            (a werkzeug ``LocalProxy``), ``None`` when Odoo declares its type.
        request_class: Full name of ``odoo.http.Request``.
        untyped_fields: Whether field classes have no value type (Odoo 17: ``Field``
            is not generic and ``__get__`` is unannotated), so the plugin types reading
            them by class, and relational fields by the comodel of the field index.
    """

    version: int
    min_python: tuple[int, int]
    namespace_odoo: bool
    base_model: str
    new_id: str
    env_getitem: str
    env_records: Mapping[str, str]
    relational_classes: frozenset[str]
    relational_base: str
    field_base: str
    comodel_generic: frozenset[str]
    field_modules: tuple[str, ...]
    many2one: str
    datetime: str
    identity_decorators: frozenset[str]
    identity_factories: frozenset[str]
    identity_template: str | None
    field_decorators: Mapping[str, str]
    domain_operators: frozenset[str]
    domain_class: str | None
    domain_rules: DomainRules
    sorted_order: bool
    request_var: str | None
    request_class: str
    untyped_fields: bool = False

    @property
    def environment(self) -> str:
        """Full name of ``Environment``."""
        return self.env_getitem.rpartition(".")[0]

    def is_field_class(self, fullname: str) -> bool:
        """Whether a full name is that of a field class (``odoo.fields.Char``...)."""
        return fullname.startswith(self.field_modules)


FIELD_DECORATORS = ("depends", "constrains", "onchange")


def _field_decorators(module: str) -> Mapping[str, str]:
    return MappingProxyType({f"{module}.{name}": name for name in FIELD_DECORATORS})


# odoo/osv/expression.py: TERM_OPERATORS, and '<>' that normalize_leaf rewrites.
_OPERATORS_18 = frozenset(
    {"=", "!=", "<>", "<=", "<", ">", ">=", "=?", "=like", "=ilike", "like", "not like"}
    | {"ilike", "not ilike", "in", "not in", "child_of", "parent_of", "any", "not any"}
)
# odoo/orm/domains.py: STANDARD_CONDITION_OPERATORS, then the operators that the
# ``@operator_optimization`` functions add to CONDITION_OPERATORS.
_STANDARD_OPERATORS_19 = frozenset(
    {"any", "not any", "any!", "not any!", "in", "not in", "<", ">", "<=", ">="}
    | {"like", "not like", "ilike", "not ilike", "=like", "not =like", "=ilike", "not =ilike"}
)
_OPERATORS_19 = _STANDARD_OPERATORS_19 | {"<>", "=", "!=", "==", "=?", "parent_of", "child_of"}
_OPERATORS_20 = _STANDARD_OPERATORS_19 | {"=", "!=", "=?", "access", "parent_of", "child_of"}


# READ_GROUP_NUMBER_GRANULARITY (Odoo 18-20): what may follow a date field in a path.
_DATE_PARTS_18 = frozenset(
    {"year_number", "quarter_number", "month_number", "iso_week_number", "day_of_year"}
    | {"day_of_month", "day_of_week", "hour_number", "minute_number", "second_number"}
)
# odoo/orm/utils.py: SQL_OPERATORS (Odoo 19/20), the operators of a property path with "=?".
_SQL_OPERATORS_19 = frozenset(
    {"=", "!=", "in", "not in", "<", ">", "<=", ">=", "like", "ilike", "=like", "=ilike"}
    | {"not like", "not ilike", "not =like", "not =ilike"}
)
DOMAIN_RULES_18 = DomainRules(
    date_parts=_DATE_PARTS_18,
    lower_case_operators=False,
    renamed_operators=MappingProxyType({"<>": "!="}),
    # expression.py, _expression_parse: the operators of a property condition.
    properties_operators=frozenset(
        {"=", "!=", ">", ">=", "<", "<=", "in", "not in", "like", "ilike", "not like"}
        | {"not ilike"}
    ),
    properties_one_step=True,
    binary_sub_paths=frozenset(),
    searchable_compute_sql=False,
    binary_check_all=False,
    strict_in=True,
    record_values=True,
    internal_any=False,
    python_filter=True,
    unsearchable_raises=False,
    access_operator=False,
    date_part_operators=frozenset({"=?", "child_of", "parent_of"}),
)
# expression.py, is_leaf(internal=True): a search method's result may use "inselect".
DOMAIN_RULES_17 = replace(
    DOMAIN_RULES_18,
    date_parts=frozenset(),
    date_part_operators=frozenset(),
    internal_operators=frozenset({"inselect", "not inselect"}),
)
DOMAIN_RULES_19 = DomainRules(
    date_parts=_DATE_PARTS_18 | {"tz"},
    lower_case_operators=True,
    renamed_operators=MappingProxyType({"<>": "!=", "==": "="}),
    properties_operators=_SQL_OPERATORS_19 | {"=?"},
    properties_one_step=False,
    binary_sub_paths=frozenset(),
    searchable_compute_sql=False,
    binary_check_all=False,
    strict_in=False,
    record_values=False,
    internal_any=True,
    python_filter=False,
    unsearchable_raises=True,
    access_operator=False,
)
# READ_GROUP_TIME_GRANULARITY joins the date parts; Binary has sub-paths and checks only
# existence; compute_sql makes a field searchable; no operator is rewritten.
DOMAIN_RULES_20 = replace(
    DOMAIN_RULES_19,
    date_parts=DOMAIN_RULES_19.date_parts | {"hour", "day", "week", "month", "quarter", "year"},
    lower_case_operators=False,
    renamed_operators=MappingProxyType({}),
    binary_sub_paths=frozenset({"size", "filename"}),
    searchable_compute_sql=True,
    binary_check_all=True,
    access_operator=True,
)


def _env_records(environment: str) -> Mapping[str, str]:
    models = {"user": "res.users", "company": "res.company", "companies": "res.company"}
    return MappingProxyType({f"{environment}.{name}": model for name, model in models.items()})


# Odoo 18: the ORM lives in odoo/fields.py, odoo/models.py, odoo/api.py.
ODOO_18 = OdooProfile(
    version=18,
    min_python=(3, 10),
    namespace_odoo=False,
    base_model="odoo.models.BaseModel",
    new_id="odoo.api.NewId",
    env_getitem="odoo.api.Environment.__getitem__",
    env_records=_env_records("odoo.api.Environment"),
    relational_classes=frozenset(
        {"odoo.fields.Many2one", "odoo.fields.One2many", "odoo.fields.Many2many"}
    ),
    relational_base="odoo.fields._Relational",
    field_base="odoo.fields.Field",
    comodel_generic=frozenset(),  # Field[M] already
    field_modules=("odoo.fields.",),
    many2one="odoo.fields.Many2one",
    datetime="odoo.fields.Datetime",
    # Some are typed ``T -> T`` already; listing them keeps the signature anyway.
    identity_decorators=frozenset(
        {
            "odoo.api.private",
            "odoo.api.model",
            "odoo.api.readonly",
            "odoo.api.model_create_multi",
            "odoo.api.model_create_single",
            "odoo.api.autovacuum",
        }
    ),
    # ``returns("self")``, ``onchange("x")``... return a decorator that returns the
    # method (``api.depends`` / ``constrains`` are typed already).
    identity_factories=frozenset(
        {"odoo.api.returns", "odoo.api.onchange", "odoo.api.ondelete", "odoo.api.depends_context"}
    ),
    identity_template="odoo.api.model",
    field_decorators=_field_decorators("odoo.api"),
    domain_operators=_OPERATORS_18,
    domain_class=None,
    domain_rules=DOMAIN_RULES_18,
    sorted_order=False,
    request_var="odoo.http.request",
    request_class="odoo.http.Request",
)

# Odoo 17: the same modules as 18, but nothing is typed: ``Field`` is not generic, the
# decorators and ``Environment.__getitem__`` are unannotated, ``NewId`` is in models.py.
ODOO_17 = replace(
    ODOO_18,
    version=17,
    new_id="odoo.models.NewId",
    identity_decorators=frozenset(
        {"odoo.api.private", "odoo.api.model", "odoo.api.model_create_multi", "odoo.api.autovacuum"}
    ),
    identity_factories=frozenset(
        {"odoo.api.returns", "odoo.api.onchange", "odoo.api.ondelete", "odoo.api.depends_context"}
        | {"odoo.api.depends", "odoo.api.constrains"}
    ),
    identity_template=None,
    domain_rules=DOMAIN_RULES_17,
    untyped_fields=True,
)

# Odoo 19: the ORM lives in odoo/orm/*; odoo.fields / odoo.models / odoo.api re-export it.
# ``api.model``, ``private``... are typed ``C -> C`` (``model_create_multi`` also accepts
# one dict); the factories return ``Decorator``, an alias of ``Callable[[C], C]`` used
# without an argument, that is ``Callable[[Any], Any]``.
ODOO_19 = OdooProfile(
    version=19,
    min_python=(3, 10),
    namespace_odoo=True,
    base_model="odoo.orm.models.BaseModel",
    new_id="odoo.orm.identifiers.NewId",
    env_getitem="odoo.orm.environments.Environment.__getitem__",
    env_records=_env_records("odoo.orm.environments.Environment"),
    relational_classes=frozenset(
        {
            "odoo.orm.fields_relational.Many2one",
            "odoo.orm.fields_relational.One2many",
            "odoo.orm.fields_relational.Many2many",
        }
    ),
    relational_base="odoo.orm.fields_relational._Relational",
    field_base="odoo.orm.fields.Field",
    comodel_generic=frozenset(
        f"odoo.orm.fields_relational.{name}"
        for name in ("_Relational", "_RelationalMulti", "Many2one", "One2many", "Many2many")
    ),
    # odoo/orm/fields.py (Field) and odoo/orm/fields_*.py (Char, Many2one...).
    field_modules=("odoo.orm.fields.", "odoo.orm.fields_"),
    many2one="odoo.orm.fields_relational.Many2one",
    datetime="odoo.orm.fields_temporal.Datetime",
    identity_decorators=frozenset(),
    identity_factories=frozenset(
        f"odoo.orm.decorators.{name}"
        for name in ("constrains", "depends", "depends_context", "onchange", "ondelete")
    ),
    identity_template="odoo.orm.decorators.model",
    field_decorators=_field_decorators("odoo.orm.decorators"),
    domain_operators=_OPERATORS_19,
    domain_class="odoo.orm.domains.Domain",
    domain_rules=DOMAIN_RULES_19,
    sorted_order=True,
    request_var="odoo.http.request",
    request_class="odoo.http.Request",
)

# Odoo 20: the same names as 19. The sources use PEP 695 type parameters
# (``class Field[T]``, ``def model[C: Callable]``), which mypy parses from Python 3.12.
# ``<>`` and ``==`` are gone; ``access`` is new. odoo/http is a package that declares
# ``request: Request`` (and ``Request.env: Environment | None``).
ODOO_20 = replace(
    ODOO_19,
    version=20,
    min_python=(3, 12),
    domain_operators=_OPERATORS_20,
    domain_rules=DOMAIN_RULES_20,
    request_var=None,
    request_class="odoo.http.requestlib.Request",
)

_PROFILES: Mapping[int, OdooProfile] = MappingProxyType(
    {17: ODOO_17, 18: ODOO_18, 19: ODOO_19, 20: ODOO_20}
)


def profile_for(version: int) -> OdooProfile | None:
    """The profile of an Odoo major version; ``None`` if not implemented yet."""
    return _PROFILES.get(version)
