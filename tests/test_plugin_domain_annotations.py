"""End-to-end tests of ``odoo.domain.*`` annotations and of typed domains (roadmap stage 8).

The annotation names the model of a domain (``-> "odoo.domain.sale_order"``); its
literals are checked where they are written (``return``, annotated variables and
parameters) and its model where it goes (``search()``, parameters, ``any``, ``domain=``).
"""

from __future__ import annotations

import pytest
from conftest import RunMypy

T_BASE = """
from typing import Annotated, Any
from odoo import fields, models

class Mixin(models.AbstractModel):
    _name = "t.mixin"

    flag = fields.Boolean()

    def _filter(self, domain: "odoo.domain.t_mixin") -> None:
        pass

class Order(models.Model):
    _name = "t.order"
    _inherit = ["t.mixin"]

    name = fields.Char()
    state = fields.Selection([("draft", "Draft"), ("done", "Done")])
    partner_id = fields.Many2one("res.partner")
    line_ids = fields.One2many("t.line", "order_id")

    def _order_domain(self) -> "odoo.domain.t_order":
        return []

    def _lines(self, domain: "odoo.domain.t_line") -> None:
        pass

class Line(models.Model):
    _name = "t.line"

    order_id = fields.Many2one("t.order")
    label = fields.Text()

    def _line_domain(self) -> Annotated[list[tuple[str, str, Any]], "odoo.domain.t_line"]:
        return [("label", "=", "x")]

class Child(models.Model):
    _name = "t.child"
    _inherits = {"t.order": "order_id"}

    order_id = fields.Many2one("t.order", required=True, ondelete="cascade")
    extra = fields.Char()
"""


def _addons(**extra: dict[str, object]) -> dict[str, dict[str, object]]:
    return {"t_base": {"models.py": T_BASE}, **extra}


def _messages(run_mypy: RunMypy, source: str, **files: str) -> list[str]:
    result = run_mypy(
        _addons(t_use={"depends": ["t_base"], "models.py": source, **files}), check=["t_use"]
    )
    return result.messages("t_use/models.py")


def _errors(run_mypy: RunMypy, source: str) -> list[str]:
    """The plugin's errors, without the suggestions."""
    return [
        line.split("; did you mean")[0].split("  [")[0] + "  [" + line.rpartition("  [")[2]
        for line in _messages(run_mypy, source)
        if "[odoo-" in line
    ]


FORMS = """
from typing import Annotated, Any
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def f(
        self,
        bare: "odoo.domain.t_order",
        items: Annotated[list[str | tuple[str, str, Any]], "odoo.domain.t_order"],
        optional: "odoo.domain.t_order | None",
        nested: "list[odoo.domain.t_order]",
    ) -> None:
        reveal_type(bare)
        reveal_type(items)
        reveal_type(optional)
        reveal_type(nested)
        reveal_type(self._order_domain())
        self.search(bare)
        self.search(items)
        items.append(("name", "=", "x"))
"""


def test_annotation_forms_have_the_domain_types(run_mypy: RunMypy, odoo_version: int) -> None:
    marker = "t_use.models.<domain of t_order>"  # messages name it "t.order domain"
    domain = "odoo.orm.domains.Domain | " if odoo_version >= 19 else ""
    expected = [
        f'15: note: Revealed type is "{domain}list[Any] | {marker}"',
        f'16: note: Revealed type is "list[Any] | {marker}"',
        f'17: note: Revealed type is "{domain}list[Any] | {marker} | None"',
        f'18: note: Revealed type is "list[{domain}list[Any] | {marker}]"',
        f'19: note: Revealed type is "{domain}list[Any]"',  # mypy simplifies a call's type
    ]

    assert _messages(run_mypy, FORMS) == expected


DOMAIN_FORMS = """
from typing import Annotated
from odoo import models
from odoo.fields import Domain
from odoo.orm.types import DomainType

class Order(models.Model):
    _inherit = "t.order"

    def f(
        self,
        domain: Annotated[Domain, "odoo.domain.t_order"],
        typed: Annotated[DomainType, "odoo.domain.t_order"],
        maybe: Annotated[DomainType | None, "odoo.domain.t_order"],
    ) -> None:
        reveal_type(domain)
        reveal_type(typed)
        reveal_type(maybe)
        self.search(domain & Domain("name", "=", "x"))
        self.search(~domain)
        if isinstance(typed, list):
            reveal_type(typed)
        else:
            reveal_type(typed)
"""


