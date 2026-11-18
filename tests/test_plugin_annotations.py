"""End-to-end tests of ``odoo.values.*`` / ``odoo.model.*`` annotations (roadmap stage 7)."""

from __future__ import annotations

from pathlib import Path

from conftest import RunMypy

T_BASE = """
from odoo import fields, models

class Order(models.Model):
    _name = "t.order"

    name = fields.Char()
    count = fields.Integer()
    state = fields.Selection([("draft", "Draft"), ("done", "Done")])
    partner_id = fields.Many2one("res.partner")
    line_ids = fields.One2many("t.line", "order_id")

class Line(models.Model):
    _name = "t.line"

    order_id = fields.Many2one("t.order")
    qty = fields.Float()
"""
T_EXT = """
from odoo import fields, models

class Order(models.Model):
    _inherit = "t.order"

    ext_note = fields.Char()

class Line(models.Model):
    _inherit = "t.line"

    ext_flag = fields.Integer()
"""
# Not a dependency of the addons below: its fields are keys of odoo.values.t_order all
# the same (a field of an optional addon, under "if 'f' in self._fields").
T_OTHER = """
from odoo import fields, models

class Order(models.Model):
    _inherit = "t.order"

    other_flag = fields.Integer()

class Thing(models.Model):
    _name = "t.thing"

class Line(models.Model):
    _inherit = "t.line"

    other_note = fields.Char()
"""


def _addons(**extra: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        "t_base": {"models.py": T_BASE},
        "t_ext": {"depends": ["t_base"], "models.py": T_EXT},
        "t_other": {"depends": ["t_base"], "models.py": T_OTHER},
        **extra,
    }


def _messages(run_mypy: RunMypy, source: str, depends: list[str] | None = None) -> list[str]:
    addons = _addons(t_use={"depends": depends or ["t_base"], "models.py": source})
    return run_mypy(addons, check=["t_use"]).messages("t_use/models.py")


VALUES = """
from typing import Annotated, Any
from odoo import models
from odoo.fields import Command

def takes_dict(d: dict) -> None: ...
def takes_str_any(d: dict[str, Any]) -> None: ...

class Order(models.Model):
    _inherit = "t.order"

    def _prepare(self) -> Annotated[dict, "odoo.values.t_order"]:
        return {"name": "x", "nmae": "y"}

    def _counted(self) -> "odoo.values.t_order":
        return {"count": "three"}

    def _line(self) -> "odoo.values.t_line":
        return {"qty": 1.0}

    def use(self) -> None:
        vals: "odoo.values.t_order" = {"count": 1}
        vals["cuont"] = 2
        vals["state"] = self.name
        vals["other_flag"] = 3
        vals["line_ids"] = [Command.create(self._line())]
        self.create(vals)
        self.create([vals, self._prepare()])
        self.write(self._prepare())
        takes_dict(vals)
        takes_str_any(vals)
        self.env["t.line"].create(vals)
        self.write({"line_ids": [Command.create(vals)]})
        self.write({"line_ids": [(0, 0, self._line())]})
"""


def test_values_annotation_checks_keys_values_and_model(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, VALUES)

    assert messages == [
        '13: error: Extra key "nmae" for TypedDict "t.order values"  [typeddict-unknown-key]',
        '16: error: Incompatible types (expression has type "str", TypedDict item "count" has '
        'type "int | NewId | bool | None")  [typeddict-item]',
        '23: error: TypedDict "t.order values" has no key "cuont"  [typeddict-unknown-key]',
        '23: note: Did you mean "count"?',
        '32: error: Values of Odoo model "t.order" given to create() of Odoo model "t.line"'
        "  [odoo-vals-type]",
        '33: error: Values of Odoo model "t.order" in a command of field "line_ids" of Odoo '
        'model "t.order", which takes "t.line" values  [odoo-vals-type]',
    ]


SUPER_A = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def _prepare(self) -> "odoo.values.t_order":
        return {"name": "a"}
"""
SUPER_B = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def _prepare(self) -> "odoo.values.t_order":
        res = super()._prepare()
        res["ext_note"] = "b"
        res["ext_nte"] = "b"
        return res
"""


