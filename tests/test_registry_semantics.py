"""Registry semantics compared with the Odoo 18 runtime (MetaModel, _build_model, graph).

Each test builds small addons on disk and asks the registry directly (no mypy run).
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from test_registry import _addon, _odoo

from odoo_typing.config import Settings
from odoo_typing.registry import Registry
from odoo_typing.scanner import Contribution

Files = Mapping[str, str | bytes]
PARTNER = 'class Partner(models.Model):\n    _name = "t.partner"\n'


def _build(
    root: Path, addons: Mapping[str, tuple[Sequence[str], Files]], base: Files | None = None
) -> Registry:
    """Registry over ``base`` (empty by default) plus addons as ``name: (depends, files)``."""
    odoo = _odoo(root)
    for relative, source in (base or {}).items():
        path = odoo / "addons" / "base" / relative
        if isinstance(source, bytes):
            path.write_bytes(source)
        else:
            path.write_text(source)
    (root / "addons").mkdir(exist_ok=True)
    for name, (depends, files) in addons.items():
        _addon(root / "addons", name, list(depends), files)
    return Registry(Settings(odoo_path=odoo, addons_paths=(root / "addons",)))


def _single(root: Path, files: Files, depends: Sequence[str] = ()) -> Registry:
    return _build(root, {"t_mod": (depends, files)})


def _names(contributions: Sequence[Contribution]) -> list[str]:
    return [item.fullname for item in contributions]


def _get(registry: Registry, fullname: str) -> Contribution:
    contribution = registry.contribution(fullname)
    assert contribution is not None, fullname
    return contribution


def _extension(class_name: str, field: str = "") -> str:
    return f'class {class_name}(models.Model):\n    _inherit = "t.partner"\n{field}'


# -- [4][17] execution order: imports and classes inside try/if, post-order of imports ------


def test_scan_imports_inside_try_and_if_follows_them(tmp_path: Path) -> None:
    init = (
        "from . import base\n"
        "try:\n    from . import optional\nexcept ImportError:\n    pass\n"
        "if True:\n    from . import wizard\n"
    )
    files = {
        "__init__.py": init,
        "base.py": PARTNER,
        "optional.py": 'class Opt(models.Model):\n    _name = "t.optional"\n',
        "wizard.py": 'class Wiz(models.TransientModel):\n    _name = "t.wiz"\n',
    }
    registry = _single(tmp_path, files)

    assert _names(registry.model_tips("t.optional", "t_mod")) == ["t_mod.optional.Opt"]
    assert _names(registry.model_tips("t.wiz", "t_mod")) == ["t_mod.wizard.Wiz"]


def test_scan_type_checking_import_is_not_followed(tmp_path: Path) -> None:
    init = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from . import hint\n"
    files = {"__init__.py": init, "hint.py": 'class H(models.Model):\n    _name = "t.hint"\n'}
    registry = _single(tmp_path, files)

    assert registry.model_tips("t.hint", "t_mod") == ()


def test_scan_imported_module_classes_come_before_importer_classes(tmp_path: Path) -> None:
    # Python creates b's classes while executing a.py's import, before PartnerA.
    files = {
        "__init__.py": "from . import base, models\n",
        "base.py": PARTNER,
        "models/__init__.py": "from . import a\n",
        "models/a.py": "from .b import HELPER\n\n" + _extension("PartnerA"),
        "models/b.py": "HELPER = 1\n\n" + _extension("PartnerB"),
    }
    registry = _single(tmp_path, files)

    assert _names(registry.model_tips("t.partner", "t_mod")) == ["t_mod.models.a.PartnerA"]
    assert _names(registry.bases(_get(registry, "t_mod.models.a.PartnerA"))) == [
        "t_mod.models.b.PartnerB"
    ]


def test_scan_class_in_init_after_import_is_registered_last(tmp_path: Path) -> None:
    files = {
        "__init__.py": "from . import base\n\n" + _extension("InInit"),
        "base.py": PARTNER,
    }
    registry = _single(tmp_path, files)

    assert _names(registry.model_tips("t.partner", "t_mod")) == ["t_mod.InInit"]


def test_scan_absolute_self_import_is_followed(tmp_path: Path) -> None:
    init = "from odoo.addons.t_mod import base\nimport t_mod.models.ext\n"
    files = {
        "__init__.py": init,
        "base.py": PARTNER,
        "models/__init__.py": "",
        "models/ext.py": _extension("Ext"),
    }
    registry = _single(tmp_path, files)

    assert _names(registry.model_tips("t.partner", "t_mod")) == ["t_mod.models.ext.Ext"]


def test_scan_from_package_import_runs_intermediate_packages(tmp_path: Path) -> None:
    files = {
        "__init__.py": "from .models.sub import leaf\n",
        "models/__init__.py": "from . import base\n",
        "models/base.py": PARTNER,
        "models/sub/__init__.py": _extension("InSub"),
        "models/sub/leaf.py": _extension("Leaf"),
    }
    registry = _single(tmp_path, files)

    tips = registry.model_tips("t.partner", "t_mod")

    assert _names(tips) == ["t_mod.models.sub.leaf.Leaf"]
    assert _names(registry.bases(tips[0])) == ["t_mod.models.sub.InSub"]
    in_sub = _get(registry, "t_mod.models.sub.InSub")
    assert _names(registry.bases(in_sub)) == ["t_mod.models.base.Partner"]


def test_scan_classes_inside_module_level_try_and_if_are_registered(tmp_path: Path) -> None:
    source = (
        'try:\n    class Tr(models.Model):\n        _name = "x.tr"\nexcept ImportError:\n    pass\n'
        'if True:\n    class Cond(models.Model):\n        _name = "x.cond"\n'
        'def make():\n    class Local(models.Model):\n        _name = "x.local"\n'
    )
    registry = _single(tmp_path, {"__init__.py": "from . import m\n", "m.py": source})

    assert _names(registry.model_tips("x.tr", "t_mod")) == ["t_mod.m.Tr"]
    assert _names(registry.model_tips("x.cond", "t_mod")) == ["t_mod.m.Cond"]
    assert registry.model_tips("x.local", "t_mod") == ()


# -- [5][29] _name / _inherit evaluated like MetaModel ---------------------------------------

NAMES = """
PREFIX = "t"
CONST = "t.const"

