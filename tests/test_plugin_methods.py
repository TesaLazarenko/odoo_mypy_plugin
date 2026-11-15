"""End-to-end tests of recordset methods and environment attributes.

Odoo 18 declares ``sudo``, ``search``, ``filtered``... as ``-> Self``, but wraps them
in untyped decorators (``api.private``, ``api.returns``), and addons override them
without annotations: the plugin keeps the receiver's model type through both.
"""

from __future__ import annotations

import pytest
from conftest import OdooNames, RunMypy

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
                        reveal_type(self.filtered(lambda r: r.size > 0).sorted("size"))
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

                    def action(self) -> None:
                        reveal_type(self._helper(1))
                        self._helper("x")
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '15: note: Revealed type is "int"',
        '16: error: Argument 1 to "_helper" of "Thing" has incompatible type "str"; '
        'expected "int"  [arg-type]',
    ]


@pytest.mark.odoo(18)  # Odoo 19 has no api.returns
def test_returns_decorated_method_keeps_its_signature(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import api, models

                class Thing(models.Model):
                    _name = "t.thing"

                    @api.returns("self")
                    def _records(self) -> "Thing":
                        return self

                    def action(self) -> None:
                        reveal_type(self._records())
                """
            }
        }
    )

    assert result.messages("models.py") == ['12: note: Revealed type is "my_mod.models.Thing"']


def test_unannotated_overrides_of_self_methods_keep_receiver_type(
    run_mypy: RunMypy, names: OdooNames
) -> None:
    # Odoo 19 has no api.returns: a comment keeps the line numbers.
    copy_decorator = '@api.returns("self", lambda value: value.id)'
    if names.version >= 19:
        copy_decorator = "# no api.returns in this Odoo"
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

                    COPY_DECORATOR
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
                """.replace("COPY_DECORATOR", copy_decorator)
            }
        }
    )

    thing = "my_mod.models.Thing"
    assert result.messages("models.py") == [
        f'33: note: Revealed type is "{thing}"',
        f'34: note: Revealed type is "{thing}"',
        f'35: note: Revealed type is "{thing}"',
        '36: note: Revealed type is "Any"',  # unannotated, not a -> Self method
        '37: note: Revealed type is "Any"',
    ]


def test_checks_run_on_self_typed_as_a_type_variable(run_mypy: RunMypy, names: OdooNames) -> None:
    # In an unannotated create / copy override the plugin types ``self`` as ``Self``,
    # and mypy then names the hooks "search of BaseModel" (or "write" for an own def).
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from typing import Self, TypeVar

                from odoo import api, fields, models

                T = TypeVar("T", bound="Thing")

                class Thing(models.Model):
                    _name = "t.thing"

                    state = fields.Selection([("a", "A")])
                    @api.model_create_multi
                    def create(self, vals_list):
                        self.search([("nope", "=", 1)])
                        self.write({"nope": 1})
                        self.mapped("nope")
                        if self.state == "zzz":
                            pass
                        records = super().create(vals_list)
                        records.write({"nope": 1})
                        reveal_type(records)
                        return records

                    def write(self, vals):
                        return super().write(vals)

                    def annotated(self) -> Self:
                        self.search([("nope", "=", 1)])
                        return self.sudo()

                    def bound(self: T) -> T:
                        self.search_count([("nope", "=", 1)])
                        self.filtered("nope")
                        return self
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '14: error: Odoo model "t.thing" has no field "nope" in domain  [odoo-domain]',
        '15: error: Odoo model "t.thing" has no field "nope"  [odoo-vals-key]',
        '16: error: Odoo model "t.thing" has no field "nope" in mapped("nope")  [odoo-field-ref]',
        '17: error: Value "zzz" is not in the selection of field "state" of Odoo model'
        ' "t.thing" ("a")  [odoo-selection]',
        *(
            ['21: note: Revealed type is "Any"']  # Odoo 17: create is unannotated
            if names.version == 17
            else [
                '20: error: Odoo model "t.thing" has no field "nope"  [odoo-vals-key]',
                '21: note: Revealed type is "Self"',
            ]
        ),
        '28: error: Odoo model "t.thing" has no field "nope" in domain  [odoo-domain]',
        '32: error: Odoo model "t.thing" has no field "nope" in domain  [odoo-domain]',
        '33: error: Odoo model "t.thing" has no field "nope" in filtered("nope")  [odoo-field-ref]',
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
    expected = messages[1].partition("; expected ")[2].removesuffix("  [arg-type]")
    assert set(expected.strip('"').split(" | ")) == {"dict[str, Any]", "list[dict[str, Any]]"}


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