def test_values_of_super_in_a_later_addon_have_its_keys(run_mypy: RunMypy) -> None:
    addons = _addons(
        t_a={"depends": ["t_base"], "models.py": SUPER_A},
        t_b={"depends": ["t_a", "t_ext"], "models.py": SUPER_B},
    )

    result = run_mypy(addons, check=["t_a", "t_b"])

    assert result.messages("t_a/models.py") == []
    assert result.messages("t_b/models.py") == [
        '10: error: TypedDict "t.order values" has no key "ext_nte"  [typeddict-unknown-key]',
        '10: note: Did you mean "ext_note"?',
    ]


FORMS = """
from __future__ import annotations

from typing import Mapping
from odoo import models

MODULE_VALS: "odoo.values.t_order" = {"nope1": 1}

class Order(models.Model):
    _inherit = "t.order"

    def f(
        self,
        vals: "odoo.values.t_order | None" = None,
        many: "list[odoo.values.t_order]" = [],
    ) -> "Mapping[str, odoo.model.t_order]":
        if vals is not None:
            vals["nope2"] = 1
        many[0]["nope3"] = 1
        commented = {}  # type: odoo.values.t_order
        commented["nope4"] = 1
        for item in many:  # type: odoo.values.t_order
            item["nope5"] = 1

        def inner() -> odoo.values.t_line:
            return {"nope6": 1}

        reveal_type(self.f()["x"])
        return {}
"""


def test_bare_string_annotations_in_every_place(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, FORMS)

    assert [line.split("  [")[0] for line in messages] == [
        '7: error: Extra key "nope1" for TypedDict "t.order values"',
        '18: error: TypedDict "t.order values" has no key "nope2"',
        '19: error: TypedDict "t.order values" has no key "nope3"',
        '21: error: TypedDict "t.order values" has no key "nope4"',
        '23: error: TypedDict "t.order values" has no key "nope5"',
        '26: error: Extra key "nope6" for TypedDict "t.line values"',
        '28: note: Revealed type is "t_use.models.Order"',
    ]


M_ONE = 'from odoo import fields, models\nclass Line(models.Model):\n    _inherit = "t.line"\n'
M_ONE += "    one = fields.Char()\n"
M_TWO = M_ONE.replace("one", "two")
MODELS = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def own(self) -> "odoo.model.t_order":
        return self

    def use(self, line: "odoo.model.t_line") -> None:
        reveal_type(line)
        reveal_type(line.one)
        reveal_type(line.two)
        line.qyt
        records = self.env["t.line"]
        records = line
        reveal_type(self.own())
"""


def test_model_annotation_is_env_records_of_the_addon(run_mypy: RunMypy) -> None:
    addons = _addons(
        m_one={"depends": ["t_base"], "models.py": M_ONE},
        m_two={"depends": ["t_base"], "models.py": M_TWO},
        t_use={"depends": ["m_one", "m_two"], "models.py": MODELS},
    )

    messages = run_mypy(addons, check=["t_use"]).messages("t_use/models.py")

    merged = '"t_use.models.<subclass of "t.line" seen from "t_use">"'
    assert messages == [
        f"11: note: Revealed type is {merged}",
        '12: note: Revealed type is "str | Literal[False]"',
        '13: note: Revealed type is "str | Literal[False]"',
        '14: error: "t.line" has no attribute "qyt"  [attr-defined]',
        '17: note: Revealed type is "t_use.models.Order"',
    ]


ERRORS = """
from typing import Annotated, Required, TypedDict
from odoo import models

class Meta(TypedDict, total=False):
    a: Annotated[Required[int], "metadata"]

META: Meta = {}

class Order(models.Model):
    _inherit = "t.order"

    def a(self) -> "odoo.values.t_ordr":
        return {}

    def b(self) -> "odoo.model.t_thing":
        return self.env["t.order"]

    def c(self) -> "odoo.values.t_thing":  # type: ignore[odoo-model]
        return {"anything": 1}

    def d(self) -> Annotated[int, "odoo.values.t_order"]:
        return 1
"""


def test_annotation_errors(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, ERRORS)

    assert messages == [
        '8: error: Missing key "a" for TypedDict "Meta"  [typeddict-item]',
        '13: error: Unknown Odoo model in "odoo.values.t_ordr"; did you mean "t_order"?'
        "  [odoo-model]",
        '16: error: Odoo model "t.thing" ("odoo.model.t_thing") is defined in addon '
        '"t_other", which is not in the dependencies of "t_use"  [odoo-model-depends]',
        '22: error: "odoo.values.t_order" needs a dict type in Annotated[...]  [valid-type]',
    ]


AMBIGUOUS = """
from odoo import models

