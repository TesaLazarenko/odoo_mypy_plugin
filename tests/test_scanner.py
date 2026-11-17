"""Unit tests for the static scan of addon source files."""

from __future__ import annotations

import ast
import sys
import textwrap
import warnings
from pathlib import Path

import pytest

from odoo_typing.scanner import SourceError, parse_bytes, parse_source, referenced_models
from odoo_typing.scanner.assigned import (
    ANY_NAME,
    ANY_VALUE,
    OVERRIDDEN,
    TEXT,
    TRUTHY,
    assigned_values,
    filled_attributes,
)
from odoo_typing.scanner.class_bodies import passed_containers

NESTED = """
    from odoo import fields, models

    def deco(arg):
        return arg

    class Thing(models.Model):
        _name = "x.thing"

        partner_id = fields.Many2one(comodel_name="in.keyword")
        line_ids = fields.One2many("in.positional", "thing_id")

        @deco(lambda self: self.env["in.decorator"])
        def action(self, default=env["in.default"]):
            data = {**self.env["in.dict.unpack"].read(), None: [env["in.list"]]}
            text = f"{self.env['in.fstring'].name}"
            match data:
                case {"k": value} if self.env["in.match.guard"]:
                    pass
            return [env["in.comprehension"] for _ in range(1) if (self.env)["in.parens"]]
"""


def test_referenced_models_nested_expressions_finds_every_literal() -> None:
    tree = ast.parse(textwrap.dedent(NESTED))

    models = referenced_models(tree)

    assert models == {
        "in.keyword",
        "in.positional",
        "in.decorator",
        "in.default",
        "in.dict.unpack",
        "in.list",
        "in.fstring",
        "in.match.guard",
        "in.comprehension",
        "in.parens",
    }


def test_referenced_models_non_literal_names_are_ignored() -> None:
    tree = ast.parse('name = "x"\nenv[name]\nfields.Many2one(name)\nother["y"]\n')

    assert referenced_models(tree) == set()


@pytest.mark.parametrize(
    ("source", "reason"),
    [(b"x = (\n", "line 1"), (b"\xff\xfe = 1\n", "line 1")],
    ids=["syntax-error", "invalid-utf8"],
)
def test_parse_source_invalid_file_raises_source_error(
    tmp_path: Path, source: bytes, reason: str
) -> None:
    path = tmp_path / "bad.py"
    path.write_bytes(source)

    with pytest.raises(SourceError, match=reason) as info:
        parse_source(path)

    assert info.value.path == path


def test_parse_source_missing_file_raises_source_error(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="No such file"):
        parse_source(tmp_path / "missing.py")


def test_parse_source_invalid_escape_is_neither_printed_nor_an_error(tmp_path: Path) -> None:
    path = tmp_path / "regex.py"
    path.write_text('import re\n\nPATTERN = re.compile("\\d+")\n')

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("error")  # python -W error
        tree = parse_source(path)

    assert caught == []
    assert isinstance(tree.body[-1], ast.Assign)


@pytest.mark.parametrize(
    "source",
    [b"x = " + b"-" * 200_000 + b"1\n", b"x = a" + b".b" * 200_000 + b"\n"],
    ids=["parser-stack", "recursion"],
)
def test_parse_source_too_deeply_nested_raises_source_error(tmp_path: Path, source: bytes) -> None:
    path = tmp_path / "deep.py"
    path.write_bytes(source)

    with pytest.raises(SourceError, match="too complex"):
        parse_source(path)


def test_referenced_models_subscript_of_env_alias_with_model_name_counts() -> None:
    source = 'ENV["res.partner"]\nenvironment["sale.order.line"]\nvals["name"]\nrows["Not.Model"]\n'

    assert referenced_models(ast.parse(source)) == {"res.partner", "sale.order.line"}


def test_parse_error_names_the_python_that_read_the_file(tmp_path: Path) -> None:
    # The grammar is the running Python's: newer syntax checked with --python-version.
    path = tmp_path / "models.py"

    with pytest.raises(SourceError) as error:
        parse_bytes(b"x = (\n", path)

    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    assert f"read as Python {running}" in error.value.reason


PASSED = """
class Mixin(models.AbstractModel):
    _name = "t.mixin"

    def _execute(self, trackings: dict[int, tuple[set[str], list[str]]], names: "list[str] | None"):
        self._log(trackings, plain=names)
        super()._execute(trackings, names)

    def _log(self, trackings: dict[int, tuple[set[str], list[str]]], plain: list[str], count: int):
        pass

    def _finalize(self, records, changes):
        kept = [name for name in changes if name]
        if not changes:
            kept = changes
        records._execute({record.id: (kept, []) for record in records}, names=("a", "b"))
        records._execute({1: ({"a"}, ["b"])}, ["a"])
        records._log({}, {name for name in kept}, 3)
        other.unknown([1], {2})
"""


def test_the_containers_a_class_passes_its_annotated_parameters() -> None:
    """A display where the annotation names another container; parameters passed on."""
    node = ast.parse(PASSED).body[0]
    assert isinstance(node, ast.ClassDef)

    assert set(passed_containers(node)) == {
        ("_log", "trackings", ("=", "_execute"), "trackings"),
        ("_log", "plain", ("=", "_execute"), "names"),
        ("_execute", "trackings", ("v", "0"), "list"),
        ("_execute", "names", (), "tuple"),
        ("_log", "plain", (), "set"),
    }


def test_a_class_without_container_annotations_passes_nothing() -> None:
    source = "class A:\n    def f(self, x: int, y=None):\n        self.f([1], {2})\n"
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.ClassDef)

    assert passed_containers(node) == ()