class Mixin(models.AbstractModel):
    _name = "t.mixin"

class Chained(models.Model):
    _name = _description = "t.chained"

class FromConst(models.Model):
    _name = CONST
    _inherit = ["t.mixin"]

class FromFString(models.Model):
    _name = f"{PREFIX}.fstring"
    _inherit = ["t.mixin"]

class Multi(models.Model):
    _inherit = ["t.partner", "t.mixin"]

class Computed(models.Model):
    _name = compute_name()
    _inherit = ["t.mixin"]

class SelfRef(models.Model):
    _name = "t.partner"
    _inherit = _name

class SelfRefList(models.Model):
    _name = "t.partner"
    _inherit = [_name, "t.mixin"]
"""


@pytest.fixture
def names_registry(tmp_path: Path) -> Registry:
    files = {"__init__.py": "from . import base, m\n", "base.py": PARTNER, "m.py": NAMES}
    return _single(tmp_path, files)


@pytest.mark.parametrize(
    ("class_name", "model", "parents"),
    [
        ("Chained", "t.chained", ()),
        ("FromConst", "t.const", ("t.mixin",)),
        ("FromFString", "t.fstring", ("t.mixin",)),
        ("Multi", "Multi", ("t.partner", "t.mixin")),
        ("SelfRef", "t.partner", ("t.partner",)),
        ("SelfRefList", "t.partner", ("t.partner", "t.mixin")),
    ],
)
def test_scan_model_name_matches_metamodel(
    names_registry: Registry, class_name: str, model: str, parents: tuple[str, ...]
) -> None:
    contribution = _get(names_registry, f"t_mod.m.{class_name}")

    assert (contribution.model, contribution.parents) == (model, parents)


def test_scan_non_literal_name_is_skipped_not_an_extension(names_registry: Registry) -> None:
    assert names_registry.contribution("t_mod.m.Computed") is None
    assert _names(names_registry.model_tips("t.mixin", "t_mod")) == ["t_mod.m.Mixin"]


def test_bases_inherit_name_reference_extends_earlier_class(names_registry: Registry) -> None:
    self_ref = _get(names_registry, "t_mod.m.SelfRef")
    self_ref_list = _get(names_registry, "t_mod.m.SelfRefList")

    assert _names(names_registry.bases(self_ref)) == ["t_mod.base.Partner"]
    assert _names(names_registry.bases(self_ref_list)) == ["t_mod.m.SelfRef", "t_mod.m.Mixin"]


# -- [44] _register ---------------------------------------------------------------------------


def test_scan_register_false_class_is_not_a_contribution(tmp_path: Path) -> None:
    source = (
        'class Unregistered(models.Model):\n    _register = False\n    _name = "my.unregistered"\n'
        'class Registered(models.Model):\n    _register = True\n    _name = "my.registered"\n'
    )
    registry = _single(tmp_path, {"__init__.py": "from . import m\n", "m.py": source})

    assert registry.contribution("t_mod.m.Unregistered") is None
    assert registry.model_tips("my.unregistered", "t_mod") == ()
    assert _names(registry.model_tips("my.registered", "t_mod")) == ["t_mod.m.Registered"]


# -- [6][35] load order is Odoo 18's graph order: base, then (depth, name) -------------------


def test_load_key_orders_by_depth_then_name(tmp_path: Path) -> None:
    registry = _build(
        tmp_path,
        {
            "y_root": (["base"], {"__init__.py": ""}),
            "b_mid": (["y_root"], {"__init__.py": ""}),
            "b_deep": (["b_mid"], {"__init__.py": ""}),
            "z_shallow": (["y_root"], {"__init__.py": ""}),
            "a_nodeps": ([], {"__init__.py": ""}),
            "y_top": (["b_deep", "z_shallow", "not_on_disk"], {"__init__.py": ""}),
        },
    )

    order = sorted(registry.addons.addons, key=registry.addons.load_key)

    assert order == ["base", "a_nodeps", "y_root", "b_mid", "z_shallow", "b_deep", "y_top"]


def test_model_tips_unrelated_extensions_ordered_by_graph_depth(tmp_path: Path) -> None:
    ext = {"__init__.py": "from . import m\n", "m.py": _extension("P")}
    registry = _build(
        tmp_path,
        {
            "y_root": (["base"], {"__init__.py": "from . import m\n", "m.py": PARTNER}),
            "b_mid": (["y_root"], {"__init__.py": ""}),
            "b_deep": (["b_mid"], ext),
            "z_shallow": (["y_root"], ext),
            "y_top": (["b_deep", "z_shallow"], ext),
        },
    )

    top = registry.model_tips("t.partner", "y_top")[0]

    assert _names(registry.bases(top)) == ["b_deep.m.P", "z_shallow.m.P"]


# -- [8] addons symlinked under another name, __openerp__.py manifests -----------------------


def test_addon_for_path_symlink_with_other_name_finds_addon(tmp_path: Path) -> None:
    registry = _build(tmp_path, {})
    _addon(tmp_path / "src", "t_link-18.0", [], {"__init__.py": "", "models.py": ""})
    (tmp_path / "addons" / "t_link").symlink_to(tmp_path / "src" / "t_link-18.0")

    addon = registry.addons.addon_for_path(tmp_path / "addons" / "t_link" / "models.py")

    assert addon is not None
    assert (addon.name, addon.module_prefix) == ("t_link", "t_link")


def test_addon_index_openerp_manifest_is_an_addon(tmp_path: Path) -> None:
    registry = _build(tmp_path, {})
    old = tmp_path / "addons" / "t_old"
    old.mkdir()
    (old / "__openerp__.py").write_text("{'depends': ['base']}")
    (old / "__init__.py").write_text("from . import m\n")
    (old / "m.py").write_text(PARTNER)

    assert _names(registry.model_tips("t.partner", "t_old")) == ["t_old.m.Partner"]


# -- [42] dependencies missing from disk ------------------------------------------------------


def test_closure_dependency_not_on_disk_is_ignored(tmp_path: Path) -> None:
    files = {"__init__.py": "from . import m\n", "m.py": _extension("P")}
    registry = _build(
        tmp_path,
        {
            "t_base": ([], {"__init__.py": "from . import m\n", "m.py": PARTNER}),
            "t_mod": (["t_base", "not_on_disk"], files),
        },
    )

    assert registry.addons.closure("t_mod") == {"t_mod", "t_base", "base"}
    assert _names(registry.model_tips("t.partner", "t_mod")) == ["t_mod.m.P"]
    assert _names(registry.bases(_get(registry, "t_mod.m.P"))) == ["t_base.m.Partner"]


# -- [38][40] exact tip order and the `before` boundary ---------------------------------------


def test_bases_of_top_lists_most_recently_loaded_first(tmp_path: Path) -> None:
    # left's class sits on a later line than right's: only the addon order may decide.
    left = {"__init__.py": "from . import m\n", "m.py": "\n\n\n" + _extension("P")}
    right = {"__init__.py": "from . import m\n", "m.py": _extension("P")}
    registry = _build(
        tmp_path,
        {
            "t_base": ([], {"__init__.py": "from . import m\n", "m.py": PARTNER}),
            "left": (["t_base"], left),
            "right": (["t_base"], right),
            "top": (
                ["left", "right"],
                {"__init__.py": "from . import m\n", "m.py": _extension("P")},
            ),
        },
    )

    assert _names(registry.bases(_get(registry, "top.m.P"))) == ["right.m.P", "left.m.P"]
    assert _names(registry.model_tips("t.partner", "left", None)) == ["left.m.P"]


CHAIN = {
    "t_base": ([], {"__init__.py": "from . import m\n", "m.py": PARTNER}),
    "mid": (["t_base"], {"__init__.py": "from . import m\n", "m.py": _extension("P")}),
    "top": (["mid"], {"__init__.py": "from . import m\n", "m.py": _extension("P")}),
}


@pytest.mark.parametrize("first", ["mid", "top"])
def test_bases_linear_chain_any_query_order_gives_exact_chain(tmp_path: Path, first: str) -> None:
    registry = _build(tmp_path, CHAIN)
    registry.bases(_get(registry, f"{first}.m.P"))

    top = _get(registry, "top.m.P")

    assert _names(registry.bases(top)) == ["mid.m.P"]
    assert _names(registry.model_tips("t.partner", "top")) == ["top.m.P"]
    assert _names(registry.model_tips("t.partner", "top", before=top)) == ["mid.m.P"]


# -- [53][41] _inherit cycles: deterministic, reported, never memoized half-way --------------

CYCLE = """
class A1(models.Model):
    _name = "x.a"
    _inherit = ["x.b"]