@pytest.mark.odoo(19, 20)
def test_domain_class_forms(run_mypy: RunMypy) -> None:
    marker = "t_use.models.<domain of t_order>"
    result = run_mypy(
        _addons(t_use={"depends": ["t_base"], "models.py": DOMAIN_FORMS}),
        check=["t_use"],
        flags=["--warn-unreachable"],
    )

    assert result.messages("t_use/models.py") == [
        f'16: note: Revealed type is "odoo.orm.domains.Domain | {marker}"',
        f'17: note: Revealed type is "odoo.orm.domains.Domain | list[Any] | {marker}"',
        f'18: note: Revealed type is "odoo.orm.domains.Domain | list[Any] | None | {marker}"',
        '22: note: Revealed type is "list[Any]"',
        '24: note: Revealed type is "odoo.orm.domains.Domain"',
    ]


ERRORS = """
from typing import Annotated
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def a(self) -> "odoo.domain.t_ordr":
        return [("anything", "=", 1)]

    def b(self) -> "odoo.domain.t_thing":
        return []

    def c(self, domain: Annotated[dict, "odoo.domain.t_order"]) -> None:
        pass
"""


def test_annotation_errors(run_mypy: RunMypy, odoo_version: int) -> None:
    needs = "a list or Domain type" if odoo_version >= 19 else "a list type"
    addons = _addons(
        t_other={"models.py": THING}, t_use={"depends": ["t_base"], "models.py": ERRORS}
    )

    result = run_mypy(addons, check=["t_use"])

    assert result.messages("t_use/models.py") == [
        '8: error: Unknown Odoo model in "odoo.domain.t_ordr"; did you mean "t_order"?'
        "  [odoo-model]",
        '11: error: Odoo model "t.thing" ("odoo.domain.t_thing") is defined in addon '
        '"t_other", which is not in the dependencies of "t_use"  [odoo-model-depends]',
        f'14: error: "odoo.domain.t_order" needs {needs} in Annotated[...]  [valid-type]',
    ]


RETURNS = """
from typing import Annotated, Any
from odoo import models

def nested_helper() -> "odoo.domain.t_order":
    def inner():
        return [("not_checked", "=", 1)]
    return [("helper_nme", "=", 1)]

class Order(models.Model):
    _inherit = "t.order"

    def a(self, flag: bool) -> "odoo.domain.t_order":
        domain = [("stat", "=", "draft")]
        domain += [("nme", "=", 1)]
        domain.append(("prtner_id", "=", 1))
        if flag:
            return domain
        return []

    def b(self) -> "odoo.domain.t_order | None":
        return [("opt_nme", "=", 1)] if self else None

    async def c(self) -> "odoo.domain.t_order":
        return [("async_nme", "=", 1)]

    def d(self) -> Annotated[list[str | tuple[str, str, Any]], "odoo.domain.t_order"]:
        return [("name", "=", "x"), ("typed_nme", "=", 1)]

    def e(self) -> "odoo.domain.t_order":
        return ["|", ("name", "=", "x")]

    def f(self) -> "odoo.domain.t_order":
        return ["|", ("name", "=", "x")] + [("name", "=", "y")]

    def g(self) -> "odoo.domain.t_order":
        return self.env["t.line"]._line_domain()

    def h(self) -> "odoo.domain.t_mixin":
        return self._order_domain()
"""


def test_returns_of_annotated_functions_checked(run_mypy: RunMypy) -> None:
    assert _errors(run_mypy, RETURNS) == [
        '8: error: Odoo model "t.order" has no field "helper_nme" in domain  [odoo-domain]',
        '14: error: Odoo model "t.order" has no field "stat" in domain  [odoo-domain]',
        '15: error: Odoo model "t.order" has no field "nme" in domain  [odoo-domain]',
        '16: error: Odoo model "t.order" has no field "prtner_id" in domain  [odoo-domain]',
        '22: error: Odoo model "t.order" has no field "opt_nme" in domain  [odoo-domain]',
        '25: error: Odoo model "t.order" has no field "async_nme" in domain  [odoo-domain]',
        '28: error: Odoo model "t.order" has no field "typed_nme" in domain  [odoo-domain]',
        '31: error: Domain operator "|" needs 2 terms, has 1  [odoo-domain]',
        '37: error: Domain of Odoo model "t.line" returned as a domain of Odoo model "t.order"'
        "  [odoo-domain-model]",
        '40: error: Domain of Odoo model "t.order" returned as a domain of Odoo model "t.mixin"'
        "  [odoo-domain-model]",
    ]


