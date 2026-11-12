"""Unit tests of the field index: ``Registry.fields`` merges definitions like Odoo (no mypy)."""

from __future__ import annotations

from pathlib import Path

from test_registry_mro import BASE, _cls, _files
from test_registry_semantics import _build

from odoo_typing.model_fields import merge_keys
from odoo_typing.registry import Registry
from odoo_typing.scanner import FieldInfo

SERVICE = ("id", "display_name", "create_uid", "create_date", "write_uid", "write_date")

CORE = (
    _cls(
        "Order",
        "t.order",
        body=(
            '    partner_id = fields.Many2one("res.partner", required=True)\n'
            "    code = fields.Char(required=True)\n"
            "    image = fields.Binary(store=False)\n"
            '    state = fields.Selection([("draft", "Draft"), ("done", "Done")])\n'
            "    note = fields.Text()\n"
            "    def _compute_x(self):\n        pass\n"
        ),
    )
    + _cls("Mixin", "t.mixin", body="    mixed = fields.Integer()\n").replace(
        "models.Model", "models.AbstractModel"
    )
    + _cls("Log", "t.log", body="    seen = fields.Datetime()\n")
    + _cls("View", "t.view", inherit=["t.log"], body="    _auto = False\n")
    + _cls("Uses", "t.uses", inherit=["t.mixin"], body="    _log_access = False\n")
)
EXTENSION = _cls(
    "Order",
    inherit=["t.order"],
    body=(
        "    partner_id = fields.Many2one(readonly=True)\n"
        "    code = fields.Text()\n"
        "    image = fields.Image()\n"
        '    state = fields.Selection(selection_add=[("sent", "Sent")])\n'
        "    def note(self):\n        pass\n"
    ),
)
LATER = _cls(
    "Order",
    inherit=["t.order"],
    body='    state = fields.Selection(selection_add=[("cancel", "Cancel"), ("done",)])\n',
)


def _registry(tmp_path: Path) -> Registry:
    return _build(
        tmp_path,
        base=BASE,
        addons={
            "t_core": ([], _files(CORE)),
            "t_ext": (["t_core"], _files(EXTENSION)),
            "t_later": (["t_ext"], _files(LATER)),
        },
    )


def test_fields_redefinition_overrides_only_its_arguments(tmp_path: Path) -> None:
    fields = _registry(tmp_path).fields("t.order", "t_ext")

    assert fields["partner_id"] == FieldInfo(
        "partner_id", "Many2one", comodel="res.partner", readonly=True, required=True
    )


def test_fields_redefinition_with_other_class_drops_earlier_arguments(tmp_path: Path) -> None:
    fields = _registry(tmp_path).fields("t.order", "t_ext")

    assert fields["code"] == FieldInfo("code", "Text")
    assert fields["image"] == FieldInfo("image", "Image", store=False)  # Image is a Binary


def test_fields_selection_add_extends_per_view(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    assert registry.fields("t.order", "t_core")["state"].selection == ("draft", "done")
    assert registry.fields("t.order", "t_ext")["state"].selection == ("draft", "done", "sent")
    # As Odoo's merge_sequences: "done" in the selection_add anchors "cancel" before it.
    assert registry.fields("t.order", "t_later")["state"].selection == (
        "draft",
        "cancel",
        "done",
        "sent",
    )


def test_fields_method_of_later_class_hides_the_field(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    assert "note" in registry.fields("t.order", "t_core")
    assert "note" not in registry.fields("t.order", "t_ext")


def test_fields_service_fields_follow_log_access(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    order = registry.fields("t.order", "t_core")
    uses = registry.fields("t.uses", "t_core")
    assert list(order)[: len(SERVICE)] == list(SERVICE)
    assert order["create_uid"] == FieldInfo("create_uid", "Many2one", "res.users", readonly=True)
    assert list(uses) == ["id", "display_name", "mixed"]  # mixin fields, no log access


def test_fields_log_access_fields_come_from_inherited_model_class(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    # Like res.device (_auto = False) _inherit res.device.log: the fields of t.log's class.
    assert set(SERVICE) <= set(registry.fields("t.view", "t_core"))
    assert "create_date" not in registry.fields("t.uses", "t_core")


def test_merge_keys_follows_each_sequence_order() -> None:
    assert merge_keys(["A", "B", "C"], ["Z"], ["Y", "C"], ["A", "X", "Y"]) == (
        "A",
        "B",
        "X",
        "Y",
        "C",
        "Z",
    )
    assert merge_keys(["a", "b"], ["c", "b", "d"]) == ("a", "c", "b", "d")


def test_fields_unknown_model_is_empty_and_methods_are_merged(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    assert registry.fields("t.nothing", "t_core") == {}
    assert registry.methods("t.order", "t_ext") == {"_compute_x", "note"}
    assert registry.methods("t.order", "t_core") == {"_compute_x"}


DELEGATION = (
    _cls("Grand", "t.grand", body="    age = fields.Integer()\n    label = fields.Char()\n")
    + _cls(
        "Parent",
        "t.parent",
        body=(
            '    _inherits = {"t.grand": "grand_id"}\n'
            '    grand_id = fields.Many2one("t.grand")\n'
            '    kind = fields.Selection([("a", "A")], readonly=True)\n'
        ),
    )
    + _cls(
        "Child",
        "t.child",
        body=(
            '    parent_id = fields.Many2one("t.parent", delegate=True)\n'
            "    label = fields.Text()\n"
        ),
    )
    + _cls(
        "Loop",
        "t.loop",
        body='    _inherits = {"t.loop2": "other_id"}\n    own = fields.Char()\n',
    )
    + _cls("Loop2", "t.loop2", body='    _inherits = {"t.loop": "back_id"}\n')
)


def test_fields_delegated_fields_are_related_and_transitive(tmp_path: Path) -> None:
    registry = _build(tmp_path, base=BASE, addons={"t_core": ([], _files(DELEGATION))})

    fields = registry.fields("t.child", "t_core")

    assert fields["kind"] == FieldInfo(
        "kind", "Selection", selection=("a",), related="parent_id.kind", readonly=True
    )
    assert fields["age"] == FieldInfo("age", "Integer", related="parent_id.age")
    assert fields["grand_id"].related == "parent_id.grand_id"
    assert fields["label"] == FieldInfo("label", "Text")  # its own field wins
    assert fields["id"] == FieldInfo("id", "Id")  # not delegated


def test_fields_delegation_cycle_terminates(tmp_path: Path) -> None:
    registry = _build(tmp_path, base=BASE, addons={"t_core": ([], _files(DELEGATION))})

    assert registry.fields("t.loop2", "t_core")["own"].related == "back_id.own"


def test_concrete_models_but_not_mixins_or_unknown(tmp_path: Path) -> None:
    source = _cls("Order", "t.order") + (
        'class Mixin(models.AbstractModel):\n    _name = "t.mixin"\n'
    )
    registry = _build(tmp_path, base=BASE, addons={"t_core": ([], _files(source))})

    assert registry.concrete("t.order", "t_core") is True
    assert registry.concrete("t.mixin", "t_core") is False
    assert registry.concrete("t.nothing", "t_core") is False
