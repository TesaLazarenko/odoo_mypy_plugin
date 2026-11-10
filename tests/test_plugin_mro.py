"""End-to-end tests of model classes as Odoo's registry builds them (work package 3).

Class MROs follow ``BaseModel._build_model`` whatever order mypy analyzes the classes in,
later extensions of ``_inherit`` parents are visible from the addon that sees them, and
several unrelated extensions of one model are merged.
"""

from __future__ import annotations

from conftest import RunMypy

PARTNER = "odoo.addons.base.models.res_partner.Partner"


def _revealed(lines: list[str]) -> list[str]:
    return [line.split("Revealed type is ", 1)[1] for line in lines if "Revealed type" in line]


# -- [24] the hook defers until every class of the chain exists -------------------------------

SCC_CORE = """
from odoo import fields, models

class Move(models.Model):
    _name = "t.move"

    core_field = fields.Char()
    pay_id = fields.Many2one("t.pay")

    def core_method(self) -> int:
        return self.env["t.move"].tip_method()
"""

SCC_PAY = """
from odoo import fields, models

class Pay(models.Model):
    _name = "t.pay"

    move_id = fields.Many2one("t.move")

class Move(models.Model):
    _inherit = "t.move"

    pay_field = fields.Integer()
"""

SCC_TIP = """
from odoo import fields, models

class Move(models.Model):
    _inherit = "t.move"

    def tip_method(self) -> int:
        reveal_type(self.core_field)
        reveal_type(self.pay_field)
        reveal_type(self.env["t.move"].core_field)
        reveal_type(self.env["t.pay"].move_id.core_field)
        return self.core_method()
"""


def test_mro_classes_analyzed_out_of_order_get_the_full_chain(run_mypy: RunMypy) -> None:
    # account: account_payment.AccountMove was analyzed before account_move.AccountMove,
    # and the tip copied its incomplete MRO. Here the cycle core -> tip (env[...]) makes
    # mypy analyze z_pay and m_tip before a_core.
    result = run_mypy(
        {
            "t_scc": {
                "__init__.py": "from . import models\n",
                "models/__init__.py": "from . import a_core, z_pay, m_tip\n",
                "models/a_core.py": SCC_CORE,
                "models/z_pay.py": SCC_PAY,
                "models/m_tip.py": SCC_TIP,
            }
        }
    )

    assert result.messages("m_tip.py") == [
        '8: note: Revealed type is "str | Literal[False]"',
        '9: note: Revealed type is "int"',
        '10: note: Revealed type is "str | Literal[False]"',
        '11: note: Revealed type is "str | Literal[False]"',
    ]
    assert result.messages("a_core.py") == []


# -- [25][50] unrelated extensions never make the MRO inconsistent -----------------------------

DIAMOND_CORE = """
from odoo import fields, models

class Company(models.Model):
    _name = "t.company"

    name = fields.Char()

    def _default_vals(self) -> dict[str, int]:
        return {}

class Res(models.AbstractModel):
    _name = "t.res"

    res_field = fields.Integer()

class Mail(models.AbstractModel):
    _name = "t.mail"

    mail_field = fields.Integer()
"""


def _company_extension(inherit: str, field: str) -> str:
    return f"""
from odoo import fields, models

class Company(models.Model):
    _name = "t.company"
    _inherit = {inherit}

    {field} = fields.Integer()
"""


DIAMOND_TOP = """
from odoo import models

class Company(models.Model):
    _inherit = "t.company"

    def _default_vals(self) -> dict[str, int]:
        return super()._default_vals()

    def action(self) -> None:
        reveal_type(self.name)
        reveal_type(self.web_field)
        reveal_type(self.sale_field)
        reveal_type(self.res_field + self.mail_field)
"""

DIAMOND_USER = """
from odoo import models

class Thing(models.Model):
    _name = "t.thing"

    def action(self) -> None:
        reveal_type(self.env["t.company"].name)
        reveal_type(self.env["t.company"].web_field)
"""


def test_mro_unrelated_extensions_ordering_mixins_differently_keep_all_bases(
    run_mypy: RunMypy,
) -> None:
    # website_sale's res.company: website lists resource before mail, sale (through
    # account) mail before resource; C3 failed and the class lost every _inherit base.
    result = run_mypy(
        {
            "t_core": {"models.py": DIAMOND_CORE},
            "t_web": {
                "depends": ["t_core"],
                "models.py": _company_extension('["t.company", "t.res", "t.mail"]', "web_field"),
            },
            "t_sale": {
                "depends": ["t_core"],
                "models.py": _company_extension('["t.company", "t.mail", "t.res"]', "sale_field"),
            },
            "t_ws": {"depends": ["t_web", "t_sale"], "models.py": DIAMOND_TOP},
            "t_user": {"depends": ["t_ws"], "models.py": DIAMOND_USER},
        },
        check=["t_ws", "t_user"],
    )

    assert result.messages("t_ws/models.py") == [
        '11: note: Revealed type is "str | Literal[False]"',
        '12: note: Revealed type is "int"',
        '13: note: Revealed type is "int"',
        '14: note: Revealed type is "int"',
    ]
    assert result.messages("t_user/models.py") == [
        '8: note: Revealed type is "str | Literal[False]"',
        '9: note: Revealed type is "int"',
    ]


