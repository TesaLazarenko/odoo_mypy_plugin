"""Per-version profiles: the names of the Odoo classes and functions the hooks target.

mypy calls a plugin hook with the full name of the class or function where it is
*defined*, after following re-exports. Odoo 19 moved the ORM into ``odoo.orm.*``, and
``odoo.fields`` / ``odoo.models`` / ``odoo.api`` only re-export it, so hooks keyed on
``odoo.fields.Many2one`` stop firing there. Every such name lives in a profile; the
``odoo_version`` setting selects it.

Supported versions are 17 to 20; a version without a profile is reported as not
supported yet (see ROADMAP.md, "Версії Odoo").
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

__all__ = ["SUPPORTED_VERSIONS", "OdooProfile", "profile_for"]

SUPPORTED_VERSIONS = (17, 18, 19, 20)


@dataclass(frozen=True, slots=True)
class OdooProfile:
    """Names that differ between Odoo versions.

    Attributes:
        version: Odoo major version.
        base_model: Full name of ``BaseModel``.
        env_getitem: Full name of ``Environment.__getitem__``.
        env_records: Full names of ``Environment`` attributes that are records
            (``env.user``), each with its model.
        relational_classes: Full names of ``Many2one``, ``One2many``, ``Many2many``.
        relational_base: Full name of their common base class.
        field_modules: Prefixes of the full names of field classes (``fields.Char``).
        many2one: Full name of ``fields.Many2one``.
        datetime: Full name of ``fields.Datetime``.
        identity_decorators: Decorators that return the decorated method.
        identity_factories: Decorator factories whose decorator returns the method.
        identity_template: A typed ``def [T] (T) -> T`` to give those decorators.
    """

    version: int
    base_model: str
    env_getitem: str
    env_records: Mapping[str, str]
    relational_classes: frozenset[str]
    relational_base: str
    field_modules: tuple[str, ...]
    many2one: str
    datetime: str
    identity_decorators: frozenset[str]
    identity_factories: frozenset[str]
    identity_template: str

    def is_field_class(self, fullname: str) -> bool:
        """Whether a full name is that of a field class (``odoo.fields.Char``...)."""
        return fullname.startswith(self.field_modules)


def _env_records(environment: str) -> Mapping[str, str]:
    models = {"user": "res.users", "company": "res.company", "companies": "res.company"}
    return MappingProxyType({f"{environment}.{name}": model for name, model in models.items()})


# Odoo 18: the ORM lives in odoo/fields.py, odoo/models.py, odoo/api.py.
ODOO_18 = OdooProfile(
    version=18,
    base_model="odoo.models.BaseModel",
    env_getitem="odoo.api.Environment.__getitem__",
    env_records=_env_records("odoo.api.Environment"),
    relational_classes=frozenset(
        {"odoo.fields.Many2one", "odoo.fields.One2many", "odoo.fields.Many2many"}
    ),
    relational_base="odoo.fields._Relational",
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
)

_PROFILES: Mapping[int, OdooProfile] = MappingProxyType({18: ODOO_18})


def profile_for(version: int) -> OdooProfile | None:
    """The profile of an Odoo major version; ``None`` if not implemented yet."""
    return _PROFILES.get(version)