class B1(models.Model):
    _name = "x.b"
    _inherit = ["x.a"]

class A2(models.Model):
    _inherit = "x.a"
"""

CYCLE_CLASSES = ["t_mod.m.A1", "t_mod.m.B1", "t_mod.m.A2"]


def _answers(registry: Registry) -> dict[str, object]:
    return {
        name: (
            _names(registry.bases(_get(registry, name))),
            _names(registry.inherit_cycle(_get(registry, name))),
        )
        for name in CYCLE_CLASSES
    } | {model: _names(registry.model_tips(model, "t_mod")) for model in ("x.a", "x.b")}


@pytest.mark.parametrize("order", list(itertools.permutations(CYCLE_CLASSES)))
def test_inherit_cycle_any_query_order_gives_same_answers(
    tmp_path: Path, order: tuple[str, ...]
) -> None:
    files = {"__init__.py": "from . import m\n", "m.py": CYCLE}
    reference = _answers(_single(tmp_path / "ref", files))
    registry = _single(tmp_path / "run", files)
    for name in order:
        registry.bases(_get(registry, name))

    assert _answers(registry) == reference


def test_inherit_cycle_drops_cycle_edges_and_reports_the_classes_that_make_it(
    tmp_path: Path,
) -> None:
    # A1 -> x.b and B1 -> x.a make the cycle; A2 just extends x.a. Odoo cannot build
    # either model; the edges inside the cycle are ignored, the rest stays.
    registry = _single(tmp_path, {"__init__.py": "from . import m\n", "m.py": CYCLE})
    members = [_get(registry, name) for name in CYCLE_CLASSES]

    assert [_names(registry.bases(member)) for member in members] == [[], [], ["t_mod.m.A1"]]
    assert [_names(registry.inherit_cycle(member)) for member in members] == [
        ["t_mod.m.A1", "t_mod.m.B1"]
    ] * 3
    assert _names(registry.model_tips("x.a", "t_mod")) == ["t_mod.m.A2"]


def test_inherit_mutual_mixins_do_not_recurse(tmp_path: Path) -> None:
    source = (
        'class A(models.Model):\n    _name = "my.a"\n    _inherit = ["my.b"]\n'
        'class B(models.Model):\n    _name = "my.b"\n    _inherit = ["my.a"]\n'
    )
    registry = _single(tmp_path, {"__init__.py": "from . import m\n", "m.py": source})

    assert _names(registry.model_tips("my.a", "t_mod")) == ["t_mod.m.A"]
    assert _names(registry.model_tips("my.b", "t_mod")) == ["t_mod.m.B"]
    assert _names(registry.inherit_cycle(_get(registry, "t_mod.m.A"))) == ["t_mod.m.A", "t_mod.m.B"]


# -- [7].1 implicit 'base' parent --------------------------------------------------------------


def test_bases_every_model_inherits_base_extensions_last(tmp_path: Path) -> None:
    base_ext = 'class Base(models.AbstractModel):\n    _inherit = "base"\n'
    base_model = 'class Base(models.AbstractModel):\n    _name = "base"\n'
    registry = _build(
        tmp_path,
        base={"__init__.py": "from . import m\n", "m.py": base_model},
        addons={
            "t_base": ([], {"__init__.py": "from . import m\n", "m.py": PARTNER}),
            "phone": ([], {"__init__.py": "from . import m\n", "m.py": base_ext}),
            "t_mod": (
                ["t_base", "phone"],
                {"__init__.py": "from . import m\n", "m.py": _extension("P")},
            ),
        },
    )

    assert _names(registry.linearization(_get(registry, "t_mod.m.P"))) == [
        "t_mod.m.P",
        "t_base.m.Partner",
        "phone.m.Base",
        "odoo.addons.base.m.Base",
    ]
    assert _names(registry.bases(_get(registry, "t_mod.m.P"))) == [
        "t_base.m.Partner",
        "phone.m.Base",
    ]
    assert _names(registry.bases(_get(registry, "phone.m.Base"))) == ["odoo.addons.base.m.Base"]
    assert registry.bases(_get(registry, "odoo.addons.base.m.Base")) == ()


# -- [2] _inherit of a model that is unknown, outside the depends, or defined later ----------

NODEP = """
class FromOtherAddon(models.Model):
    _inherit = "t.partner"

