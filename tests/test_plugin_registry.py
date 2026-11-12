"""End-to-end tests of registry semantics that only show in mypy's output.

Load order, model names, ``_inherit`` problems and the implicit ``base`` parent,
checked with the real mypy and the installed Odoo.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from conftest import OdooNames, RunMypy

T_PARTNER = """
from odoo import fields, models

class Partner(models.Model):
    _name = "t.partner"

    base_field = fields.Integer()
"""


def _override(return_type: str, blank_lines: int = 0) -> str:
    return "\n" * blank_lines + (
        "from odoo import models\n\n"
        "class Partner(models.Model):\n"
        '    _inherit = "t.partner"\n\n'
        f"    def kind(self) -> {return_type}:\n"
        f"        return {return_type}()\n"
    )


TOP = """
from odoo import models

class Partner(models.Model):
    _inherit = "t.partner"

    def action(self) -> None:
        reveal_type(self.kind())
"""


def _revealed(result_lines: list[str]) -> list[str]:
    return [line.split("note: ", 1)[1] for line in result_lines if "Revealed type" in line]


# -- [4][17] execution order ----------------------------------------------------------------


def test_imports_in_try_if_and_imported_module_order_match_python(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "t_imp": {
                "__init__.py": (
                    "from . import base, models\n"
                    "try:\n    from . import optional\nexcept ImportError:\n    pass\n"
                    "if True:\n    from . import wizard\n"
                ),
                "base.py": T_PARTNER,
                "models/__init__.py": "from . import a\n",
                "models/a.py": """
                from odoo import fields, models
                from .b import HELPER

                class PartnerA(models.Model):
                    _inherit = "t.partner"
                    field_a = fields.Integer()

                    def action(self) -> None:
                        reveal_type(self.field_b)
                        self.env["t.optional"]
                        self.env["t.wiz"]
                """,
                "models/b.py": """
                from odoo import fields, models
                HELPER = 1

                class PartnerB(models.Model):
                    _inherit = "t.partner"
                    field_b = fields.Char()
                """,
                "optional.py": """
                from odoo import models
                try:
                    class Opt(models.Model):
                        _name = "t.optional"
                except ImportError:
                    pass
                """,
                "wizard.py": """
                from odoo import models

                class Wiz(models.TransientModel):
                    _name = "t.wiz"
                """,
            }
        }
    )

    assert result.messages("a.py") == ['10: note: Revealed type is "str | Literal[False]"']


# -- [5][29] model names ------------------------------------------------------------------------


def test_model_names_chained_constant_and_name_reference(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "t_names": {
                "models.py": """
                from odoo import fields, models

                CONST = "t.const"

                class Mixin(models.AbstractModel):
                    _name = "t.mixin"

                class Const(models.Model):
                    _name = CONST
                    _inherit = ["t.mixin"]
                    const_field = fields.Integer()

                class Chained(models.Model):
                    _name = _description = "t.chained"

                class Thing(models.Model):
                    _name = "t.thing"

                    def action(self) -> None:
                        reveal_type(self.env["t.mixin"])
                        reveal_type(self.env["t.const"].const_field)
                        reveal_type(self.env["t.chained"])
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '21: note: Revealed type is "t_names.models.Mixin"',
        '22: note: Revealed type is "int"',
        '23: note: Revealed type is "t_names.models.Chained"',
    ]


def test_inherit_equal_to_name_extends_model_for_dependents(run_mypy: RunMypy) -> None:
    # website/models/ir_module_module.py: `_name = "ir.module.module"; _inherit = _name`
    result = run_mypy(
        {
            "t_web": {
                "models.py": """
                from odoo import fields, models

                class ResPartner(models.Model):
                    _name = "res.partner"
                    _description = "Partner"
                    _inherit = _name

                    web_field = fields.Integer()
                """
            },
            "t_user": {
                "depends": ["t_web"],
                "models.py": """
                from odoo import models

                class Thing(models.Model):
                    _name = "t.thing"

                    def action(self) -> None:
                        reveal_type(self.env["res.partner"].name)
                        reveal_type(self.env["res.partner"].web_field)
                """,
            },
        }
    )

    assert result.messages("t_web/models.py") == []
    assert result.messages("t_user/models.py") == [
        '8: note: Revealed type is "str | Literal[False]"',
        '9: note: Revealed type is "int"',
    ]


# -- [6][35][38] load order decides which unrelated override wins ------------------------------


def test_override_of_deeper_addon_wins_over_shallower_one(run_mypy: RunMypy) -> None:
    # Odoo 18 loads by depth, then name: z_shallow (depth 2) before b_deep (depth 3).
    result = run_mypy(
        {
            "y_root": {"models.py": T_PARTNER},
            "b_mid": {"depends": ["y_root"], "models.py": ""},
            "b_deep": {"depends": ["b_mid"], "models.py": _override("int")},
            "z_shallow": {"depends": ["y_root"], "models.py": _override("str")},
            "y_top": {"depends": ["b_deep", "z_shallow"], "models.py": TOP},
        },
        check=["y_top"],
    )

    assert _revealed(result.messages("y_top/models.py")) == ['Revealed type is "int"']