VARIABLES = """
from dataclasses import dataclass, field
from typing import Annotated
from odoo import models

ABOVE: "odoo.domain.t_order" = [("above_nme", "=", 1)]
ANNOTATED: Annotated[list, "odoo.domain.t_order"] = [("annotated_nme", "=", 1)]

class Order(models.Model):
    _inherit = "t.order"

    _DEFAULT: "odoo.domain.t_order" = [("class_nme", "=", 1)]

    def f(self, domain: "odoo.domain.t_order") -> None:
        local: Annotated[list, "odoo.domain.t_order"] = [("local_nme", "=", 1)]
        local.append(("appended_nme", "=", 1))
        built: Annotated[list, "odoo.domain.t_order"] = ["|"]
        built += [("name", "=", 1), ("name", "=", 2)]
        domain = domain + [("param_nme", "=", 1)]
        self._lines(domain)
        self.search(domain)
        self.search(local)
        self.search(built)
        wrong: "odoo.domain.t_order" = self.env["t.line"]._line_domain()

BELOW: "odoo.domain.t_order" = [("below_nme", "=", 1)]

@dataclass
class Options:
    domain: "odoo.domain.t_order" = field(default_factory=list)
    other: "odoo.domain.t_order" = field(default=None)  # type: ignore[assignment]
"""


def test_annotated_variables_checked(run_mypy: RunMypy) -> None:
    assert _errors(run_mypy, VARIABLES) == [
        '6: error: Odoo model "t.order" has no field "above_nme" in domain  [odoo-domain]',
        '7: error: Odoo model "t.order" has no field "annotated_nme" in domain  [odoo-domain]',
        '12: error: Odoo model "t.order" has no field "class_nme" in domain  [odoo-domain]',
        '15: error: Odoo model "t.order" has no field "local_nme" in domain  [odoo-domain]',
        '16: error: Odoo model "t.order" has no field "appended_nme" in domain  [odoo-domain]',
        '19: error: Odoo model "t.order" has no field "param_nme" in domain  [odoo-domain]',
        '20: error: Domain of Odoo model "t.order" given to parameter "domain" of "_lines", which '
        'takes a domain of Odoo model "t.line"  [odoo-domain-model]',
        '24: error: Domain of Odoo model "t.line" assigned to a domain of Odoo model "t.order"'
        "  [odoo-domain-model]",
        '26: error: Odoo model "t.order" has no field "below_nme" in domain  [odoo-domain]',
    ]


CONSUMERS = """
from odoo import fields, models

class Order(models.Model):
    _inherit = "t.order"

    # the lambda's self is the model's records: its call is a typed domain of t.order
    other_ids = fields.One2many("t.line", "order_id", domain=lambda self: self._order_domain())
    lambda_ids = fields.One2many("t.line", "order_id", domain=lambda self: [("lable", "=", 1)])

    def f(self) -> None:
        order = self._order_domain()
        line = self.env["t.line"]._line_domain()
        self.env["t.line"].search(order)
        self.env["t.line"].search_count(self._order_domain())
        self.env["t.line"].name_search("x", self._order_domain())
        self.env["t.line"]._name_search("x", domain=self._order_domain())
        self._lines(order)
        self._lines([("labl", "=", 1)])
        self.search([("line_ids", "any", order)])
        self.search([("line_ids", "any", line), ("line_ids", "any", [("labl", "=", 1)])])
        self.search(line)
        self.filtered_domain(line)
"""


def test_consumers_check_the_model(run_mypy: RunMypy, odoo_version: int) -> None:
    def given(method: str) -> str:
        return (
            f'Domain of Odoo model "t.order" given to {method}() of Odoo model "t.line"'
            "  [odoo-domain-model]"
        )

    expected = [
        '8: error: Domain of Odoo model "t.order" given to domain= of a field with comodel '
        '"t.line"  [odoo-domain-model]',
        '9: error: Odoo model "t.line" has no field "lable" in domain  [odoo-domain]',
        f"14: error: {given('search')}",
        f"15: error: {given('search_count')}",
        f"16: error: {given('name_search')}",
        f"17: error: {given('_name_search')}",  # Odoo 17 only
        '18: error: Domain of Odoo model "t.order" given to parameter "domain" of "_lines", which '
        'takes a domain of Odoo model "t.line"  [odoo-domain-model]',
        '19: error: Odoo model "t.line" has no field "labl" in domain  [odoo-domain]',
        '20: error: Domain of Odoo model "t.order" in "any" of field "line_ids" of Odoo model '
        '"t.order", which takes a domain of "t.line"  [odoo-domain-model]',
        '21: error: Odoo model "t.line" has no field "labl" in domain  [odoo-domain]',
        '22: error: Domain of Odoo model "t.line" given to search() of Odoo model "t.order"'
        "  [odoo-domain-model]",
        '23: error: Domain of Odoo model "t.line" given to filtered_domain() of Odoo model '
        '"t.order"  [odoo-domain-model]',
    ]
    if odoo_version > 17:  # _name_search is gone
        expected.remove(f"17: error: {given('_name_search')}")

    assert _errors(run_mypy, CONSUMERS) == expected