class Typo(models.Model):
    _inherit = "t.partnr"

class NewWithTypo(models.Model):
    _name = "t.new"
    _inherit = ["t.mixinn"]

class Early(models.Model):
    _name = "t.early"
    _inherit = ["t.late"]

class Late(models.AbstractModel):
    _name = "t.late"
"""


@pytest.fixture
def nodep_registry(tmp_path: Path) -> Registry:
    return _build(
        tmp_path,
        {
            "t_base": ([], {"__init__.py": "from . import m\n", "m.py": PARTNER}),
            "t_nodep": (
                ["base", "ee_missing"],
                {"__init__.py": "from . import m\n", "m.py": NODEP},
            ),
        },
    )


@pytest.mark.parametrize(
    ("class_name", "parent", "defined_later", "outside", "missing"),
    [
        ("FromOtherAddon", "t.partner", False, ("t_base",), ("ee_missing",)),
        ("Typo", "t.partnr", False, (), ("ee_missing",)),
        ("NewWithTypo", "t.mixinn", False, (), ("ee_missing",)),
        ("Early", "t.late", True, (), ("ee_missing",)),
    ],
)
def test_missing_parents_reports_why_parent_is_not_visible(
    nodep_registry: Registry,
    class_name: str,
    parent: str,
    defined_later: bool,
    outside: tuple[str, ...],
    missing: tuple[str, ...],
) -> None:
    problems = nodep_registry.missing_parents(_get(nodep_registry, f"t_nodep.m.{class_name}"))

    assert [
        (item.model, item.defined_later, item.outside_addons, item.missing_depends)
        for item in problems
    ] == [(parent, defined_later, outside, missing)]


def test_missing_parents_known_parents_report_nothing(nodep_registry: Registry) -> None:
    assert nodep_registry.missing_parents(_get(nodep_registry, "t_nodep.m.Late")) == ()
    assert nodep_registry.missing_parents(_get(nodep_registry, "t_base.m.Partner")) == ()


def test_model_tips_extension_of_unknown_model_does_not_define_it(nodep_registry: Registry) -> None:
    assert nodep_registry.model_tips("t.partnr", "t_nodep") == ()
    assert nodep_registry.model_tips("t.partner", "t_nodep") == ()