def test_override_of_later_addon_at_same_depth_wins(run_mypy: RunMypy) -> None:
    # left's class is on a later line: only the addon order may pick right's override.
    result = run_mypy(
        {
            "y_root": {"models.py": T_PARTNER},
            "left": {"depends": ["y_root"], "models.py": _override("int", blank_lines=5)},
            "right": {"depends": ["y_root"], "models.py": _override("str")},
            "top": {"depends": ["left", "right"], "models.py": TOP},
        },
        check=["top"],
    )

    assert _revealed(result.messages("top/models.py")) == ['Revealed type is "str"']


# -- [8] addon layouts Odoo accepts ----------------------------------------------------------


def test_symlinked_addon_with_other_name_and_openerp_manifest_see_dependency(
    run_mypy: RunMypy, tmp_path: Path
) -> None:
    user = """
    from odoo import models

    class Partner(models.Model):
        _inherit = "t.partner"

        def action(self) -> None:
            reveal_type(self.base_field)
    """
    source = tmp_path / "src" / "t_link-18.0"
    source.mkdir(parents=True)
    (source / "__manifest__.py").write_text("{'depends': ['t_base']}")
    (source / "__init__.py").write_text("from . import models\n")
    (source / "models.py").write_text(textwrap.dedent(user))
    (tmp_path / "addons").mkdir()
    (tmp_path / "addons" / "t_link").symlink_to(source)

    result = run_mypy(
        {
            "t_base": {"models.py": T_PARTNER},
            "t_old": {"manifest": "__openerp__.py", "depends": ["t_base"], "models.py": user},
        },
        check=["t_link", "t_old"],
    )

    assert result.messages("t_link/models.py") == ['8: note: Revealed type is "int"']
    assert result.messages("t_old/models.py") == ['8: note: Revealed type is "int"']


# -- [42][44] missing dependency, _register = False -------------------------------------------


def test_dependency_missing_from_disk_keeps_resolution(run_mypy: RunMypy, names: OdooNames) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "depends": ["base", "not_on_disk"],
                "models.py": """
                from odoo import models

                class Unregistered(models.Model):
                    _register = False
                    _name = "my.unregistered"

                class Thing(models.Model):
                    _name = "my.thing"

                    def action(self) -> None:
                        reveal_type(self.env["res.partner"])
                        self.env["my.unregistered"]
                """,
            }
        }
    )

    assert result.messages("models.py") == [
        f'12: note: Revealed type is "{names.partner}"',
        '13: error: Unknown Odoo model "my.unregistered"  [odoo-model]',
    ]


# -- [2] problems with _inherit ---------------------------------------------------------------


def test_inherit_of_unknown_or_unreachable_model_reports_error(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "t_base": {"models.py": T_PARTNER},
            "t_nodep": {
                "depends": ["base", "ee_missing"],
                "models.py": """
                from odoo import models

                class FromOtherAddon(models.Model):
                    _inherit = "t.partner"

                class Typo(models.Model):
                    _inherit = "t.partnr"

                class NewWithTypo(models.Model):
                    _name = "t.new"
                    _inherit = ["t.mixinn", "mail.thread"]

                class Early(models.Model):
                    _name = "t.early"
                    _inherit = "t.late"

                class Late(models.AbstractModel):
                    _name = "t.late"

                    def action(self) -> None:
                        self.env["t.partnr"]
                """,
            },
        },
        check=["t_nodep"],
    )

    missing = "; dependencies not found on addons_path: ee_missing"
    assert result.messages("t_nodep/models.py") == [
        '5: error: Odoo model "t.partner" in _inherit is defined in addon "t_base", '
        'which is not in the dependencies of "t_nodep"  [odoo-model-depends]',
        f'8: error: Unknown Odoo model "t.partnr" in _inherit{missing}  [odoo-model]',
        f'12: error: Unknown Odoo model "t.mixinn" in _inherit{missing}  [odoo-model]',
        '12: error: Odoo model "mail.thread" in _inherit is defined in addon "mail", '
        'which is not in the dependencies of "t_nodep"  [odoo-model-depends]',
        '16: error: Odoo model "t.late" in _inherit is defined after this class; '
        "Odoo registers the parent first  [odoo-model]",
        '22: error: Unknown Odoo model "t.partnr"  [odoo-model]',
    ]