COMPATIBLE = """
from odoo import models

class Child(models.Model):
    _inherit = "t.child"

    def f(self) -> "odoo.domain.t_mixin":
        self.search(self.env["t.order"]._order_domain())
        self.env["t.order"].search(self._child_domain())
        self.env["t.order"]._filter([("name", "=", "x"), ("nam", "=", "x")])
        self.env["t.line"]._filter([("label", "=", "x")])
        self.env["t.order"]._filter(self.env["t.line"]._line_domain())
        return [("flag", "=", True)]

    def _child_domain(self) -> "odoo.domain.t_child":
        return [("extra", "=", "x"), ("name", "=", "x")]
"""


def test_parents_mixins_and_delegates_are_accepted(run_mypy: RunMypy) -> None:
    assert _errors(run_mypy, COMPATIBLE) == [
        '9: error: Domain of Odoo model "t.child" given to search() of Odoo model "t.order"'
        "  [odoo-domain-model]",
        '10: error: Odoo model "t.order" has no field "nam" in domain  [odoo-domain]',
        '12: error: Domain of Odoo model "t.line" given to parameter "domain" of "_filter", '
        'which takes a domain of Odoo model "t.order"  [odoo-domain-model]',
    ]


AGREEMENT = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def f(self, flag: bool, given: "odoo.model.t_order") -> None:
        if flag:
            domain = self._order_domain()
            self.search(domain)
            return
        domain = self.env["t.line"]._line_domain()
        self.env["t.line"].search(domain)
        self.env["t.line"].search(domain)
        same = self._order_domain() if flag else self._order_domain()
        self.env["t.line"].search(same)

    def g(self, domain, flag: bool) -> None:
        if flag:
            domain = self._order_domain()
        self.env["t.line"].search(domain)

    def h(self, flag: bool) -> None:
        both = self._order_domain()
        if flag:
            both = self.env["t.line"]._line_domain()
        self.search(both)

    def i(self) -> None:
        for _ in range(2):
            loop = self._order_domain()
            self.env["t.line"].search(loop)

    def j(self) -> None:
        later = self._order_domain()
        for _ in range(2):
            self.env["t.line"].search(later)
            later = self.env["t.line"]._line_domain()

    def k(self) -> None:
        reused = self._order_domain()
        self.search(reused)
        reused = self.env["t.line"]._line_domain()
        self.env["t.line"].search(reused)
"""


def test_model_of_a_variable_when_its_bindings_agree(run_mypy: RunMypy) -> None:
    # Only a binding to a typed domain (a call, a cast, an annotated name) has a model: a
    # conditional (line 16) has none. A parameter, a binding in another branch and one
    # later in a loop make the model unknown; a binding after the line does not count.
    assert _errors(run_mypy, AGREEMENT) == [
        '32: error: Domain of Odoo model "t.order" given to search() of Odoo model "t.line"'
        "  [odoo-domain-model]",
    ]


SEARCH_METHODS = """
from odoo import fields, models

class Order(models.Model):
    _inherit = "t.order"

    total = fields.Float(compute="_compute_total", search="_search_total")
    shown = fields.Float(compute="_compute_total", search="_search_shown")
    hidden = fields.Float(compute="_compute_total")

    def _compute_total(self):
        pass

    def _search_total(self, operator, value):
        return [("nme", operator, value), ("hidden", "=", 1), ("line_ids", "any!", [])]

    def _search_shown(self, operator, value) -> "odoo.domain.t_order":
        return [("hidden", "=", 1), ("shwn", "=", 1)]

    def _search_other(self, operator, value):
        return [("not_checked", "=", 1)]
"""


def test_search_methods_return_domains_of_their_model(run_mypy: RunMypy, odoo_version: int) -> None:
    # The search rules apply (the search runs the returned domain), and "any!" is allowed
    # there (Odoo reads it as an internal domain); an annotation names the model.
    unsearchable = 'Field "hidden" of Odoo model "t.order" cannot be searched'
    errors = [line.split(" (not stored")[0] for line in _errors(run_mypy, SEARCH_METHODS)]
    expected = [
        '15: error: Odoo model "t.order" has no field "nme" in domain  [odoo-domain]',
        f"15: error: {unsearchable}",
        f"18: error: {unsearchable}",
        '18: error: Odoo model "t.order" has no field "shwn" in domain  [odoo-domain]',
    ]
    if odoo_version < 19:
        expected.insert(2, '15: error: Unknown domain operator "any!"  [odoo-domain]')

    assert errors == expected


MIXIN_SEARCH = """
from odoo import fields, models

class Mixin(models.AbstractModel):
    _inherit = "t.mixin"

    computed = fields.Char(compute="_compute", search="_search_computed")

    def _compute(self):
        pass

    def _search_computed(self, operator, value):
        return [("anything", operator, value)]
"""


def test_search_methods_of_mixins_are_not_checked(run_mypy: RunMypy) -> None:
    assert _errors(run_mypy, MIXIN_SEARCH) == []


SUPER = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def _lines(self, domain: "odoo.domain.t_line") -> None:
        super()._lines([("not_checked", "=", 1)])
"""


