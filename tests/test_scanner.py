"""Unit tests for the static scan of addon source files."""

from __future__ import annotations

import ast
import sys
import textwrap
import warnings
from pathlib import Path

import pytest

from odoo_typing.scanner import SourceError, parse_bytes, parse_source, referenced_models
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
