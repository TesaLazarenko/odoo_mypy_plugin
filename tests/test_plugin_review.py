"""End-to-end regression tests for the review findings on the registry MRO work.

Each test pins one behaviour: merged view classes shared between modules, unreachable
chain classes, written generic bases, the owner-view fallback of relational reads and
the ``loaded_with`` guard of ``env``.
"""

from __future__ import annotations

from conftest import RunMypy

X_M = """
from odoo import fields, models

class M(models.Model):
    _name = "x.m"

    a = fields.Char()
"""


def _extension(field: str) -> str:
    return f"""
from odoo import fields, models

class M(models.Model):
    _inherit = "x.m"

    {field} = fields.Char()
"""


# -- one merged class per model and addon, whichever module needs it ----------------------

MERGED_D1 = """
from odoo import api

env: api.Environment = None  # type: ignore[assignment]
RECS = env["x.m"]
"""

MERGED_D2 = """
from .d1 import RECS, env

other = env["x.m"]
other = RECS
reveal_type(other.b)
"""


def test_merged_class_of_imported_module_is_reused(run_mypy: RunMypy) -> None:
    addons = {
        "xa": {"m.py": X_M},
        "xb": {"depends": ["xa"], "m.py": _extension("b")},
        "xc": {"depends": ["xa"], "m.py": _extension("c")},
        "xd": {
            "depends": ["xb", "xc"],
            "__init__.py": "from . import d1, d2\n",
            "d1.py": MERGED_D1,
            "d2.py": MERGED_D2,
        },
    }

    result = run_mypy(addons, check=["xd"])

    assert (result.messages("xd/d1.py"), result.messages("xd/d2.py")) == (
        [],
        ['6: note: Revealed type is "str | Literal[False]"'],
    )


def test_merged_class_of_cached_imported_module_is_reused(run_mypy: RunMypy) -> None:
    xd = {"depends": ["xb", "xc"], "__init__.py": "from . import d1, d2\n", "d1.py": MERGED_D1}
    addons = {
        "xa": {"m.py": X_M},
        "xb": {"depends": ["xa"], "m.py": _extension("b")},
        "xc": {"depends": ["xa"], "m.py": _extension("c")},
        "xd": {**xd, "d2.py": "x = 1\n"},
    }
    run_mypy(addons, check=["xd"], fresh_cache=True)

    result = run_mypy({"xd": {**xd, "d2.py": MERGED_D2}}, check=["xd"], fresh_cache=True)

    assert result.messages("xd/d2.py") == ['6: note: Revealed type is "str | Literal[False]"']


# -- a chain class mypy never analyzes does not switch attribute checks off ---------------

UNREACHABLE = """
import sys
from odoo import fields, models

class M(models.Model):
    _name = "u.m"

    a = fields.Char()

if sys.version_info < (3, 0):
    class M2(models.Model):
        _inherit = "u.m"

        old = fields.Char()
"""

UNREACHABLE_USER = """
from odoo import models

class M(models.Model):
    _inherit = "u.m"

    def f(self) -> None:
        self.no_such_attr
        self.env["u.m"].no_such_attr
"""


def test_unreachable_chain_class_keeps_attribute_errors(run_mypy: RunMypy) -> None:
    addons = {"ua": {"m.py": UNREACHABLE}, "ub": {"depends": ["ua"], "m.py": UNREACHABLE_USER}}

    result = run_mypy(addons)

    assert result.messages("ub/m.py") == [
        '8: error: "M" has no attribute "no_such_attr"  [attr-defined]',
        '9: error: "M" has no attribute "no_such_attr"  [attr-defined]',
    ]


# -- written Python bases keep their type arguments ---------------------------------------

GENERIC_MIXIN = """
from typing import Generic, TypeVar
from odoo import models

T = TypeVar("T")

class Mix(models.AbstractModel, Generic[T]):
    _name = "y.mix"

    def get(self) -> T:
        raise NotImplementedError

class N(Mix[int]):
    _name = "y.n"
    _inherit = "y.mix"

    def h(self) -> None:
        reveal_type(self.get())
"""

