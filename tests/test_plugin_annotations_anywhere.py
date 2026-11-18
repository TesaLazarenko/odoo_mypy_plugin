"""``odoo.values.*`` / ``odoo.model.*`` / ``odoo.domain.*`` outside the classes of models.

A bare string is a type wherever mypy analyzes one in an addon's file
(:mod:`odoo_typing.alias_names`): a module of plain functions, a class-syntax
``NamedTuple`` or ``TypedDict``, ``cast(...)``, a type alias, a class in a function.
"""

from __future__ import annotations

from conftest import RunMypy
from test_plugin_annotations import _addons

FUNCTIONS = """
from typing import cast

def plain(order: "odoo.model.t_order") -> "odoo.values.t_order":
    reveal_type(order)
    local: "odoo.values.t_line" = {"qtty": 1}
    return {"nmae": order.name}

def dom(a: "odoo.domain.t_order", b: "odoo.values.t_order") -> "odoo.domain.t_order":
    d: "odoo.domain.t_line" = [("qtty", "=", 1)]
    return [("nmae", "=", 1)]

x = cast("odoo.values.t_order", {})
x["nmae"] = 1
y: "odoo.domain.t_order" = [("cuont", "=", 1)]

def late() -> None:
    def inner() -> "odoo.domain.t_line":
        return [("qtty", "=", 2)]
    dom([("nmae", "=", 3)], {"nmae": 1})
"""
FUNCTIONS_MESSAGES = [
    '5: note: Revealed type is "t_base.models.Order"',
    '6: error: Extra key "qtty" for TypedDict "t.line values"  [typeddict-unknown-key]',
    '7: error: Extra key "nmae" for TypedDict "t.order values"  [typeddict-unknown-key]',
    '10: error: Odoo model "t.line" has no field "qtty" in domain; did you mean "qty"?'
    "  [odoo-domain]",
    '11: error: Odoo model "t.order" has no field "nmae" in domain; did you mean "name"?'
    "  [odoo-domain]",
    '14: error: TypedDict "t.order values" has no key "nmae"  [typeddict-unknown-key]',
    '15: error: Odoo model "t.order" has no field "cuont" in domain; did you mean "count"?'
    "  [odoo-domain]",
    '19: error: Odoo model "t.line" has no field "qtty" in domain; did you mean "qty"?'
    "  [odoo-domain]",
    '20: error: Odoo model "t.order" has no field "nmae" in domain; did you mean "name"?'
    "  [odoo-domain]",
    '20: error: Extra key "nmae" for TypedDict "t.order values"  [typeddict-unknown-key]',
]
# The first annotation mypy reaches is inside a function's body.
BODIES = """
def first() -> None:
    d: "odoo.domain.t_line" = [("qtty", "=", 1)]
    class Local:
        v: "odoo.values.t_line"
    Local().v["qtty"] = 1

def second() -> "odoo.domain.t_line":
    return [("qtty", "=", 2)]
"""
BODIES_MESSAGES = [
    '3: error: Odoo model "t.line" has no field "qtty" in domain; did you mean "qty"?'
    "  [odoo-domain]",
    '6: error: TypedDict "t.line values" has no key "qtty"  [typeddict-unknown-key]',
    '6: note: Did you mean "qty"?',
    '9: error: Odoo model "t.line" has no field "qtty" in domain; did you mean "qty"?'
    "  [odoo-domain]",
]
# A module that binds the real ``odoo`` package, with classes that are no regular ones.
TUPLES = """
import odoo
from typing import NamedTuple, TypedDict, TypeAlias

class Row(NamedTuple):
    order: "odoo.model.t_order"
    values: "odoo.values.t_order"

class Packet(TypedDict):
    order: "odoo.model.t_order"
    values: "odoo.values.t_order"

V: TypeAlias = "odoo.values.t_order"
D: TypeAlias = "odoo.domain.t_order"

def use(r: Row, p: Packet, v: V, d: D) -> None:
    reveal_type(r.order)
    r.values["nmae"] = 1
    reveal_type(p["order"])
    p["values"]["nmae"] = 1
    v["nmae"] = 2
    bad: "odoo.values.t_ordr" = {}
    reveal_type(d)
    reveal_type(odoo.release)
"""
TUPLES_MESSAGES = [
    '17: note: Revealed type is "t_base.models.Order"',
    '18: error: TypedDict "t.order values" has no key "nmae"  [typeddict-unknown-key]',
    '19: note: Revealed type is "t_base.models.Order"',
    '20: error: TypedDict "t.order values" has no key "nmae"  [typeddict-unknown-key]',
    '21: error: TypedDict "t.order values" has no key "nmae"  [typeddict-unknown-key]',
    '22: error: Unknown Odoo model in "odoo.values.t_ordr"; did you mean "t_order"?  [odoo-model]',
    '23: note: Revealed type is "list[Any] | t_use.tools.tuples.<domain of t_order>"',
    '24: note: Revealed type is "types.ModuleType"',
]
SCRIPT = """
bare: "odoo.values.t_order" = {}
odoo.tools.misc()
"""


def _tools(**modules: str) -> dict[str, dict[str, object]]:
    names = ", ".join(modules)
    spec: dict[str, object] = {
        "depends": ["t_base"],
        "models.py": "from . import tools\n",
        "tools/__init__.py": f"from . import {names}\n",
    }
    spec.update({f"tools/{name}.py": source for name, source in modules.items()})
    return _addons(t_use=spec)


def test_bare_strings_in_modules_without_a_regular_class(run_mypy: RunMypy) -> None:
    addons = _tools(functions=FUNCTIONS, bodies=BODIES, tuples=TUPLES)
    expected = {"functions": FUNCTIONS_MESSAGES, "bodies": BODIES_MESSAGES}

    # Cold, then from the cache: the names are stored with ``builtins`` and ``odoo``.
    for _ in range(2):
        result = run_mypy(addons, check=["t_use"], fresh_cache=True)

        for name, messages in expected.items():
            assert result.messages(f"t_use/tools/{name}.py") == messages


def test_bare_strings_in_named_tuples_typed_dicts_and_aliases(
    run_mypy: RunMypy, odoo_version: int
) -> None:
    result = run_mypy(_tools(tuples=TUPLES), check=["t_use"])

    messages = result.messages("t_use/tools/tuples.py")
    if odoo_version >= 19:
        assert "Domain" in messages[6]
        del messages[6]
        assert messages == TUPLES_MESSAGES[:6] + TUPLES_MESSAGES[7:]
    else:
        assert messages == TUPLES_MESSAGES


def test_a_module_checked_again_from_the_cache_of_its_dependencies(run_mypy: RunMypy) -> None:
    """Only the module of plain functions is analyzed: no class asks the plugin first."""
    addons = _tools(functions=FUNCTIONS)
    run_mypy(addons, check=["t_use"], fresh_cache=True)
    changed = FUNCTIONS + "\nz = 1\n"

    result = run_mypy(_tools(functions=changed), check=["t_use"], fresh_cache=True)

    assert result.messages("t_use/tools/functions.py") == FUNCTIONS_MESSAGES


def test_outside_the_addons_the_name_means_nothing(run_mypy: RunMypy) -> None:
    result = run_mypy(_addons(), check=["t_base"], scripts={"tool.py": SCRIPT})

    assert result.messages("tool.py") == [
        '2: error: Name "odoo" is not defined  [name-defined]',
        # ``odoo`` is a class of builtins without members: a missing import is still reported.
        '3: error: "type[odoo]" has no attribute "tools"  [attr-defined]',
    ]