class A(models.Model):
    _name = "t.a_b"

class B(models.Model):
    _name = "t_a.b"

    def f(self) -> "odoo.values.t_a_b":
        return {"anything": 1}
"""


def test_ambiguous_alias_is_reported(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, AMBIGUOUS)

    assert messages == [
        '10: error: Odoo model alias "odoo.values.t_a_b" is ambiguous: "t.a_b", "t_a.b"'
        "  [odoo-model]"
    ]


SCRIPT = """
from typing import Annotated

annotated: Annotated[dict, "odoo.values.t_order"] = {"anything": 1}
bare: "odoo.values.t_order" = {}
"""


def test_outside_any_addon_annotations_keep_plain_mypy_meaning(run_mypy: RunMypy) -> None:
    result = run_mypy(_addons(), check=["t_base"], scripts={"tool.py": SCRIPT})

    assert result.messages("tool.py") == ['5: error: Name "odoo" is not defined  [name-defined]']


CACHED = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def f(self) -> "odoo.values.t_order":
        return {"name": "x", "late": 1}
"""


def test_values_keys_follow_a_field_added_later(run_mypy: RunMypy) -> None:
    addons = _addons(t_use={"depends": ["t_base"], "models.py": CACHED})
    first = run_mypy(addons, check=["t_use"], fresh_cache=True)
    later = T_BASE.replace("count = fields.Integer()", "count = 1\n    late = fields.Integer()")
    later = later.replace("count = 1", "count = fields.Integer()")
    addons["t_base"] = {"models.py": later}

    second = run_mypy(addons, check=["t_use"], fresh_cache=True)

    assert first.messages("t_use/models.py") == [
        '8: error: Extra key "late" for TypedDict "t.order values"  [typeddict-unknown-key]'
    ]
    assert second.messages("t_use/models.py") == []


def test_cold_and_warm_runs_report_the_same(run_mypy: RunMypy) -> None:
    addons = _addons(t_use={"depends": ["t_base"], "models.py": VALUES})

    cold = run_mypy(addons, check=["t_use"], fresh_cache=True)
    warm = run_mypy(addons, check=["t_use"], fresh_cache=True)

    assert [line for line in cold.lines if "[odoo-" in line]  # not two empty reports
    assert warm.lines == cold.lines


HELPER = '    line_ids = fields.One2many("t.line", "order_id")\n'
T_HELPER = T_BASE.replace(HELPER, f"{HELPER}\n    def helper(self) -> str:\n        return ''\n")
STALE = """
from odoo import models

def before(order: "odoo.model.t_order") -> "odoo.values.t_order":
    order.helper()
    return {"nope1": 1}

class Order(models.Model):
    _inherit = "t.order"

    def f(self) -> "odoo.values.t_order":
        self.helper()
        return {"nope2": 1}
"""
# A module without a class: mypy looks its bare strings up itself (odoo_typing.alias_names).
TOOLS = """
from typing import Annotated
from t_base.models import Order

def bare(order: Order) -> "odoo.values.t_order":
    order.helper()
    return {"nope0": 1}

def annotated(order: Order) -> Annotated[dict, "odoo.values.t_order"]:
    return {"nope3": 1}

def local() -> None:
    class Inner:  # a class in a function does not make the plugin rewrite the module
        pass
"""
# A module that binds the name odoo, with a helper above its first class.
BOUND = """
import odoo
from odoo import models

def helper(order: "odoo.model.t_order") -> "odoo.values.t_order":
    order.helper()
    return {"nope4": 1}

class Order(models.Model):
    _inherit = "t.order"
"""


def _stale_addons(base: str) -> dict[str, dict[str, object]]:
    files = {"models.py": STALE, "tools.py": TOOLS, "bound.py": BOUND}
    addons = _addons(t_use={"depends": ["t_base"], **files})
    addons["t_base"] = {"models.py": base}
    return addons


