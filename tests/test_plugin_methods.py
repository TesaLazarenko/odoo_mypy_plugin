"""End-to-end tests of recordset methods and environment attributes.

Odoo 18 declares ``sudo``, ``search``, ``filtered``... as ``-> Self``, but wraps them
in untyped decorators (``api.private``, ``api.returns``), and addons override them
without annotations: the plugin keeps the receiver's model type through both.
"""

from __future__ import annotations

from conftest import RunMypy

THING = """
from odoo import fields, models

class Thing(models.Model):
    _name = "t.thing"

    size = fields.Integer()
"""

# -- [12][27] decorated BaseModel methods ------------------------------------------------------


def test_decorated_self_methods_keep_receiver_type(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from typing import Final

                from odoo import api, fields, models

                class Thing(models.Model):
                    _name = "t.thing"

                    size = fields.Integer()
                    def action(self) -> None:
                        reveal_type(self.sudo())
                        reveal_type(self.with_context(a=1).with_company(1).with_user(1))
                        reveal_type(self.filtered(lambda r: r.size).sorted("size"))
                        reveal_type(self.search([]))
                        reveal_type(self.exists().ensure_one())
                        reveal_type(self.copy())
                        self.sudo().sizee
                        self.sudo().env["t.thng"]
                """
            }
        }
    )

    thing = "my_mod.models.Thing"
    assert result.messages("models.py") == [
        f'11: note: Revealed type is "{thing}"',
        f'12: note: Revealed type is "{thing}"',
        f'13: note: Revealed type is "{thing}"',
        f'14: note: Revealed type is "{thing}"',
        f'15: note: Revealed type is "{thing}"',
        f'16: note: Revealed type is "{thing}"',
        '17: error: "Thing" has no attribute "sizee"; maybe "size"?  [attr-defined]',
        '18: error: Unknown Odoo model "t.thng"  [odoo-model]',
    ]


def test_decorated_own_methods_keep_their_signature(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from typing import Final

                from odoo import api, fields, models

                class Thing(models.Model):
                    _name = "t.thing"

                    size = fields.Integer()
                    @api.private
                    def _helper(self, count: int) -> int:
                        return count

                    @api.returns("self")
                    def _records(self) -> "Thing":
                        return self

                    def action(self) -> None:
                        reveal_type(self._helper(1))
                        reveal_type(self._records())
                        self._helper("x")
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '19: note: Revealed type is "int"',
        '20: note: Revealed type is "my_mod.models.Thing"',
        '21: error: Argument 1 to "_helper" of "Thing" has incompatible type "str"; '
        'expected "int"  [arg-type]',
    ]


def test_unannotated_overrides_of_self_methods_keep_receiver_type(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from typing import Final

                from odoo import api, fields, models

                class Thing(models.Model):
                    _name = "t.thing"

                    size = fields.Integer()
                    @api.model_create_multi
                    def create(self, vals_list):
                        return super().create(vals_list)

                    @api.returns("self", lambda value: value.id)
                    def copy(self, default=None):
                        return super().copy(default)

                    def sudo(self, flag=True):
                        return super().sudo(flag)

                    def search_count(self, domain, limit=None):
                        return super().search_count(domain, limit)

                class Other:
                    def create(self):
                        return 1

                class User(models.Model):
                    _name = "t.user"

                    def action(self) -> None:
                        things = self.env["t.thing"]
                        reveal_type(things.create({}))
                        reveal_type(things.copy())
                        reveal_type(things.sudo())
                        reveal_type(things.search_count([]))
                        reveal_type(Other().create())
                """
            }
        }
    )

    thing = "my_mod.models.Thing"
    assert result.messages("models.py") == [
        f'33: note: Revealed type is "{thing}"',
        f'34: note: Revealed type is "{thing}"',
        f'35: note: Revealed type is "{thing}"',
        '36: note: Revealed type is "Any"',
        '37: note: Revealed type is "Any"',
    ]