def test_env_user_outside_addon_keeps_declared_type(run_mypy: RunMypy, names: OdooNames) -> None:
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

    # Odoo 18 does not annotate Environment.user; Odoo 19 declares -> BaseModel.
    declared = "Any" if names.version < 19 else names.base_model
    assert result.messages("tool.py") == [f'5: note: Revealed type is "{declared}"']


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
        '20: note: Revealed type is "Any"',  # a plain variable: dynamic_env = "any"
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


# -- roadmap 4.10: env[...] with a name known only at run time ----------------------------------

DYNAMIC_ENV = """
from odoo import models

class Thing(models.Model):
    _name = "t.thing"

    def action(self, model_name: str) -> None:
        records = self.env[model_name]
        reveal_type(records)
        records.with_company(1).partner_id
"""


def test_env_dynamic_name_default_is_any(run_mypy: RunMypy) -> None:
    result = run_mypy({"my_mod": {"models.py": DYNAMIC_ENV}})

    assert result.messages("models.py") == ['9: note: Revealed type is "Any"']


# Odoo 17 declares no types (BaseModel, Field[T], _inherit): nothing to keep there.
@pytest.mark.odoo(18, 19, 20)
def test_env_dynamic_name_option_base_keeps_base_model(run_mypy: RunMypy, names: OdooNames) -> None:
    result = run_mypy({"my_mod": {"models.py": DYNAMIC_ENV}}, options='dynamic_env = "base"\n')

    assert result.messages("models.py") == [
        f'9: note: Revealed type is "{names.base_model}"',
        '10: error: "BaseModel" has no attribute "partner_id"  [attr-defined]',
    ]


# Odoo 17 declares no types (BaseModel, Field[T], _inherit): nothing to keep there.
@pytest.mark.odoo(18, 19, 20)
def test_env_dynamic_name_option_change_invalidates_cache(
    run_mypy: RunMypy, names: OdooNames
) -> None:
    run_mypy({"my_mod": {"models.py": DYNAMIC_ENV}})

    result = run_mypy({"my_mod": {"models.py": DYNAMIC_ENV}}, options='dynamic_env = "base"\n')

    assert f'9: note: Revealed type is "{names.base_model}"' in result.messages("models.py")


OWN_NAME_ENV = """
from odoo import fields, models

class Mixin(models.AbstractModel):
    _name = "t.mixin"

    def touch(self) -> None:
        reveal_type(self.env[self._name])

class Thing(models.Model):
    _name = "t.thing"
    _inherit = ["t.mixin"]

    size = fields.Integer()

    def action(self) -> None:
        reveal_type(self.env[self._name].search([]).size)
        self.env[self._name].sizes

    def copy(self, default=None):
        reveal_type(self.env[self._name])
        return super().copy(default)
"""


def test_env_own_name_is_the_type_of_the_records(run_mypy: RunMypy) -> None:
    result = run_mypy({"my_mod": {"models.py": OWN_NAME_ENV}})

    assert result.messages("models.py") == [
        '8: note: Revealed type is "my_mod.models.Mixin"',
        '17: note: Revealed type is "int"',
        '18: error: "Thing" has no attribute "sizes"; maybe "size"?  [attr-defined]',
        '21: note: Revealed type is "Self"',
    ]


# -- roadmap 4.4: super() in unannotated overrides -------------------------------------------


def test_super_call_to_unannotated_override_keeps_self_type(
    run_mypy: RunMypy, names: OdooNames
) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import api, fields, models

                class Thing(models.Model):
                    _name = "t.thing"

                    size = fields.Integer()

                    @api.model_create_multi
                    def create(self, vals_list):
                        return super().create(vals_list)

                    def copy(self, default=None):
                        return super().copy(default)

                class ThingMore(models.Model):
                    _inherit = "t.thing"

                    @api.model_create_multi
                    def create(self, vals_list):
                        records = super().create(vals_list)
                        reveal_type(records)
                        records.sizee + records.size
                        return records

                    def copy(self, default=None):
                        reveal_type(super().copy(default))
                        return super().copy(default)

                    def search_count(self, domain, limit=None):
                        reveal_type(super().search_count(domain, limit))
                        return 0
                """
            }
        }
    )

    # Odoo 18 leaves BaseModel.search_count untyped; Odoo 19 declares -> int.
    count = "Any" if names.version < 19 else "int"
    # Inside the override ``self`` is ``Self`` (bound to ThingMore), so is super()'s result.
    messages = result.messages("models.py")
    assert messages == [
        '22: note: Revealed type is "Self"',
        '23: error: "Self" has no attribute "sizee"  [attr-defined]',
        '27: note: Revealed type is "Self"',
        f'31: note: Revealed type is "{count}"',  # not a -> Self method: unchanged
    ]


# -- roadmap 6.7: a -> Self override that returns other records --------------------------------

OTHER_RECORDS_OVERRIDE = """
from odoo import api, fields, models

