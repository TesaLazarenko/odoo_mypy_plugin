"""Unit tests for the static scan of addon source files."""

from __future__ import annotations

import ast
import sys
import textwrap
import warnings
from pathlib import Path

import pytest

from odoo_typing.scanner import SourceError, parse_bytes, parse_source, referenced_models

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