def test_super_calls_are_not_checked(run_mypy: RunMypy) -> None:
    # mypy calls no plugin hook for a call through super().
    assert _errors(run_mypy, SUPER) == []


ARG_TYPE = """
from odoo import models
from odoo.fields import Domain
from odoo.orm.types import DomainType

class Order(models.Model):
    _inherit = "t.order"

    def f(self, value: int) -> None:
        domain = [("state", "=", "draft")]
        self.search(domain)
        lines = [("label", "=", "x")]
        self.env["t.line"].search(lines)
        self.search([(1, "=", 1), Domain("name", "=", "x"), (value, "=", 1)])
        nested = [("name", "=", "x")]
        self.search([nested, None, [("a", "=", 1)]])
        self.env["t.mixin"].search([nested])
        self.custom(domain)

    def custom(self, domain: DomainType) -> None:
        pass
"""
OUTSIDE = """
from odoo.fields import Domain
from odoo.models import BaseModel

def f(records: BaseModel) -> None:
    records.search([(1, "=", 1)])
"""


@pytest.mark.odoo(19, 20)
def test_domain_type_takes_lists_in_addons(run_mypy: RunMypy) -> None:
    addons = _addons(t_use={"depends": ["t_base"], "models.py": ARG_TYPE})

    result = run_mypy(addons, check=["t_use"], scripts={"tool.py": OUTSIDE})

    assert result.messages("t_use/models.py") == [
        '16: error: Domain item of type "list[tuple[str, str, str]]" is not a term or an '
        "operator  [odoo-domain]",
        '16: error: Domain item of type "None" is not a term or an operator  [odoo-domain]',
        '16: error: Domain item of type "list" is not a term or an operator  [odoo-domain]',
        '17: error: Domain item of type "list[tuple[str, str, str]]" is not a term or an '
        "operator  [odoo-domain]",
    ]
    assert [line for line in result.messages("tool.py") if "[list-item]" in line]


CACHED_HELPER = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def f(self) -> None:
        self.env["t.line"].search(self._order_domain())
"""


def test_cold_warm_and_parallel_runs_report_the_same(run_mypy: RunMypy) -> None:
    addons = _addons(
        t_use={"depends": ["t_base"], "models.py": RETURNS + CACHED_HELPER.split("\n", 2)[2]}
    )
    addons["t_use"]["helpers.py"] = VARIABLES
    addons["t_use"]["more.py"] = CACHED_HELPER

    cold = run_mypy(addons, check=["t_use"], fresh_cache=True)
    warm = run_mypy(addons, check=["t_use"], fresh_cache=True)
    parallel = run_mypy(addons, check=["t_use"], flags=["--num-workers", "2"])

    assert [line for line in cold.lines if "[odoo-" in line]
    assert warm.lines == cold.lines
    assert sorted(parallel.lines) == sorted(cold.lines)


def test_a_cached_helper_keeps_its_model(run_mypy: RunMypy) -> None:
    # Only the consumer's module changes: the helper's module comes from the cache.
    first = run_mypy(
        _addons(t_use={"depends": ["t_base"], "models.py": CACHED_HELPER}),
        check=["t_use"],
        fresh_cache=True,
    )
    changed = CACHED_HELPER + "\n        self.env['t.line'].search(self._order_domain())\n"
    second = run_mypy(
        _addons(t_use={"depends": ["t_base"], "models.py": changed}),
        check=["t_use"],
        fresh_cache=True,
    )

    given = 'Domain of Odoo model "t.order" given to search() of Odoo model "t.line"'
    assert [line for line in first.messages("t_use/models.py") if given in line]
    assert len([line for line in second.messages("t_use/models.py") if given in line]) == 2


UNRESOLVED = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def f(self) -> "odoo.domain.t_thing":
        return []
"""
THING = "from odoo import models\n\nclass Thing(models.Model):\n    _name = 't.thing'\n"


def test_alias_of_a_model_outside_the_closure_follows_new_addons(run_mypy: RunMypy) -> None:
    addons = _addons(t_use={"depends": ["t_base"], "models.py": UNRESOLVED})
    first = run_mypy(addons, check=["t_use"], fresh_cache=True)
    addons["t_other"] = {"models.py": THING}
    second = run_mypy(addons, check=["t_use"], fresh_cache=True)

    assert first.messages("t_use/models.py") == [
        '7: error: Unknown Odoo model in "odoo.domain.t_thing"  [odoo-model]'
    ]
    assert second.messages("t_use/models.py") == [
        '7: error: Odoo model "t.thing" ("odoo.domain.t_thing") is defined in addon "t_other", '
        'which is not in the dependencies of "t_use"  [odoo-model-depends]'
    ]


COLUMNS = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def wrapped(self) -> "odoo.domain.t_order":
        return [("name", "=", self.nope)]

    def plain(self) -> list:
        return [("name", "=", self.nope)]
