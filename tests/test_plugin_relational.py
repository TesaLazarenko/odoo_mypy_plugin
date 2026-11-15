"""End-to-end tests of relational fields and env[...] resolved from the reading addon.

A relational field returns the comodel as Odoo's registry has it, so a record read
through a field declared upstream has every extension the reading addon can see.
"""

from __future__ import annotations

import pytest
from conftest import OdooNames, RunMypy

DEP_PARTNER = """
from odoo import fields, models

class ResPartner(models.Model):
    _inherit = "res.partner"

    x_level = fields.Integer()
"""

# -- [0][26] comodel extended by the reading addon itself -------------------------------------


def test_relational_read_sees_extension_of_reading_addon(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "audit_a": {
                "models.py": """
                from odoo import fields, models

                class Parent(models.Model):
                    _name = "audit.parent"

                    name = fields.Char()
                    child_ids = fields.One2many("audit.child", "parent_id")

                class Child(models.Model):
                    _name = "audit.child"

                    parent_id = fields.Many2one("audit.parent")
                """
            },
            "audit_b": {
                "depends": ["audit_a"],
                "models.py": """
                from odoo import fields, models

                class Parent(models.Model):
                    _inherit = "audit.parent"

                    extra = fields.Integer()

                class Child(models.Model):
                    _inherit = "audit.child"

                    def use(self) -> None:
                        reveal_type(self.parent_id)
                        reveal_type(self.parent_id.extra)
                        for child in self.parent_id.child_ids:
                            reveal_type(child.parent_id.extra)
                """,
            },
        },
        check=["audit_b"],
    )

    assert result.messages("audit_b/models.py") == [
        '13: note: Revealed type is "audit_b.models.Parent"',
        '14: note: Revealed type is "int"',
        '16: note: Revealed type is "int"',
    ]


# -- [11] comodel extended by a dependency of the reading addon -------------------------------


def test_relational_read_sees_extension_of_dependency(run_mypy: RunMypy) -> None:
    # The comodel (x.target) is not named in vc: the field's comodel is.
    result = run_mypy(
        {
            "va": {
                "models.py": """
                from odoo import fields, models

                class Target(models.Model):
                    _name = "x.target"

                class Owner(models.Model):
                    _name = "x.owner"

                    target_id = fields.Many2one("x.target")
                    target_ids = fields.One2many("x.target", "owner_id")
                """
            },
            "vb": {
                "depends": ["va"],
                "models.py": """
                from odoo import fields, models

                class Target(models.Model):
                    _inherit = "x.target"

                    b = fields.Integer()
                """,
            },
            "vc": {
                "depends": ["vb"],
                "models.py": """
                from odoo import models

                class Owner(models.Model):
                    _inherit = "x.owner"

                    def use(self) -> None:
                        reveal_type(self.target_id.b)
                        for target in self.target_ids:
                            reveal_type(target.b)
                """,
            },
        },
        check=["vc"],
    )

    assert result.messages("vc/models.py") == [
        '8: note: Revealed type is "int"',
        '10: note: Revealed type is "int"',
    ]


def test_relational_read_of_core_field_sees_dependency_extension(run_mypy: RunMypy) -> None:
    # res.partner.parent_id is declared (and annotated) in base.
    result = run_mypy(
        {
            "rv_a": {"models.py": DEP_PARTNER},
            "rv_b": {
                "depends": ["rv_a"],
                "models.py": """
                from odoo import models

                class Thing(models.Model):
                    _name = "x.rvthing"

                    def go(self) -> None:
                        partner = self.env["res.partner"]
                        reveal_type(partner.parent_id.x_level)
                        reveal_type(partner.child_ids.x_level)
                """,
            },
        },
        check=["rv_b"],
    )

    assert result.messages("rv_b/models.py") == [
        '9: note: Revealed type is "int"',
        '10: note: Revealed type is "int"',
    ]


