"""Unit tests of what the registry knows about ``odoo.values.*`` / ``odoo.model.*`` (stage 7)."""

from __future__ import annotations

import ast
import os
import textwrap
from collections.abc import Mapping
from pathlib import Path

from pytest_mock import MockerFixture

from odoo_typing.config import Settings
from odoo_typing.records import RecordStore
from odoo_typing.registry import Registry
from odoo_typing.registry.annotations import UNCHECKED, AliasResolution, SearchMethod
from odoo_typing.scan_store import ScanStore
from odoo_typing.scanner import annotation_aliases


def _addon(root: Path, name: str, depends: list[str], files: Mapping[str, str]) -> None:
    path = root / name
    path.mkdir(parents=True)
    (path / "__manifest__.py").write_text(repr({"name": name, "depends": depends}))
    for relative, source in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(source))


BASE = {"__init__.py": "from . import models\n", "models.py": "from odoo import models\n"}
SHOP = {
    "__init__.py": "from . import models\n",
    "models.py": """
        from odoo import fields, models

        class Order(models.Model):
            _name = "shop.order"
            name = fields.Char()
            count = fields.Integer()
            widget = fields.Binary(compute="_compute_widget")

        class Line(models.Model):
            _name = "shop.order_line"
            order_id = fields.Many2one("shop.order")

        class Product(models.Model):
            _name = "shop.product"
            _inherits = {"shop.template": "template_id"}
            template_id = fields.Many2one("shop.template")
            code = fields.Char()

        class Template(models.Model):
            _name = "shop.template"
            list_price = fields.Float()
            code = fields.Integer()
    """,
}
EXTRA = {
    "__init__.py": "from . import models\n",
    "models.py": """
        from odoo import fields, models

        class Order(models.Model):
            _inherit = ["shop.order"]
            extra_flag = fields.Boolean()
            count = fields.Float()
    """,
}
USER = {
    "__init__.py": "from . import models\n",
    "models.py": """
        from typing import Annotated
        from odoo import models

        class Order(models.Model):
            _inherit = "shop.order"

            def _vals(self) -> Annotated[dict, "odoo.values.shop_order"]:
                return {}

            def _lines(self) -> "odoo.model.shop.order_line":
                return self.env["shop.order_line"]
    """,
    "tests/test_order.py": 'VALS: "odoo.values.shop_product" = {}\n',
}


def _registry(root: Path) -> Registry:
    odoo = root / "odoo"
    (odoo / "addons").mkdir(parents=True)
    (odoo / "release.py").write_text("")
    _addon(odoo / "addons", "base", [], BASE)
    _addon(root / "addons", "shop", ["base"], SHOP)
    _addon(root / "addons", "extra", ["shop"], EXTRA)  # not a dependency of user
    _addon(root / "addons", "user", ["shop"], USER)
    return Registry(Settings(odoo_path=odoo, addons_paths=(root / "addons",)))


def test_annotation_aliases_text_search_finds_both_kinds() -> None:
    source = b'x: "odoo.values.res_partner" = {}\ny: Annotated[dict, "odoo.values.sale.order"]\n'
    source += b'def f() -> "Mapping[str, odoo.model.res_partner]": ...\n'

    found = annotation_aliases(source)

    assert found == {("values", "res_partner"), ("values", "sale.order"), ("model", "res_partner")}


def test_annotation_aliases_text_search_finds_domains() -> None:
    source = (
        b'def f() -> "odoo.domain.sale_order": ...\nX: Annotated[list, "odoo.domain.res.partner"]\n'
    )

    assert annotation_aliases(source) == {("domain", "sale_order"), ("domain", "res.partner")}
    assert annotation_aliases(b'"odoo.domains.x" odoo.dom') == frozenset()