"""


def test_columns_of_mypy_errors_in_a_wrapped_expression(run_mypy: RunMypy) -> None:
    addons = _addons(t_use={"depends": ["t_base"], "models.py": COLUMNS})

    result = run_mypy(addons, check=["t_use"], flags=["--show-column-numbers"])

    assert result.messages("t_use/models.py") == [
        '8:31: error: "Order" has no attribute "nope"  [attr-defined]',
        '11:31: error: "Order" has no attribute "nope"  [attr-defined]',
    ]


BASES = """
from t_base.models import Line, Order

class Both(Order, Line):
    _name = "t.both"

    def f(self) -> None:
        reveal_type(self.name)
"""


def test_classes_with_two_model_bases(run_mypy: RunMypy) -> None:
    # A module with model classes has a hidden attribute with a digest of their fields
    # (for the daemon): not a class member, so classes with two model bases are fine.
    messages = _messages(run_mypy, BASES)

    assert [line for line in messages if "error" in line or "odoo_fields" in line] == []


RULE_CLASSES = """
from odoo import fields, models

class Order(models.Model):
    _inherit = "t.order"

    total = fields.Float(compute="_compute_total")

    def _compute_total(self):
        pass

    def helper(self) -> "odoo.domain.t_order":
        return [("total", "=", 1), ("name", "child_of", 1)]

    def f(self) -> None:
        domain: "odoo.domain.t_order" = [("total", "=", 2)]
        self.search(domain)
        other: "odoo.domain.t_order" = [("total", "=", 3)]
        self.filtered_domain(other)
"""


def test_search_rules_apply_where_a_search_takes_the_literal(run_mypy: RunMypy) -> None:
    # An annotated helper may go to filtered_domain() too: only the rules that hold there.
    errors = [line.split(" (not stored")[0] for line in _errors(run_mypy, RULE_CLASSES)]

    assert errors == [
        '13: error: Operator "child_of" needs a relational field, not the Char field "name" '
        'of Odoo model "t.order"  [odoo-domain]',
        '16: error: Field "total" of Odoo model "t.order" cannot be searched',
    ]


# -- round 1 of the review: shapes that gave crashes or false errors ---------------------

SELF_REFERENCE = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def a(self, domain):
        domain = [
            ("line_ids", "any", domain),
        ]
        return self.search(domain)

    def b(self, domain):
        domain.append(
            ("line_ids", "any", domain),
        )
        return self.search(domain)

    def c(self, lines: "odoo.domain.t_line"):
        domain = [
            ("line_ids", "any", lines),
            ("line_ids", "any", [("order_id.line_ids", "any", [("labl", "=", 1)])]),
        ]
        return self.search(domain)
"""


def test_a_domain_that_holds_its_own_variable_is_checked_once(run_mypy: RunMypy) -> None:
    # The multi-line statement that binds the variable runs after it reads it: the
    # inner name is the earlier value (once a crash: endless any -> variable -> any).
    assert _errors(run_mypy, SELF_REFERENCE) == [
        '22: error: Odoo model "t.line" has no field "labl" in domain  [odoo-domain]',
    ]


SELF_REFERENCE_19 = """
from odoo import models
from odoo.fields import Domain

class Order(models.Model):
    _inherit = "t.order"

    def a(self, domain: "odoo.domain.t_line"):
        domain = Domain(
            "line_ids", "any", domain,
        )
        return self.search(domain)
"""


@pytest.mark.odoo(19, 20)
def test_a_domain_object_that_holds_its_own_variable_is_checked_once(run_mypy: RunMypy) -> None:
    # The parameter is a t.line domain: the value written to it is checked against
    # t.line, and it goes to a search of t.order. Each error once, no endless walk.
    assert _errors(run_mypy, SELF_REFERENCE_19) == [
        '10: error: Odoo model "t.line" has no field "line_ids" in domain  [odoo-domain]',
        '12: error: Domain of Odoo model "t.line" given to search() of Odoo model "t.order"'
        "  [odoo-domain-model]",
    ]