def test_bare_strings_survive_a_dependency_signature_change(run_mypy: RunMypy) -> None:
    # The cached modules of t_use are parsed again (their dependency changed), without
    # mypy asking for their dependencies.
    cold = run_mypy(_stale_addons(T_HELPER), check=["t_use"], fresh_cache=True)
    changed = T_HELPER.replace("helper(self)", "helper(self, x: int = 0)")

    warm = run_mypy(_stale_addons(changed), check=["t_use"], fresh_cache=True)

    for result in (cold, warm):
        assert result.messages("t_use/models.py") == [
            '6: error: Extra key "nope1" for TypedDict "t.order values"  [typeddict-unknown-key]',
            '13: error: Extra key "nope2" for TypedDict "t.order values"  [typeddict-unknown-key]',
        ]
        assert result.messages("t_use/tools.py") == [
            '7: error: Extra key "nope0" for TypedDict "t.order values"  [typeddict-unknown-key]',
            '10: error: Extra key "nope3" for TypedDict "t.order values"  [typeddict-unknown-key]',
        ]
        assert result.messages("t_use/bound.py") == [
            '7: error: Extra key "nope4" for TypedDict "t.order values"  [typeddict-unknown-key]'
        ]


CYCLE_A = """
from odoo import models
from . import b

A = 1

class Order(models.Model):
    _inherit = "t.order"

    def a(self) -> "odoo.values.t_order":
        return {"nope_a": b.B}
"""
CYCLE_B = """
from odoo import models
from . import a

B = 1

class Line(models.Model):
    _inherit = "t.line"

    def b(self) -> "odoo.values.t_line":
        return {"nope_b": a.A}
"""


def _cycle_addons(module_a: str) -> dict[str, dict[str, object]]:
    cycle = {
        "__init__.py": "from . import models\n",
        "models/__init__.py": "from . import a, b\n",
        "models/a.py": module_a,
        "models/b.py": CYCLE_B,
    }
    return _addons(t_use={"depends": ["t_base"], **cycle})


def test_bare_strings_survive_a_body_edit_in_an_import_cycle(run_mypy: RunMypy) -> None:
    # Editing a.py makes mypy parse b.py (same import cycle) again from its cache entry.
    run_mypy(_cycle_addons(CYCLE_A), check=["t_use"], fresh_cache=True)

    warm = run_mypy(
        _cycle_addons(CYCLE_A.replace("nope_a", "nope_c")), check=["t_use"], fresh_cache=True
    )

    assert warm.messages("t_use/models/a.py") == [
        '11: error: Extra key "nope_c" for TypedDict "t.order values"  [typeddict-unknown-key]'
    ]
    assert warm.messages("t_use/models/b.py") == [
        '11: error: Extra key "nope_b" for TypedDict "t.line values"  [typeddict-unknown-key]'
    ]


def test_parallel_workers_report_the_same(run_mypy: RunMypy) -> None:
    addons = _stale_addons(T_HELPER)
    addons["t_use"]["values.py"] = VALUES

    sequential = run_mypy(addons, check=["t_use"], fresh_cache=True)
    parallel = run_mypy(addons, check=["t_use"], flags=["--num-workers", "2"])

    assert sorted(parallel.lines) == sorted(sequential.lines)
    assert any("values.py:13: error" in line for line in parallel.lines)


CREATE_LISTS = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def _vals(self) -> "odoo.values.t_order":
        return {"name": "x"}

    def _many(self) -> "list[odoo.values.t_order]":
        return [self._vals()]

    def use(self) -> None:
        listed: "list[odoo.values.t_order]" = [self._vals()]
        self.create(listed)
        self.create(self._many())
        self.create([self._vals() for _ in range(2)])
        appended = []
        appended.append(self._vals())
        self.create(appended)
        self.env["t.line"].create(listed)
        self.create(1)
"""


def test_create_takes_a_list_of_values(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, CREATE_LISTS)

    assert [line.split(";")[0] for line in messages] == [
        '21: error: Values of Odoo model "t.order" given to create() of Odoo model "t.line"'
        "  [odoo-vals-type]",
        '22: error: Argument 1 to "create" of "BaseModel" has incompatible type "int"',
    ]
    # Where no list of values is passed: one dict, or any iterable of dicts (Odoo reads
    # the list once).
    expected = messages[1].split("; expected ")[1].removesuffix("  [arg-type]").strip('"')
    assert set(expected.split(" | ")) == {"dict[str, Any]", "Iterable[dict[str, Any]]"}


DRIFT_A = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def _prepare(self) -> "odoo.values.t_order":
        return {"name": "a"}
"""
DRIFT_B = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def _prepare(self) -> "odoo.values.t_order":
        vals = super()._prepare()
        vals["c_flag"] = 1
        return vals
