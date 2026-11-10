"""Linearization of model classes as ``BaseModel._build_model`` builds them (no mypy run).

Each addon is a view: a class sees the classes of its own addon's dependency closure,
the classes of its model registered before it, and every class of its ``_inherit``
parents in that closure (Odoo extends a parent's registry class in place).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from test_registry_semantics import _build, _get, _names

from odoo_typing.registry import Registry
from odoo_typing.scanner import Contribution

BASE_MODEL = 'class Base(models.AbstractModel):\n    _name = "base"\n'
BASE = {"__init__.py": "from . import m\n", "m.py": BASE_MODEL}


def _files(source: str) -> dict[str, str]:
    return {"__init__.py": "from . import m\n", "m.py": source}


def _cls(name: str, model: str = "", inherit: Sequence[str] = (), body: str = "") -> str:
    lines = [f"class {name}(models.Model):"]
    if model:
        lines.append(f'    _name = "{model}"')
    if inherit:
        lines.append(f"    _inherit = {list(inherit)!r}")
    return "\n".join(lines) + "\n" + body + "\n"


def _lin(registry: Registry, fullname: str) -> list[str]:
    return _names(registry.linearization(_get(registry, fullname)))


def _short(contributions: Sequence[Contribution]) -> list[str]:
    return [f"{item.addon}.{item.class_name}" for item in contributions]


def test_linearization_extension_lists_model_chain_then_mixins_then_base(
    tmp_path: Path,
) -> None:
    registry = _build(
        tmp_path,
        base=BASE,
        addons={
            "t_core": (
                [],
                _files(
                    _cls("Mixin", "t.mixin")
                    + _cls("Order", "t.order", ["t.mixin"])
                    + _cls("Ext", inherit=["t.order"])
                ),
            ),
        },
    )

    assert _lin(registry, "t_core.m.Ext") == [
        "t_core.m.Ext",
        "t_core.m.Order",
        "t_core.m.Mixin",
        "odoo.addons.base.m.Base",
    ]


def test_linearization_includes_later_extension_of_parent_in_view(tmp_path: Path) -> None:
    # [1] project_sms: sms extends mail.thread after project defined project.task
    registry = _build(
        tmp_path,
        base=BASE,
        addons={
            "t_mail": ([], _files(_cls("Thread", "t.thread"))),
            "t_project": (["t_mail"], _files(_cls("Task", "t.task", ["t.thread"]))),
            "t_sms": (["t_mail"], _files(_cls("Thread", inherit=["t.thread"]))),
            "t_project_sms": (
                ["t_project", "t_sms"],
                _files(_cls("Task", inherit=["t.task"])),
            ),
        },
    )

    assert _short(registry.linearization(_get(registry, "t_project_sms.m.Task"))) == [
        "t_project_sms.Task",
        "t_project.Task",
        "t_sms.Thread",
        "t_mail.Thread",
        "base.Base",
    ]
    # project itself cannot see sms
    assert _short(registry.linearization(_get(registry, "t_project.m.Task"))) == [
        "t_project.Task",
        "t_mail.Thread",
        "base.Base",
    ]


def test_linearization_diamond_with_mixins_in_different_orders_is_one_chain(
    tmp_path: Path,
) -> None:
    # [25] website lists (res, mail), sale (mail, res); Odoo's LastOrderedSet keeps the
    # order of the latest class (t_web), so the merged class has one consistent chain.
    core = _cls("Company", "t.company") + _cls("Res", "t.res") + _cls("Mail", "t.mail")
    registry = _build(
        tmp_path,
        base=BASE,
        addons={
            "t_core": ([], _files(core)),
            "t_web": (["t_core"], _files(_cls("C", "t.company", ["t.company", "t.res", "t.mail"]))),
            "t_sale": (
                ["t_core"],
                _files(_cls("C", "t.company", ["t.company", "t.mail", "t.res"])),
            ),
            "t_ws": (["t_web", "t_sale"], _files(_cls("C", inherit=["t.company"]))),
        },
    )

    assert _short(registry.linearization(_get(registry, "t_ws.m.C"))) == [
        "t_ws.C",
        "t_web.C",
        "t_sale.C",
        "t_core.Company",
        "t_core.Res",
        "t_core.Mail",
        "base.Base",
    ]
    assert _short(registry.model_tips("t.company", "t_ws")) == ["t_ws.C"]


def test_view_class_single_complete_tip_is_that_class(tmp_path: Path) -> None:
    registry = _build(
        tmp_path,
        base=BASE,
        addons={
            "t_core": ([], _files(_cls("Partner", "t.partner"))),
            "t_ext": (["t_core"], _files(_cls("Partner", inherit=["t.partner"]))),
        },
    )

    view = registry.view_class("t.partner", "t_ext")

    assert view is not None
    assert view.tip == _get(registry, "t_ext.m.Partner")
    assert registry.view_class("t.unknown", "t_ext") is None


def test_view_class_unrelated_extensions_need_a_merged_class(tmp_path: Path) -> None:
    registry = _build(
        tmp_path,
        base=BASE,
        addons={
            "t_core": ([], _files(_cls("Partner", "t.partner"))),
            "t_x": (["t_core"], _files(_cls("Partner", inherit=["t.partner"]))),
            "t_y": (["t_core"], _files(_cls("Partner", inherit=["t.partner"]))),
            "t_top": (["t_x", "t_y"], _files("")),
        },
    )

    view = registry.view_class("t.partner", "t_top")

    assert view is not None
    assert view.tip is None
    assert _short(view.chain) == ["t_y.Partner", "t_x.Partner", "t_core.Partner", "base.Base"]
    assert _short(view.bases) == ["t_y.Partner", "t_x.Partner", "base.Base"]


def test_view_class_mixin_extended_outside_tips_view_needs_a_merged_class(
    tmp_path: Path,
) -> None:
    registry = _build(
        tmp_path,
        base=BASE,
        addons={
            "t_mail": ([], _files(_cls("Thread", "t.thread"))),
            "t_project": (["t_mail"], _files(_cls("Task", "t.task", ["t.thread"]))),
            "t_sms": (["t_mail"], _files(_cls("Thread", inherit=["t.thread"]))),
            "t_user": (["t_project", "t_sms"], _files("")),
        },
    )

    view = registry.view_class("t.task", "t_user")
    project_view = registry.view_class("t.task", "t_project")

    assert view is not None and project_view is not None
    assert view.tip is None
    assert _short(view.chain) == ["t_project.Task", "t_sms.Thread", "t_mail.Thread", "base.Base"]
    assert project_view.tip == _get(registry, "t_project.m.Task")


def test_bases_are_tips_of_each_parent_in_the_view(tmp_path: Path) -> None:
    registry = _build(
        tmp_path,
        base=BASE,
        addons={
            "t_mail": ([], _files(_cls("Thread", "t.thread"))),
            "t_project": (["t_mail"], _files(_cls("Task", "t.task", ["t.thread"]))),
            "t_sms": (["t_mail"], _files(_cls("Thread", inherit=["t.thread"]))),
            "t_ps": (["t_project", "t_sms"], _files(_cls("Task", inherit=["t.task"]))),
        },
    )

    assert _short(registry.bases(_get(registry, "t_ps.m.Task"))) == [
        "t_project.Task",
        "t_sms.Thread",
        "base.Base",
    ]


def test_dependency_modules_cover_tips_of_every_model_in_the_chain(tmp_path: Path) -> None:
    registry = _build(
        tmp_path,
        base=BASE,
        addons={
            "t_mail": ([], _files(_cls("Thread", "t.thread"))),
            "t_project": (["t_mail"], _files(_cls("Task", "t.task", ["t.thread"]))),
            "t_sms": (["t_mail"], _files(_cls("Thread", inherit=["t.thread"]))),
            "t_user": (["t_project", "t_sms"], _files("")),
        },
    )

    assert registry.view_modules("t.task", "t_user") == frozenset(
        {"t_project.m", "t_sms.m", "odoo.addons.base.m"}
    )


def test_model_cycle_drops_cycle_edges_in_every_query_order(tmp_path: Path) -> None:
    source = _cls("A", "x.a", ["x.b"]) + _cls("B", "x.b", ["x.a"]) + _cls("A2", inherit=["x.a"])
    registry = _build(tmp_path, base=BASE, addons={"t_mod": ([], _files(source))})

    assert _lin(registry, "t_mod.m.A2") == ["t_mod.m.A2", "t_mod.m.A", "odoo.addons.base.m.Base"]
    assert _lin(registry, "t_mod.m.B") == ["t_mod.m.B", "odoo.addons.base.m.Base"]
    assert _names(registry.inherit_cycle(_get(registry, "t_mod.m.A2"))) == [
        "t_mod.m.A",
        "t_mod.m.B",
    ]