class Thing(models.Model):
    _name = "t.thing"

    size = fields.Integer()

    def copy(self, default=None):
        if default:
            return self.env["t.thing"].browse(1)
        return super().copy(default)

    def with_company(self, company):
        records = super().with_company(company)
        return records.sudo() if company else records | self

    def action(self) -> None:
        reveal_type(self.copy())
        reveal_type(self.with_company(1))
"""


def test_self_override_returning_other_records_stays_unannotated(run_mypy: RunMypy) -> None:
    result = run_mypy({"my_mod": {"models.py": OTHER_RECORDS_OVERRIDE}})

    assert result.messages("models.py") == [
        '19: note: Revealed type is "my_mod.models.Thing"',
        '20: note: Revealed type is "my_mod.models.Thing"',
    ]


@pytest.mark.odoo(19, 20)
def test_filtered_function_may_return_any_value(run_mypy: RunMypy) -> None:
    # Odoo declares Callable[[Self], bool] but keeps the records whose result is truthy.
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import fields, models

                class Thing(models.Model):
                    _name = "t.thing"

                    partner_id = fields.Many2one("res.partner")
                    def action(self) -> None:
                        reveal_type(self.filtered(lambda r: r.partner_id))
                        self.filtered(lambda r: r.partner_id.name.upper())
                        self.filtered(lambda r: r.sizee)
                        self.filtered(1)
                """
            }
        }
    )

    messages = result.messages("models.py")
    assert messages[0] == '9: note: Revealed type is "my_mod.models.Thing"'
    assert messages[1].startswith('10: error: Item "bool" of "str | Literal[False]"')
    assert messages[2] == '11: error: "Thing" has no attribute "sizee"  [attr-defined]'
    assert messages[3].startswith('12: error: Argument 1 to "filtered" of "BaseModel"')
    assert len(messages) == 4


def test_commands_take_the_id_of_a_record(run_mypy: RunMypy) -> None:
    # record.id is int | NewId on Odoo 17 and 18; Odoo declares Command.link(id: int).
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import fields, models
                from odoo.fields import Command

                class Thing(models.Model):
                    _name = "t.thing"

                    partner_ids = fields.Many2many("res.partner")
                    def action(self) -> None:
                        partner = self.env["res.partner"].new({})
                        self.write({"partner_ids": [Command.link(partner.id)]})
                        Command.update(partner.id, {})
                        Command.delete(partner.id)
                        Command.unlink(partner.id)
                        Command.link("1")
                """
            }
        }
    )

    messages = result.messages("models.py")
    assert len(messages) == 1
    assert messages[0].startswith('15: error: Argument 1 to "link" of "Command"')


def test_hooks_of_view_classes_keep_their_names(run_mypy: RunMypy) -> None:
    # A model extended by two addons of the closure is a merged view class, whose name has
    # " of " in it (<subclass of "t.thing" seen from "my_use">): no "search of BaseModel".
    result = run_mypy(
        {
            "my_mod": {"models.py": THING},
            "my_ext": {
                "depends": ["my_mod"],
                "models.py": """
                from odoo import fields, models

                class Thing(models.Model):
                    _inherit = "t.thing"

                    extra = fields.Char()
                """,
            },
            "my_other": {
                "depends": ["my_mod"],
                "models.py": """
                from odoo import fields, models

                class Thing(models.Model):
                    _inherit = "t.thing"

                    other = fields.Char()
                """,
            },
            "my_use": {
                "depends": ["my_ext", "my_other"],
                "models.py": """
                from odoo import models

                class User(models.Model):
                    _name = "t.user"

                    def action(self) -> None:
                        things = self.env["t.thing"]
                        things.create({"extra": "x"})
                        things.create({"nope": 1})
                        things.search([("nope", "=", 1)])
                        things.mapped("nope")
                """,
            },
        },
        check=["my_use"],
    )

    assert result.messages("my_use/models.py") == [
        '10: error: Odoo model "t.thing" has no field "nope"  [odoo-vals-key]',
        '11: error: Odoo model "t.thing" has no field "nope" in domain  [odoo-domain]',
        '12: error: Odoo model "t.thing" has no field "nope" in mapped("nope")  [odoo-field-ref]',
    ]