"""
DRIFT_C = 'from odoo import fields, models\nclass Order(models.Model):\n    _inherit = "t.order"\n'


def _drift_addons(c_body: str) -> dict[str, dict[str, object]]:
    return _addons(
        t_a={"depends": ["t_base"], "models.py": DRIFT_A},
        t_c={"depends": ["t_base"], "models.py": DRIFT_C + c_body},
        t_b={"depends": ["t_a", "t_c"], "models.py": DRIFT_B},
    )


def test_values_keys_agree_between_cached_and_checked_modules(run_mypy: RunMypy) -> None:
    # t_a does not depend on t_c, t_b does: a field t_c adds must reach t_a's cached
    # TypedDict too, or super()._prepare() in t_b returns other keys than t_b's.
    first = run_mypy(_drift_addons("    other = fields.Char()\n"), fresh_cache=True)

    second = run_mypy(_drift_addons("    c_flag = fields.Integer()\n"), fresh_cache=True)

    assert first.messages("t_b/models.py") == [
        '9: error: TypedDict "t.order values" has no key "c_flag"  [typeddict-unknown-key]'
    ]
    assert second.lines[-1].startswith("Success: no issues found")


INHERITED = """
from odoo import fields, models

class Mixin(models.AbstractModel):
    _name = "t.mixin"
    tag = fields.Char()

class Template(models.Model):
    _name = "t.template"
    list_price = fields.Float()

class Product(models.Model):
    _name = "t.product"
    _inherit = ["t.mixin"]
    template_id = fields.Many2one("t.template", delegate=True)  # Odoo 19+: no dict _inherits

    def _template(self) -> "odoo.values.t_template":
        return {"list_price": 1.0}

    def _mixin(self) -> "odoo.values.t_mixin":
        return {"tag": "x"}

    def _product(self) -> "odoo.values.t_product":
        return {"tag": "x", "list_price": 2.0}

    def use(self) -> None:
        self.create(self._template())
        self.create([self._mixin()])
        self.env["t.template"].create(self._product())
"""


def test_values_of_parents_and_delegated_models_are_accepted(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, INHERITED)

    assert messages == [
        '29: error: Values of Odoo model "t.product" given to create() of Odoo model '
        '"t.template"  [odoo-vals-type]'
    ]


GET = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def use(self, vals: "odoo.values.t_order", plain: dict[str, int]) -> None:
        reveal_type(vals.get("name"))
        reveal_type(vals.get("name", "x"))
        reveal_type(vals.get("nmae"))
        reveal_type(plain.get("x"))
"""


def test_get_of_values_is_typed_like_a_typeddict(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, GET)

    assert messages == [
        '8: note: Revealed type is "str | bool | None"',
        '9: note: Revealed type is "str | bool | None"',
        '10: note: Revealed type is "Any | None"',  # no error, as for any TypedDict
        '11: note: Revealed type is "int | None"',
    ]


DICT_HELPERS = (
    T_BASE
    + """
class Helpers(models.Model):
    _inherit = "t.order"

    def _helper(self) -> dict:
        return {}

    def _helper2(self) -> dict:
        return {}
"""
)
OVERRIDES = """
from typing import Annotated, cast
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def _helper(self) -> "odoo.values.t_order":
        return super()._helper()

    def _helper2(self) -> "odoo.values.t_order":
        return cast(Annotated[dict, "odoo.values.t_order"], super()._helper2())
"""


def test_override_of_a_dict_helper_needs_a_cast(run_mypy: RunMypy) -> None:
    # Odoo 19+ annotates a few helpers "-> dict": super() returns a plain dict there.
    addons = _addons(t_use={"depends": ["t_base"], "models.py": OVERRIDES})
    addons["t_base"] = {"models.py": DICT_HELPERS}

    messages = run_mypy(addons, check=["t_use"]).messages("t_use/models.py")

    assert messages == [
        '9: error: Incompatible return value type (got "dict[Any, Any]", expected '
        '"t.order values")  [return-value]'
    ]