def test_create_accepts_one_dict_or_a_list_of_dicts(run_mypy: RunMypy) -> None:
    # model_create_multi turns a single dict into a list at runtime.
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import fields, models

                class Thing(models.Model):
                    _name = "t.thing"

                    size = fields.Integer()

                    def action(self) -> None:
                        reveal_type(self.sudo().create({"size": 1}))
                        self.create([{"size": 1}, {"size": 2}])
                        self.env["t.thing"].with_context(a=1).create({})
                        self.create(1)
                """
            }
        }
    )

    messages = result.messages("models.py")
    assert messages[0] == '10: note: Revealed type is "my_mod.models.Thing"'
    assert len(messages) == 2
    assert messages[1].startswith('13: error: Argument 1 to "create" of "BaseModel" has')
    assert 'expected "dict[str, Any] | list[dict[str, Any]]"  [arg-type]' in messages[1]


# -- [27] env.user / env.company / env.companies ---------------------------------------------


def test_env_user_and_company_are_records_seen_from_the_addon(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import fields, models

                class Users(models.Model):
                    _inherit = "res.users"

                    x_level = fields.Integer()

                class Company(models.Model):
                    _inherit = "res.company"

                    x_code = fields.Char()

                class Thing(models.Model):
                    _name = "t.thing"

                    def action(self) -> None:
                        reveal_type(self.env.user.x_level)
                        reveal_type(self.env.company.x_code)
                        reveal_type(self.env.companies.sudo().x_code)
                        reveal_type(self.env.user.login)
                        self.env.user.x_levell
                """
            }
        }
    )

    messages = result.messages("models.py")
    assert messages[:4] == [
        '18: note: Revealed type is "int"',
        '19: note: Revealed type is "str | Literal[False]"',
        '20: note: Revealed type is "str | Literal[False]"',
        '21: note: Revealed type is "str | Literal[False]"',
    ]
    assert len(messages) == 5
    assert 'has no attribute "x_levell"' in messages[4]


def test_env_user_outside_addon_keeps_declared_type(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {"my_mod": {"models.py": THING}},
        scripts={
            "tool.py": """
            from odoo import api

            def run(env: api.Environment) -> None:
                reveal_type(env.user)
            """
        },
    )

    assert result.messages("tool.py") == ['5: note: Revealed type is "Any"']


# -- [12] Final model names in env[...] -------------------------------------------------------


def test_env_getitem_accepts_final_model_names(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from typing import Final

                from odoo import api, fields, models

                class Thing(models.Model):
                    _name = "t.thing"

                    size = fields.Integer()
                THING_MODEL: Final = "t.thing"
                TYPO: Final = "t.thng"

                class User(models.Model):
                    _name = "t.user"

                    def action(self) -> None:
                        reveal_type(self.env[THING_MODEL])
                        self.env[TYPO]
                        name = "t.thing"
                        reveal_type(self.env[name])
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '17: note: Revealed type is "my_mod.models.Thing"',
        '18: error: Unknown Odoo model "t.thng"  [odoo-model]',
        '20: note: Revealed type is "odoo.models.BaseModel"',
    ]


# -- roadmap 4.3: untyped decorator factories ---------------------------------------------------


def test_untyped_decorator_factories_keep_method_signature(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import api, fields, models

                class Thing(models.Model):
                    _name = "t.thing"

                    size = fields.Integer()

                    @api.onchange("size")
                    def _onchange_size(self) -> int:
                        return self.size

                    @api.ondelete(at_uninstall=False)
                    def _unlink_check(self) -> str:
                        return ""

                    @api.depends_context("company")
                    def _company_size(self) -> bool:
                        return True

                    def action(self) -> None:
                        reveal_type(self._onchange_size())
                        reveal_type(self._unlink_check())
                        reveal_type(self._company_size())
                        self._onchange_size(1)
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '22: note: Revealed type is "int"',
        '23: note: Revealed type is "str"',
        '24: note: Revealed type is "bool"',
        '25: error: Too many arguments for "_onchange_size" of "Thing"  [call-arg]',
    ]