def test_mro_python_bases_contradicting_inherit_order_keep_bases_with_note(
    run_mypy: RunMypy,
) -> None:
    # _inherit puts my.m1 before my.m2, Python puts M2 before M1: Odoo cannot build this
    # class either (C3 fails). The plugin keeps every base and says why the order is off.
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import fields, models

                class M1(models.AbstractModel):
                    _name = "my.m1"

                    m1_value = fields.Integer()

                class M2(M1):
                    _name = "my.m2"
                    _inherit = ["my.m1"]

                    m2_value = fields.Integer()

                class C(M2):
                    _name = "my.c"
                    _inherit = ["my.m1", "my.m2"]

                    def action(self) -> None:
                        reveal_type(self.m1_value + self.m2_value)
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '15: note: Odoo model class "C" has no consistent method resolution order '
        "(Odoo cannot load it either); its bases are kept, some out of order",
        '20: note: Revealed type is "int"',
    ]


def test_mro_python_base_of_other_model_keeps_python_order(run_mypy: RunMypy) -> None:
    # class C(M2) with M2(M1): M2 must stay before M1 in C's MRO ([43] P1).
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import fields, models

                class M1(models.AbstractModel):
                    _name = "my.m1"

                    m1_value = fields.Integer()

                    def kind(self) -> int:
                        return 1

                class M2(M1):
                    _name = "my.m2"

                    def kind(self) -> int:
                        return 2

                class C(M2):
                    _name = "my.c"
                    _inherit = ["my.m1"]

                    def action(self) -> None:
                        reveal_type(self.m1_value)
                        reveal_type(super().kind())
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '23: note: Revealed type is "int"',
        '24: note: Revealed type is "int"',
    ]


def test_mro_python_base_of_same_model_plus_mixin_keeps_mixin(run_mypy: RunMypy) -> None:
    # website.published.multi.mixin shape: class B(A) with _inherit = [A's model, mixin]
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import fields, models

                class A(models.Model):
                    _name = "my.a"

                    a_value = fields.Integer()

                class B(A):
                    _inherit = ["my.a", "avatar.mixin"]

                    def action(self) -> None:
                        reveal_type(self.a_value)
                        reveal_type(self._compute_avatar_128)
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '13: note: Revealed type is "int"',
        '14: note: Revealed type is "def () -> Any"',
    ]


# -- [1] later extensions of _inherit parents ----------------------------------------------

THREAD = """
from odoo import models

class Thread(models.AbstractModel):
    _name = "t.thread"

    def message_post(self) -> int:
        return 1
"""

TASK = """
from odoo import models

class Task(models.Model):
    _name = "t.task"
    _inherit = ["t.thread"]
"""

SMS = """
from odoo import models

class Thread(models.AbstractModel):
    _inherit = "t.thread"

    def _message_sms(self) -> str:
        return ""
"""


def test_mixin_extended_by_later_addon_is_visible_on_inheriting_models(
    run_mypy: RunMypy,
) -> None:
    # project_sms: sms adds _message_sms to mail.thread, which project.task inherits
    # in project; project_sms (depends on both) uses it on self and through env[...].
    result = run_mypy(
        {
            "t_mail": {"models.py": THREAD},
            "t_project": {"depends": ["t_mail"], "models.py": TASK},
            "t_sms": {"depends": ["t_mail"], "models.py": SMS},
            "t_project_sms": {
                "depends": ["t_project", "t_sms"],
                "models.py": """
                from odoo import models

                class Task(models.Model):
                    _inherit = "t.task"

                    def action(self) -> None:
                        reveal_type(self._message_sms())
                        reveal_type(self.message_post())
                """,
            },
            "t_user": {
                "depends": ["t_project", "t_sms"],
                "models.py": """
                from odoo import models

                class Thing(models.Model):
                    _name = "t.thing"

                    def action(self) -> None:
                        task = self.env["t.task"]
                        reveal_type(task._message_sms())
                        reveal_type(task.message_post())
                """,
            },
        },
        check=["t_project_sms", "t_user"],
    )

    assert result.messages("t_project_sms/models.py") == [
        '8: note: Revealed type is "str"',
        '9: note: Revealed type is "int"',
    ]
    assert result.messages("t_user/models.py") == [
        '9: note: Revealed type is "str"',
        '10: note: Revealed type is "int"',
    ]