ASSIGNED = """
class Wizard(models.TransientModel):
    _name = "t.wizard"

    def _compute_data(self):
        for record in self:
            if record.action == "a":
                record.data = json.dumps({})
            elif record.action == "b":
                record.data = "{}"
            else:
                raise UserError("no action")
            record.label = _("Total: %s") % record.total
            record.note = "a" + record.name if record.name else f"{record.id}"
            record.joined = ", ".join(record.mapped("name")) or False
            record.code, record.other = "x", self.env.user.name
            record.count += 1
            record.alias = record.name or "-"
            record.partner_id.email = record.email
            record.parent_id.write({"code": False})

    def _compute_names(self):
        url = urlparse("/")
        with self.env.cr.savepoint():
            self.update({"name": "x".format(), "label": url.geturl(), **self._values()})
            self["title"] = "%s" % self.id
            self[key] = False
        try:
            setattr(self, "ref", self._ref())
        except ValueError:
            setattr(self, name, "x")

    def _compute_local(self):
        def text(value):
            return None
        self.local = text(1)
        self.outer = str(self.id)

    def _search_results(self, rows):
        lines = self.line_ids
        for line in lines.filtered("name"):
            line["state"] = "done"
        values = {}
        values["data"] = False
        values.update({"note": False, **rows[0]})
        for record, row in zip(self, rows):
            row["title"] = None

    def helper(self):
        return 1
"""


def test_the_kinds_of_the_values_methods_assign_to_attributes() -> None:
    """Strings by their syntax, calls of the module's names, and what is neither."""
    node = ast.parse(ASSIGNED).body[0]
    assert isinstance(node, ast.ClassDef)

    known, unknown, through, filled = assigned_values(node)

    assert set(known) == {
        ("_compute_data", "data", (TEXT, "json.dumps")),
        ("_compute_data", "label", ("_",)),
        ("_compute_data", "note", (TEXT,)),
        ("_compute_data", "code", (TEXT,)),
        ("_compute_data", "alias", (TEXT, TRUTHY)),  # ``or``: the true operand
        ("_compute_names", "name", (TEXT,)),
        ("_compute_names", ANY_NAME, (ANY_VALUE,)),
        ("_compute_names", "title", (TEXT,)),
        ("_compute_local", "outer", ("str",)),
        ("_search_results", "state", (TEXT,)),  # an item of records; not those of dicts
    }
    # ``or False``, an attribute's value, ``+=``, calls of a local name and of ``self``.
    assert unknown == ("count", "joined", "label", "local", "other", "ref")
    # Values not known, written to the records of a field: maybe of another model.
    assert through == (("code", ("parent_id",)), ("email", ("partner_id",)))
    # Values known in the method, given each record on every path (not in a ``with``).
    assert filled == (
        ("_compute_data", ("alias", "code", "data", "label", "note")),
        ("_compute_local", ("outer",)),
    )


FILLED = """
class Menu(models.Model):
    _name = "t.menu"

    def _all(self):
        self.a = "x"
        self["b"] = "x"
        setattr(self, "c", "x")
        self.update({"d": "x", **values})
        for record in self:
            if record.x:
                record.e = "x"
                continue
            elif record.y:
                raise UserError("y")
            else:
                record.e = "y"
            record.f = "x"

    def _early(self):
        for record in self:
            if record.x:
                return
            record.a = "x"
        self.b = "x"

    def _broken(self):
        for record in self:
            record.a = "x"
            if record.x:
                break

    def _returned(self):
        if not self.x:
            self.a = "x"
            return
        self.a = "y"
        self.b = "x"

    def _inner(self):
        for record in self:
            for line in record.line_ids:
                record.a = "x"
                continue
            while record.x:
                record.b = "x"
                break
            record.c = "x"

    def _guarded(self):
        for record in self:
            try:
                record.a = "x"
            except ValueError:
                record.a = "y"
            with savepoint():
                record.b = "x"
            match record.x:
                case _:
                    record.c = "x"

    def _rebound(self):
        self = self.sudo()
        self.a = "x"

    def _rebound_record(self):
        for record in self:
            record = record.sudo()
            record.a = "x"

    def _some(self):
        for record in self.filtered("x"):
            record.a = "x"

    def _super(self):
        super()._super()
        for record in self:
            record.a = "x"

    def _other_super(self):
        return super(Menu, self)._other_super()

    def _not_super(self):
        super()._compute()
        for record in self:
            super(Menu, record)._not_super()
"""


def test_the_attributes_a_method_assigns_to_each_record_on_every_path() -> None:
    """Paths through ``for record in self:`` and the method: a field Odoo fills with False."""
    node = ast.parse(FILLED).body[0]
    assert isinstance(node, ast.ClassDef)

    filled = {
        item.name: filled_attributes(item)
        for item in node.body
        if isinstance(item, ast.FunctionDef)
    }

    assert filled == {
        "_all": {"a", "b", "c", "d", "e"},  # not ``f``: a ``continue`` before it
        "_early": set(),  # a ``return`` in the loop: the records after it, ``b``
        "_broken": set(),
        "_returned": {"a"},  # on the path of the ``return`` too
        "_inner": {"c"},  # a loop inside may run no time
        "_guarded": {"a"},  # ``with`` may silence an exception; the ``match`` may not match
        "_rebound": set(),
        "_rebound_record": set(),
        "_some": set(),
        "_super": {"a", OVERRIDDEN},
        "_other_super": {OVERRIDDEN},
        "_not_super": set(),  # another method; a ``super()`` of one record
    }