PYTHON_BOX = """
from typing import Generic, TypeVar
from odoo import models

T = TypeVar("T")

class Box(Generic[T]):
    def get(self) -> T:
        raise NotImplementedError

class Thing(models.Model, Box[int]):
    _name = "my.thing"

    def action(self) -> None:
        reveal_type(self.get())
        box: Box[str] = self
"""


def test_registry_base_written_with_type_arguments_keeps_them(run_mypy: RunMypy) -> None:
    result = run_mypy({"my_mod": {"models.py": GENERIC_MIXIN}})

    assert result.messages("models.py") == ['18: note: Revealed type is "int"']


def test_python_generic_base_of_model_keeps_type_arguments(run_mypy: RunMypy) -> None:
    result = run_mypy({"my_mod": {"models.py": PYTHON_BOX}})

    assert result.messages("models.py") == [
        '15: note: Revealed type is "int"',
        '16: error: Incompatible types in assignment (expression has type "Thing", '
        'variable has type "Box[str]")  [assignment]',
    ]


# -- relational reads fall back to the view of the record's own class ---------------------

DEP_THING = """
from odoo import fields, models

class ResPartner(models.Model):
    _inherit = "res.partner"

    x_level = fields.Integer()

class Thing(models.Model):
    _name = "dep.thing"

    partner_id = fields.Many2one("res.partner")
"""

READER = """
from dep_mod.models import Thing

def read(thing: Thing) -> None:
    reveal_type(thing.partner_id.x_level)
"""

MERGED_HELPERS = """
from odoo import api

env: api.Environment = None  # type: ignore[assignment]
RECS = env["x.m"]
"""

MERGED_READER = """
from dep_mod.helpers import RECS

reveal_type(RECS.partner_id.x_level)
"""


def test_relational_read_in_module_without_edges_uses_owner_view(run_mypy: RunMypy) -> None:
    addons = {
        "dep_mod": {"models.py": DEP_THING},
        "my_mod": {"depends": ["dep_mod"], "reader.py": READER},
    }

    result = run_mypy(addons, check=["my_mod"])

    assert result.messages("reader.py") == ['5: note: Revealed type is "int"']


def test_relational_read_on_merged_records_of_other_addon_uses_their_view(
    run_mypy: RunMypy,
) -> None:
    partner = DEP_THING.split("class Thing")[0]
    owner = _extension("b").replace(
        "b = fields.Char()", 'partner_id = fields.Many2one("res.partner")'
    )
    addons = {
        "xa": {"m.py": X_M},
        "xb": {"depends": ["xa"], "m.py": owner},
        "xc": {"depends": ["xa"], "m.py": _extension("c")},
        "dep_mod": {"depends": ["xb", "xc"], "models.py": partner, "helpers.py": MERGED_HELPERS},
        "my_mod": {"depends": ["dep_mod"], "reader.py": MERGED_READER},
    }

    result = run_mypy(addons, check=["my_mod"])

    assert result.messages("reader.py") == ['4: note: Revealed type is "int"']


# -- env[...] through an alias of env is Any when its classes are not loaded ---------------

ENV_ALIAS = """
from odoo import models

class Thing(models.Model):
    _name = "my.thing"

    def action(self) -> None:
        env2 = self.env
        reveal_type(env2["res.partner"])
"""


def test_env_alias_of_model_without_edges_is_any(run_mypy: RunMypy) -> None:
    addons = {
        "dep_a": {"models.py": DEP_THING.split("class Thing")[0]},
        "dep_b": {"models.py": DEP_THING.split("class Thing")[0].replace("x_level", "y")},
        "my_mod": {"depends": ["dep_a", "dep_b"], "models.py": ENV_ALIAS},
    }

    result = run_mypy(addons, check=["my_mod"])

    assert result.messages("models.py") == ['9: note: Revealed type is "Any"']