ROUND_1 = """
from typing import cast, Annotated
from odoo import fields, models

class Base(models.Model):
    _name = "t.base"

    name = fields.Char()
    date = fields.Date()
    pic = fields.Binary(compute="_compute_pic", search="_search_pic")
    line_ids = fields.One2many("t.line", "order_id")

    def _compute_pic(self) -> None:
        pass

    def _search_pic(self, operator, value):
        return [("name", operator, value)]

    def _count(self, domain: "odoo.domain.t_base") -> int:
        return self.search_count(domain)

    def inferred(self, flag: bool, pd: "odoo.domain.t_line", od: "odoo.domain.t_base") -> None:
        if flag:
            d = pd
            self.env["t.line"].search(d)
            return
        d = od
        self.search(d)

    def shapes(self, rest: tuple[str, int], names: list[str]) -> None:
        self.search([("pic", "ilike", "x"), ("date", "=?", 0), ("name", *rest)])
        d = []
        for n in names[1:]:
            d.append("|")
        for n in names:
            d.append(("name", "ilike", n))
        self.search(d)
        e: "odoo.domain.t_base" = ["|"]
        e = e + [("name", "=", "a"), ("name", "=", "b")]
        self.search(e)
        self.env["t.line"].search(cast(Annotated[list, "odoo.domain.t_base"], d))

class Child(models.Model):
    _name = "t.child2"
    _inherit = "t.base"

    extra = fields.Char()

    def run(self) -> None:
        self._count([("extra", "=", "x")])
"""


def test_round_1_shapes_without_false_errors(run_mypy: RunMypy) -> None:
    # A non-stored Binary with a search method, "=?" with 0, a starred term, "|" appended
    # in a loop, a rebinding that extends the variable, a variable bound from annotated
    # names in two branches, a method of a model the receiver inherits by prototype; a
    # cast in a call is a typed domain.
    assert _errors(run_mypy, ROUND_1) == [
        '41: error: Domain of Odoo model "t.base" given to search() of Odoo model "t.line"'
        "  [odoo-domain-model]",
    ]


INTERNAL_17 = """
from odoo import fields, models

class Order(models.Model):
    _inherit = "t.order"

    tag_names = fields.Char(compute="_compute_tag_names", search="_search_tag_names")

    def _compute_tag_names(self):
        pass

    def _search_tag_names(self, operator, value):
        return [("id", "inselect", ("SELECT id FROM t_order", []))]

    def f(self):
        self.search([("id", "inselect", ("SELECT id FROM t_order", []))])
"""


@pytest.mark.odoo(17)
def test_inselect_only_in_what_a_search_method_returns(run_mypy: RunMypy) -> None:
    # Odoo 17 reads a search method's result as an internal domain (is_leaf(internal=True)).
    assert _errors(run_mypy, INTERNAL_17) == [
        '16: error: Unknown domain operator "inselect"  [odoo-domain]',
    ]


INTERNAL_ANNOTATED = """
from odoo import fields, models

class Order(models.Model):
    _inherit = "t.order"

    shown = fields.Char(compute="_compute_shown", search="_search_shown")
    hidden = fields.Char(compute="_compute_shown")

    def _compute_shown(self):
        pass

    def _search_shown(self, operator, value) -> "odoo.domain.t_order":
        return [("line_ids", "any!", [("label", "=", value)]), ("hidden", "=", 1)]
"""


@pytest.mark.odoo(19, 20)
def test_annotated_search_methods_return_internal_domains(run_mypy: RunMypy) -> None:
    errors = [line.split(" (not stored")[0] for line in _errors(run_mypy, INTERNAL_ANNOTATED)]

    assert errors == ['14: error: Field "hidden" of Odoo model "t.order" cannot be searched']


# -- round 2 of the review: checks the round 1 fixes must keep ---------------------------

ROUND_2 = """
from odoo import fields, models
from odoo.osv import expression

class Base(models.Model):
    _name = "t.base"

    name = fields.Char()
    pic = fields.Binary()
    child_id = fields.Many2one("t.base")
    child_pic = fields.Binary(related="child_id.pic")

    def helper(self, d) -> None:
        pass

    def shapes(self, flag: bool, names: list[str]) -> None:
        self.search([("child_pic", "ilike", "x")])
        for n in names:
            d = ["|"]
            d.append(("name", "=", n))
            self.search(d)
        e = []
        if flag:
            e.append("|")
        else:
            e.append("&")
        e.append(("name", "=", "a"))
        e.append(("name", "=", "b"))
        self.search(e)
        f: "odoo.domain.t_base" = ["|"]
        f = [*f, ("name", "=", "a"), ("name", "=", "b")]
        self.search(f)
        g: "odoo.domain.t_base" = ["|", ("name", "=", "a")]
        g = expression.AND([g, [("name", "=", "b")]])
        self.helper(g)
        h = ["|"]
        if flag:
            h.append(("name", "=", "a"))
        self.search(h)

    def aliases(self) -> None:
        d0 = self.env["t.line"]._line_domain()
        d1 = d0
        self.search(d1)
        d3: "odoo.domain.t_line" = [("label", "=", "y")]
        d4 = d3
        d5 = d4
        self.search(d5)

class Del(models.Model):
    _name = "t.del"
    _inherits = {"t.base": "base_id"}

    base_id = fields.Many2one("t.base", required=True, ondelete="cascade")

    def f(self) -> None:
        self.search([("pic", "ilike", "x")])
"""