def test_relational_same_field_declared_in_two_views_is_compatible(run_mypy: RunMypy) -> None:
    # hr.employee: resource_id is declared by hr.employee.base (in hr, which extends
    # resource.resource) and by resource.mixin (in resource).
    result = run_mypy(
        {
            "t_res": {
                "models.py": """
                from odoo import fields, models

                class Resource(models.Model):
                    _name = "t.resource"

                class ResourceMixin(models.AbstractModel):
                    _name = "t.resource.mixin"

                    resource_id = fields.Many2one("t.resource")
                """
            },
            "t_hr": {
                "depends": ["t_res"],
                "models.py": """
                from odoo import fields, models

                class Resource(models.Model):
                    _inherit = "t.resource"

                    hr_field = fields.Integer()

                class EmployeeBase(models.AbstractModel):
                    _name = "t.employee.base"

                    resource_id = fields.Many2one("t.resource")

                class Employee(models.Model):
                    _name = "t.employee"
                    _inherit = ["t.employee.base", "t.resource.mixin"]

                    def use(self) -> None:
                        reveal_type(self.resource_id.hr_field)
                """,
            },
        },
        check=["t_hr"],
    )

    assert result.messages("t_hr/models.py") == ['19: note: Revealed type is "int"']


# Odoo 17's Many2one is not generic: the comodel is found when the field is read.
@pytest.mark.odoo(18, 19, 20)
def test_relational_field_declaration_is_bound_to_the_defining_class(
    run_mypy: RunMypy, names: OdooNames
) -> None:
    result = run_mypy(
        {
            "rv_a": {"models.py": DEP_PARTNER},
            "rv_b": {
                "depends": ["rv_a"],
                "models.py": """
                from odoo import fields, models

                class Thing(models.Model):
                    _name = "x.rvthing"

                    partner_id = fields.Many2one("res.partner")

                    def go(self) -> None:
                        reveal_type(Thing.partner_id)
                        reveal_type(self.partner_id.x_level)
                """,
            },
        },
        check=["rv_b"],
    )

    assert result.messages("rv_b/models.py") == [
        f'10: note: Revealed type is "{names.many2one}[{names.partner}]"',
        '11: note: Revealed type is "int"',
    ]


# -- [36] dependency edges: each pattern alone must load the extending module ----------------

USES = {
    "env_in_plain_model": """
        from odoo import models

        class Thing(models.Model):
            _name = "my.thing"

            def action(self) -> None:
                reveal_type(self.env["res.partner"].x_level)
        """,
    "env_local_name": """
        from odoo import models

        class Thing(models.Model):
            _name = "my.thing"

            def action(self) -> None:
                env = self.env
                reveal_type(env["res.partner"].x_level)
        """,
    "comodel_keyword": """
        from odoo import fields, models

        class Thing(models.Model):
            _name = "my.thing"

            partner_id = fields.Many2one(comodel_name="res.partner")

            def action(self) -> None:
                reveal_type(self.partner_id.x_level)
        """,
}


@pytest.mark.parametrize("use", sorted(USES))
def test_dependency_extension_is_loaded_for_each_reference_pattern(
    run_mypy: RunMypy, use: str
) -> None:
    result = run_mypy(
        {
            "dep_mod": {"models.py": DEP_PARTNER},
            "my_mod": {"depends": ["dep_mod"], "models.py": USES[use]},
        },
        check=["my_mod"],
    )

    assert [line.split(": ", 1)[1] for line in result.messages("my_mod/models.py")] == [
        'note: Revealed type is "int"'
    ]


def test_dependency_extension_is_loaded_for_unimported_test_file(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "dep_mod": {"models.py": DEP_PARTNER},
            "my_mod": {
                "depends": ["dep_mod"],
                "__init__.py": "",
                "tests/__init__.py": "from . import test_x\n",
                "tests/test_x.py": """
                from odoo.tests import TransactionCase

                class TestX(TransactionCase):
                    def test_level(self) -> None:
                        reveal_type(self.env["res.partner"].x_level)
                """,
            },
        },
        check=["my_mod"],
    )

    assert result.messages("test_x.py") == ['6: note: Revealed type is "int"']


# -- [45] annotated _name, custom relational subclass, model whose class is not loaded -------