def test_mixin_extension_not_in_dependencies_stays_invisible(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "t_mail": {"models.py": THREAD},
            "t_project": {"depends": ["t_mail"], "models.py": TASK},
            "t_sms": {"depends": ["t_mail"], "models.py": SMS},
            "t_user": {
                "depends": ["t_project"],
                "models.py": """
                from odoo import models

                class Thing(models.Model):
                    _name = "t.thing"

                    def action(self) -> None:
                        self.env["t.task"]._message_sms()
                """,
            },
        },
        check=["t_user", "t_sms"],
    )

    assert result.messages("t_user/models.py") == [
        '8: error: "Task" has no attribute "_message_sms"  [attr-defined]'
    ]


# -- [7] part 3: several tips are merged ------------------------------------------------------


def test_env_model_extended_by_unrelated_addons_merges_their_classes(run_mypy: RunMypy) -> None:
    def extension(field: str) -> str:
        return f"""
        from odoo import fields, models

        class ResPartner(models.Model):
            _inherit = "res.partner"

            {field} = fields.Integer()
        """

    result = run_mypy(
        {
            "t_x": {"models.py": extension("field_x")},
            "t_y": {"models.py": extension("field_y")},
            "t_z": {"models.py": extension("field_z")},
            "my_mod": {
                "depends": ["t_x", "t_y", "t_z"],
                "models.py": """
                from odoo import models

                class Thing(models.Model):
                    _name = "my.thing"

                    def action(self) -> None:
                        partner = self.env["res.partner"]
                        reveal_type(partner)
                        reveal_type(partner.field_x + partner.field_y + partner.field_z)
                        reveal_type(partner.name)
                        partner.field_w
                """,
            },
        },
        check=["my_mod"],
    )

    merged = 'my_mod.models.<subclass of "res.partner" seen from "my_mod">'
    assert result.messages("my_mod/models.py") == [
        f'9: note: Revealed type is "{merged}"',
        '10: note: Revealed type is "int"',
        '11: note: Revealed type is "str | Literal[False]"',
        '12: error: "res.partner" has no attribute "field_w"  [attr-defined]',
    ]


def test_env_single_complete_tip_is_the_real_class(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import models

                class Thing(models.Model):
                    _name = "my.thing"

                    def action(self) -> None:
                        reveal_type(self.env["res.partner"])
                """,
            }
        }
    )

    assert result.messages("models.py") == [f'8: note: Revealed type is "{PARTNER}"']


def test_env_merged_class_survives_warm_cache(run_mypy: RunMypy) -> None:
    # The merged class lives in the symbol table of the module that needed it, so a
    # type that refers to it (here the attribute Thing.task) is reloaded from the cache.
    addons: dict[str, dict[str, object]] = {
        "t_mail": {"models.py": THREAD},
        "t_project": {"depends": ["t_mail"], "models.py": TASK},
        "t_sms": {"depends": ["t_mail"], "models.py": SMS},
        "t_user": {
            "depends": ["t_project", "t_sms"],
            "models.py": """
            from odoo import models

            class Thing(models.Model):
                _name = "t.thing"

                def action(self) -> None:
                    self.task = self.env["t.task"]
            """,
        },
        "t_reader": {
            "depends": ["t_user"],
            "models.py": """
            from odoo import models

            class Thing(models.Model):
                _inherit = "t.thing"

                def other(self) -> None:
                    reveal_type(self.task)
                    reveal_type(self.task._message_sms())
            """,
        },
    }

    cold = run_mypy(addons, check=["t_user"])
    warm = run_mypy(addons, check=["t_reader"])

    merged = 't_user.models.<subclass of "t.task" seen from "t_user">'
    assert cold.lines[-1].startswith("Success")
    assert warm.messages("t_reader/models.py") == [
        f'8: note: Revealed type is "{merged}"',
        '9: note: Revealed type is "str"',
    ]


def test_core_website_sale_company_and_order_keep_every_extension(run_mypy: RunMypy) -> None:
    # The real diamond: website_sale's res.company class had MroError and lost all bases.
    result = run_mypy(
        {
            "audit_ws": {
                "depends": ["website_sale"],
                "models.py": """
                from odoo import models

                class SaleOrderLine(models.Model):
                    _inherit = "sale.order.line"

                    def probe(self) -> None:
                        company = self.env["res.company"]
                        reveal_type(company.name)
                        reveal_type(company.website_id)
                        reveal_type(self.order_id.website_id)
                        reveal_type(self.order_id.company_id.website_id)
                """,
            }
        }
    )

    website = "odoo.addons.website_sale.models.website.Website"
    assert result.messages("models.py") == [
        '9: note: Revealed type is "str | Literal[False]"',
        f'10: note: Revealed type is "{website}"',
        f'11: note: Revealed type is "{website}"',
        f'12: note: Revealed type is "{website}"',
    ]