NAMED = """
from odoo import fields, models

MODEL = "t.const"

class Ann(models.Model):
    _name: str = "t.annotated"
    title = fields.Char()

class Const(models.Model):
    _name = MODEL
    code = fields.Char()

class Ext(models.Model):
    _inherit: str = "t.order"
    ann_note = fields.Char()
"""
USE_NAMED = """
from odoo import models

class Ann(models.Model):
    _inherit = "t.annotated"

    def a(self) -> "odoo.values.t_annotated":
        return {"title": "x", "nope_a": 1}

    def c(self) -> "odoo.values.t_const":
        return {"code": "x", "nope_c": 1}

    def o(self) -> "odoo.values.t_order":
        return {"ann_note": "x"}
"""


def test_values_keys_of_annotated_and_constant_model_names(run_mypy: RunMypy) -> None:
    # The scanner evaluates these _name / _inherit forms; the keys come from those files.
    addons = _addons(
        t_named={"depends": ["t_base"], "models.py": NAMED},
        t_use={"depends": ["t_named"], "models.py": USE_NAMED},
    )

    messages = run_mypy(addons, check=["t_use"]).messages("t_use/models.py")

    assert messages == [
        '8: error: Extra key "nope_a" for TypedDict "t.annotated values"  [typeddict-unknown-key]',
        '11: error: Extra key "nope_c" for TypedDict "t.const values"  [typeddict-unknown-key]',
    ]


SUPER_CREATE = """
from typing import Any, cast
from odoo import api, fields, models

class Mine(models.Model):
    _name = "t.mine"
    title = fields.Char()

    def _prepare(self, vals: Any) -> "odoo.values.t_mine":
        return {"title": "x"}

    @api.model_create_multi
    def create(self, vals_list):
        prepared = [self._prepare(vals) for vals in vals_list]
        return super().create(prepared)

class Other(models.Model):
    _name = "t.other"
    title = fields.Char()

    def _prepare(self, vals: Any) -> "odoo.values.t_other":
        return {"title": "x"}

    @api.model_create_multi
    def create(self, vals_list):
        prepared = [self._prepare(vals) for vals in vals_list]
        return super().create(cast("list[dict[str, Any]]", prepared))
"""


def test_super_create_keeps_odoos_declared_type(run_mypy: RunMypy, odoo_version: int) -> None:
    # mypy calls no plugin hook for super() calls: a list of values given to Odoo 18+'s
    # create(vals_list: list[ValuesType]) is arg-type (list is invariant); a cast passes.
    messages = _messages(run_mypy, SUPER_CREATE)

    expected = (
        []
        if odoo_version < 18
        else [
            '15: error: Argument 1 to "create" of "BaseModel" has incompatible type '
            '"list[t.mine values]"'
        ]
    )
    assert [line.split("; expected")[0] for line in messages if "error" in line] == expected


TYPO = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def f(self) -> "odoo.values.t_ordr":
        return {}
"""


def test_unreadable_file_of_another_addon_is_skipped(run_mypy: RunMypy, tmp_path: Path) -> None:
    # An unknown alias makes the plugin search every addon's sources for definers.
    broken = tmp_path / "addons" / "t_broken"
    broken.mkdir(parents=True)
    (broken / "__manifest__.py").write_text("{'name': 't_broken', 'depends': ['base']}\n")
    (broken / "models.py").symlink_to(tmp_path / "missing.py")

    messages = _messages(run_mypy, TYPO)

    assert messages == [
        '7: error: Unknown Odoo model in "odoo.values.t_ordr"; did you mean "t_order"?'
        "  [odoo-model]"
    ]


LIMITS = """
from odoo import models

def takes_values(vals: "odoo.values.t_order") -> None: ...

class Order(models.Model):
    _inherit = "t.order"

    def use(self) -> None:
        vals: "odoo.values.t_order" = {}
        vals["x_studio_note"] = "db"
        vals["state"] = "nope"
        vals["partner_id"] = self.env["t.line"]
        reveal_type({**vals}["name"])
        plain = {"name": "x"}
        takes_values(plain)
        takes_values({"name": "x"})
"""


def test_values_annotation_documented_limits(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, LIMITS)

    assert messages == [
        # A field only the database has: one key set for every addon (README).
        '11: error: TypedDict "t.order values" has no key "x_studio_note"  [typeddict-unknown-key]',
        # Selection keys and comodels are checked in literal create() / write() vals.
        # A copy is a dict[str, object], whose values read as Any (mixed values).
        '14: note: Revealed type is "Any"',
        '16: error: Argument 1 to "takes_values" has incompatible type "dict[str, str]"; '
        'expected "t.order values"  [arg-type]',
    ]