def test_annotated_name_registers_model(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import models

                class Thing(models.Model):
                    _name: str = "my.thing"

                    def action(self) -> None:
                        reveal_type(self.env["my.thing"])
                """
            }
        }
    )

    assert result.messages("models.py") == ['8: note: Revealed type is "my_mod.models.Thing"']


# Odoo 17 declares no types (BaseModel, Field[T], _inherit): nothing to keep there.
@pytest.mark.odoo(18, 19, 20)
def test_custom_relational_subclass_reads_comodel(run_mypy: RunMypy, names: OdooNames) -> None:
    # Odoo 19 declares Many2one without a type parameter; the plugin adds one.
    partner_class = names.partner.rpartition(".")[2]
    result = run_mypy(
        {
            "my_mod": {
                "models.py": f"""
                from odoo import fields, models
                from odoo.addons.base.models.res_partner import {partner_class} as Partner

                class PartnerMany2one(fields.Many2one[Partner]):
                    pass

                class Thing(models.Model):
                    _name = "my.thing"

                    partner_id = PartnerMany2one("res.partner")

                    def action(self) -> None:
                        reveal_type(self.partner_id)
                """
            }
        }
    )

    assert result.messages("models.py") == [f'14: note: Revealed type is "{names.partner}"']


def test_env_model_whose_classes_are_not_loaded_falls_back_to_any(run_mypy: RunMypy) -> None:
    # --follow-imports=skip: dep_mod's classes are not analyzed. Its model is Any, and
    # res.partner keeps base's attributes while the unknown extension's are Any.
    dep = DEP_PARTNER + '\nclass Thing(models.Model):\n    _name = "dep.thing"\n'
    result = run_mypy(
        {
            "dep_mod": {"models.py": dep},
            "my_mod": {
                "depends": ["dep_mod"],
                "models.py": """
                from odoo import models

                class Thing(models.Model):
                    _name = "my.thing"

                    def action(self) -> None:
                        reveal_type(self.env["dep.thing"])
                        reveal_type(self.env["res.partner"].name)
                        reveal_type(self.env["res.partner"].x_level)
                """,
            },
        },
        check=["my_mod"],
        flags=["--follow-imports", "skip"],
    )

    assert result.messages("my_mod/models.py") == [
        '8: note: Revealed type is "Any"',
        '9: note: Revealed type is "str | Literal[False]"',
        '10: note: Revealed type is "Any"',
    ]


def test_relational_redefinition_without_comodel_needs_no_annotation(run_mypy: RunMypy) -> None:
    # crm.lead: ``campaign_id = fields.Many2one(ondelete="set null")`` keeps the comodel.
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import fields, models

                class Partner(models.Model):
                    _inherit = "res.partner"

                    parent_id = fields.Many2one(ondelete="set null")
                    child_ids = fields.One2many(string="Kids")
                """
            }
        }
    )

    assert result.messages("models.py") == []


def test_annotated_relational_fields_take_what_write_takes(run_mypy: RunMypy) -> None:
    # base annotates country_id: Country = fields.Many2one(...) and the like.
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import models
                from odoo.fields import Command

                class Thing(models.Model):
                    _name = "t.thing"

                    def action(self) -> None:
                        partner = self.env["res.partner"]
                        partner.country_id = False
                        partner.country_id = None
                        partner.country_id = 1
                        partner.country_id = self.env["res.country"]
                        partner.parent_id = partner
                        partner.child_ids = [Command.clear()]
                        partner.country_id = self.env["res.users"]
                        partner.country_id = "BE"
                        reveal_type(partner.country_id)
                """
            }
        }
    )

    messages = result.messages("models.py")
    # Annotated (Odoo 18+): a mypy assignment error; on Odoo 17 the check of __set__ (a
    # res.partner read in another addon is Any there).
    assert [message.partition(": ")[0] for message in messages] == ["16", "17", "18"]
    assert messages[0].endswith(("[assignment]", "[odoo-vals-type]"))
    assert messages[1].endswith(("[assignment]", "[odoo-vals-type]"))
    assert messages[2].startswith('18: note: Revealed type is "')