def test_round_2_checks_that_the_fixes_keep(run_mypy: RunMypy) -> None:
    # A related and a delegated Binary are searched as their target; a variable built in
    # one loop iteration or one branch is one list, one built in two exclusive branches is
    # not; a rebinding that reads a variable as a whole domain (AND) keeps its value a
    # whole domain, one that spreads it (*f) does not; chains of variables keep the model.
    binary = 'Binary field "{}" of Odoo model "{}" takes only existence checks in a search'
    errors = [line.split(' ("=" /')[0] for line in _errors(run_mypy, ROUND_2)]

    assert errors == [
        f"17: error: {binary.format('child_pic', 't.base')}",
        '19: error: Domain operator "|" needs 2 terms, has 1  [odoo-domain]',
        '33: error: Domain operator "|" needs 2 terms, has 1  [odoo-domain]',
        '36: error: Domain operator "|" needs 2 terms, has 1  [odoo-domain]',
        '44: error: Domain of Odoo model "t.line" given to search() of Odoo model "t.base"'
        "  [odoo-domain-model]",
        '48: error: Domain of Odoo model "t.line" given to search() of Odoo model "t.base"'
        "  [odoo-domain-model]",
        f"57: error: {binary.format('pic', 't.del')}",
    ]


SUB_DOMAINS = """
from odoo import fields, models

class Order(models.Model):
    _inherit = "t.order"

    total = fields.Char(compute="_compute_total", search="_search_total")

    def _compute_total(self):
        pass

    def _search_total(self, operator, value):
        return [("line_ids", "any", [("id", "inselect", ("SELECT 1", []))])]
"""


@pytest.mark.odoo(17)
def test_sub_domains_of_search_methods_are_not_internal_on_odoo_17(run_mypy: RunMypy) -> None:
    # Odoo 17 parses an "any" sub-domain as a plain domain: no "inselect" there.
    assert _errors(run_mypy, SUB_DOMAINS) == [
        '13: error: Unknown domain operator "inselect"  [odoo-domain]',
    ]


MIXIN_SEARCH_ANNOTATED = """
from odoo import fields, models

class Mixin(models.AbstractModel):
    _inherit = "t.mixin"

    computed = fields.Char(compute="_compute", search="_search_computed")

    def _compute(self):
        pass

    def _search_computed(self, operator, value) -> "odoo.domain.t_mixin":
        return [("flag", "=", True), ("flg", "=", True), MIXIN_TERM]

class Order(models.Model):
    _inherit = "t.order"

    computed = fields.Char(compute="_compute", search="_search_computed")

    def _compute(self):
        pass

    def _search_computed(self, operator, value) -> "odoo.domain.t_order":
        return [ORDER_TERM]
"""
# A term only an internal domain takes, per version (Odoo 18 has none: a plain term).
INTERNAL_TERMS = {
    17: ('("id", "inselect", ("SELECT 1", []))', '("id", "not inselect", ("SELECT 1", []))'),
    18: ('("flag", "=", False)', '("name", "=", "x")'),
    19: ('("id", "any!", [("flag", "=", True)])', '("line_ids", "any!", [("label", "=", 1)])'),
    20: ('("id", "any!", [("flag", "=", True)])', '("line_ids", "any!", [("label", "=", 1)])'),
}


def test_annotated_search_methods_of_mixins_are_search_methods(
    run_mypy: RunMypy, odoo_version: int
) -> None:
    # Checked against the annotation's model, as an internal domain: for a mixin's method
    # too (an unannotated one is not checked, its fields are those of the models using it).
    mixin, order = INTERNAL_TERMS[odoo_version]
    source = MIXIN_SEARCH_ANNOTATED.replace("MIXIN_TERM", mixin).replace("ORDER_TERM", order)

    assert _errors(run_mypy, source) == [
        '13: error: Odoo model "t.mixin" has no field "flg" in domain  [odoo-domain]',
    ]


def _nested_any(depth: int) -> str:
    """A literal domain of t.order with a typo ``depth`` ``any`` sub-domains deep."""
    domain = '[("labl", "=", 1)]'
    for level in reversed(range(depth)):
        path = "line_ids" if level % 2 == 0 else "order_id"  # t.order -> t.line -> t.order
        domain = f'[("{path}", "any", {domain})]'
    return domain


DEPTH = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def f(self) -> None:
        self.search({shallow})
        self.search({deep})
"""


def test_sub_domains_are_checked_to_a_bounded_depth(run_mypy: RunMypy) -> None:
    # The bound of the walk (MAX_SUBDOMAIN_DEPTH = 5): a typo 2 levels deep is reported,
    # one 7 levels deep is not (a missed check, never a false error).
    source = DEPTH.format(shallow=_nested_any(2), deep=_nested_any(7))

    assert _errors(run_mypy, source) == [
        '8: error: Odoo model "t.order" has no field "labl" in domain  [odoo-domain]',
    ]