def test_annotation_models_leave_domains_out(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    path = tmp_path / "addons" / "user" / "domains.py"
    path.write_text('def f() -> "odoo.domain.shop_order_line": ...\n')

    # A domain's type needs no class of its model, so no dependency edge.
    assert registry.annotation_models(path) == frozenset()
    assert registry.annotation_aliases(path) == {("domain", "shop_order_line")}


SEARCH_METHODS = """
    from odoo import fields, models

    class Order(models.Model):
        _inherit = "shop.order"
        total = fields.Float(compute="_compute", search="_search_total")
        other = fields.Float(compute="_compute", search=lambda self, operator, value: [])

        def _compute(self):
            pass

        def _search_total(self, operator, value):
            return []

        def _search_display_name(self, operator, value):
            return []

    class Line(models.Model):
        _inherit = "shop.order_line"

        def _search_total(self, operator, value):
            return []
"""


def test_search_methods_of_a_module(tmp_path: Path) -> None:
    root = tmp_path
    odoo = root / "odoo"
    (odoo / "addons").mkdir(parents=True)
    (odoo / "release.py").write_text("")
    _addon(odoo / "addons", "base", [], BASE)
    _addon(root / "addons", "shop", ["base"], SHOP)
    files = {"__init__.py": "from . import models\n", "models.py": SEARCH_METHODS}
    _addon(root / "addons", "searcher", ["shop"], files)
    registry = Registry(Settings(odoo_path=odoo, addons_paths=(root / "addons",)))

    found = registry.search_methods("searcher.models")

    # Line's _search_total is no search method of its model's fields.
    method = SearchMethod("shop.order", concrete=True)
    assert found == {"Order": {"_search_total": method, "_search_display_name": method}}
    assert registry.model_classes("searcher.models") == {"Order", "Line"}


def test_annotation_aliases_of_scanned_and_unreached_files(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    user = tmp_path / "addons" / "user"

    scanned = registry.annotation_aliases(user / "models.py")
    unreached = registry.annotation_aliases(user / "tests" / "test_order.py")

    assert scanned == {("values", "shop_order"), ("model", "shop.order_line")}
    assert unreached == {("values", "shop_product")}


def test_annotation_models_are_dependency_edges(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    path = tmp_path / "addons" / "user" / "models.py"

    covered = registry.covered_models("user.models", path)

    assert {"shop.order", "shop.order_line"} <= covered


def test_resolve_alias_underscores_dots_and_errors(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    found = [
        registry.resolve_alias(alias, "user")
        for alias in ("shop_order", "shop.order", "shop_order_line", "nope")
    ]

    assert found == [
        AliasResolution("shop.order"),
        AliasResolution("shop.order"),
        AliasResolution("shop.order_line"),
        AliasResolution(None),
    ]


def test_resolve_alias_ambiguous_and_outside(tmp_path: Path) -> None:
    odoo = tmp_path / "odoo"
    (odoo / "addons").mkdir(parents=True)
    (odoo / "release.py").write_text("")
    _addon(odoo / "addons", "base", [], BASE)
    two = """
        from odoo import models
        class A(models.Model):
            _name = "t.a_b"
        class B(models.Model):
            _name = "t_a.b"
    """
    _addon(tmp_path / "addons", "two", ["base"], {"__init__.py": "from . import m\n", "m.py": two})
    other = 'from odoo import models\nclass C(models.Model):\n    _name = "other.thing"\n'
    _addon(
        tmp_path / "addons", "other", ["base"], {"__init__.py": "from . import m\n", "m.py": other}
    )
    registry = Registry(Settings(odoo_path=odoo, addons_paths=(tmp_path / "addons",)))

    ambiguous = registry.resolve_alias("t_a_b", "two")
    outside = registry.resolve_alias("other_thing", "two")

    assert ambiguous == AliasResolution(None, candidates=("t.a_b", "t_a.b"))
    assert outside == AliasResolution(None, outside_model="other.thing", outside=("other",))


def test_values_fields_come_from_every_addon(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    fields = registry.values_fields("shop.order")

    assert fields["name"] == {"Char"}
    assert fields["count"] == {"Integer", "Float"}  # redeclared by an addon user does not see
    assert fields["extra_flag"] == {"Boolean"}  # an addon outside user's dependencies
    assert fields["widget"] == {UNCHECKED}  # a computed binary that is not stored
    assert {"id", "display_name", "create_uid", "write_date"} <= set(fields)


def test_values_fields_delegated_fields_unless_own(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    fields = registry.values_fields("shop.product")

    assert fields["list_price"] == {"Float"}  # of _inherits shop.template
    assert fields["code"] == {"Char"}  # the model's own field wins


MIXINS = {
    "__init__.py": "from . import models\n",
    "models.py": """
        from odoo import fields, models

        class Avatar(models.AbstractModel):
            _name = "t.avatar"
            avatar_128 = fields.Image(compute="_compute_avatar")

        class Image(models.AbstractModel):
            _name = "t.image"
            _inherit = ["t.avatar"]
            image_1920 = fields.Image()
            note = fields.Char()

        class Doc(models.Model):
            _name = "shop.doc"
            _inherit = ["t.image"]
            note = fields.Integer()

        class Copy(models.Model):
            _name = "shop.copy"
            _inherit = "shop.doc"

        class Everything(models.AbstractModel):
            _inherit = "base"
            x_flag = fields.Boolean()
    """,
}


def test_values_fields_of_inherit_parents_and_base(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _addon(tmp_path / "addons", "mix", ["base"], MIXINS)

    doc = registry.values_fields("shop.doc")
    copy = registry.values_fields("shop.copy")

    assert doc["image_1920"] == {"Image"}  # of the mixin
    assert doc["avatar_128"] == {UNCHECKED}  # of the mixin's mixin
    assert doc["note"] == {"Integer"}  # the model's own field wins
    assert doc["x_flag"] == {"Boolean"}  # every model inherits base
    assert copy["note"] == {"Integer"} and "image_1920" in copy  # a copy of shop.doc


def test_values_fields_cover_the_fields_of_every_model_in_a_view(odoo_path: Path) -> None:
    registry = Registry(Settings(odoo_path=odoo_path, addons_paths=()))

    missing = {
        model: sorted(set(registry.fields(model, "sale")) - set(registry.values_fields(model)))
        for model in registry.models("sale")
    }

    assert {model: names for model, names in missing.items() if names} == {}
    assert {"image_1920", "avatar_128", "message_ids"} <= set(registry.values_fields("res.partner"))
    assert "access_token" in registry.values_fields("sale.order")  # of portal.mixin


def test_values_fields_skip_a_file_that_cannot_be_read(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    (tmp_path / "addons" / "shop" / "broken.py").symlink_to(tmp_path / "missing.py")

    fields = registry.values_fields("shop.order")

    assert "name" in fields


def test_values_fields_read_each_file_once_until_it_changes(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    _registry(tmp_path)
    _stored(tmp_path).values_fields("shop.order")
    parse = mocker.spy(ast, "parse")

    _stored(tmp_path).values_fields("shop.order")  # a new run: the stored classes of each file
    sources = [call for call in parse.call_args_list if "filename" in call.kwargs]
    path = tmp_path / "addons" / "extra" / "models.py"
    path.write_text(path.read_text().replace("extra_flag", "other_flag"))
    os.utime(path, ns=(10**18, 10**18))  # a new stamp even within the clock's resolution
    changed = _stored(tmp_path).values_fields("shop.order")

    assert sources == []  # manifests are parsed without a file name
    assert "other_flag" in changed and "extra_flag" not in changed


def test_values_digest_is_kept_until_a_file_of_its_models_changes(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    _registry(tmp_path)
    first = _stored(tmp_path).values_digest("shop_order")
    registry = _stored(tmp_path)
    fields = mocker.spy(registry, "values_fields")

    kept = registry.values_digest("shop_order")
    late = tmp_path / "addons" / "user" / "late.py"  # a new file in an addon
    late.write_text('class O:\n    _inherit = "shop.order"\n    late = fields.Char()\n')
    os.utime(late.parent, ns=(10**18, 10**18))
    changed = _stored(tmp_path).values_digest("shop_order")

    assert kept == first
    assert fields.call_count == 0
    assert changed != first


def _stored(root: Path) -> Registry:
    """A registry (a new run) over the addons of :func:`_registry`, with a record store."""
    settings = Settings(odoo_path=root / "odoo", addons_paths=(root / "addons",))
    return Registry(settings, scans=ScanStore(RecordStore(root / "store")))