def test_inherit_cycle_reports_one_error(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "cyc": {
                "models.py": """
                from odoo import models

                class A(models.Model):
                    _name = "my.a"
                    _inherit = ["my.b"]

                class B(models.Model):
                    _name = "my.b"
                    _inherit = ["my.a"]
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '4: error: _inherit cycle: classes "A", "B" inherit from each other  [odoo-model]',
        '6: error: Odoo model "my.b" in _inherit is defined after this class; '
        "Odoo registers the parent first  [odoo-model]",
    ]


# -- [7].1 every model implicitly inherits 'base' ---------------------------------------------


def test_methods_added_to_base_are_visible_on_every_model(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "t_helper": {
                "models.py": """
                from odoo import models

                class Base(models.AbstractModel):
                    _inherit = "base"

                    def _t_helper(self) -> int:
                        return 1
                """
            },
            "t_user": {
                "depends": ["t_helper"],
                "models.py": """
                from odoo import models

                class Thing(models.Model):
                    _name = "t.thing"

                    def action(self) -> None:
                        reveal_type(self._t_helper())

                class Partner(models.Model):
                    _inherit = "res.partner"

                    def action(self) -> None:
                        reveal_type(self._t_helper())

                class Other(models.Model):
                    _name = "t.other"

                    def action(self) -> None:
                        reveal_type(self.env["res.users"]._t_helper())
                """,
            },
        },
        check=["t_user"],
    )

    # res.users is not extended in t_user: env[...] merges t_helper's base extension in.
    assert result.messages("t_user/models.py") == [
        '8: note: Revealed type is "int"',
        '14: note: Revealed type is "int"',
        '20: note: Revealed type is "int"',
    ]


def _base_extension(*names: str) -> str:
    classes = "".join(
        f'class {name}(models.AbstractModel):\n    _inherit = "base"\n\n' for name in names
    )
    return "from odoo import models\n\n" + classes


def test_unrelated_base_extensions_keep_one_mro_order(run_mypy: RunMypy) -> None:
    # p's class reaches m's extension before we's (through x.thread), q's base
    # extension reaches we's first; with only the tips of 'base' as bases, r's class
    # could not get an MRO and lost every _inherit base.
    result = run_mypy(
        {
            "w": {"models.py": _base_extension("W")},
            "m": {
                "depends": ["w"],
                "models.py": _base_extension("MExt")
                + 'class Thread(models.AbstractModel):\n    _name = "x.thread"\n',
            },
            "we": {"depends": ["w"], "models.py": _base_extension("We1", "We2")},
            "p": {
                "depends": ["m", "we"],
                "models.py": """
                from odoo import fields, models

                class P(models.Model):
                    _name = "x.p"
                    _inherit = ["x.thread"]
                    p_field = fields.Integer()
                """,
            },
            "q": {"depends": ["m", "we"], "models.py": _base_extension("Q")},
            "r": {
                "depends": ["p", "q"],
                "models.py": """
                from odoo import models

                class R(models.Model):
                    _inherit = "x.p"

                    def action(self) -> None:
                        reveal_type(self.p_field)
                """,
            },
        },
        check=["r"],
    )

    assert result.messages("r/models.py") == ['8: note: Revealed type is "int"']


def test_python_base_listed_in_inherit_keeps_all_inherit_bases(run_mypy: RunMypy) -> None:
    # website.published.multi.mixin: class X(PublishedMixin) with _inherit = [published, multi]
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import fields, models

                class Published(models.AbstractModel):
                    _name = "x.published"
                    published = fields.Boolean()

                class Multi(models.AbstractModel):
                    _name = "x.multi"
                    multi = fields.Integer()

                class PublishedMulti(Published):
                    _name = "x.published.multi"
                    _inherit = ["x.published", "x.multi"]

                    def action(self) -> None:
                        reveal_type(self.multi)
                        reveal_type(self.published)
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '17: note: Revealed type is "int"',
        '18: note: Revealed type is "bool"',
    ]


# -- guards of optional dependencies (roadmap 4.6) --------------------------------------------


def test_guarded_models_outside_depends_are_any(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "t_base": {"models.py": T_PARTNER},
            "t_nodep": {
                "depends": ["base"],
                "models.py": """
                from odoo import models

                class Thing(models.Model):
                    _name = "t.thing"

                    def action(self) -> None:
                        if "t.partner" in self.env:
                            reveal_type(self.env["t.partner"])
                        if "ee.only" not in self.env:
                            return
                        self.env["ee.only"].search([])
                        self.env["t.partner"]
                """,
            },
        },
        check=["t_nodep"],
    )

    assert result.messages("t_nodep/models.py") == [
        '9: note: Revealed type is "Any"',
        '13: error: Odoo model "t.partner" is defined in addon "t_base", which is not in '
        'the dependencies of "t_nodep"  [odoo-model-depends]',
    ]


def test_flag_and_helper_guards_of_models_outside_depends(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "t_base": {"models.py": T_PARTNER},
            "t_nodep": {
                "depends": ["base"],
                "models.py": """
                from odoo import models

                class Thing(models.Model):
                    _name = "t.thing"

                    def _has_partner(self) -> bool:
                        return "t.partner" in self.env

                    def action(self) -> None:
                        installed = "t.partner" in self.env
                        if installed:
                            self.env["t.partner"].search([])
                        if self._has_partner():
                            self.env["t.partner"].search([])
                        self.env["t.partner"]
                """,
            },
        },
        check=["t_nodep"],
    )

    assert result.messages("t_nodep/models.py") == [
        '16: error: Odoo model "t.partner" is defined in addon "t_base", which is not in '
        'the dependencies of "t_nodep"  [odoo-model-depends]',
    ]
