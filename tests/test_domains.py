"""Unit tests of the domains written as strings (``domain="[...]"`` of relational fields)."""

from __future__ import annotations

import pytest
from pytest_mock import MockerFixture

from odoo_typing import domains as domains_module
from odoo_typing.domains import string_domain_paths


def test_string_domain_paths_literal_list_yields_paths_and_operators() -> None:
    source = (
        "[('partner_id.name', 'ilike', 'x'), '|', ('a', '=', company_id), ('parent.b', '=', 1)]"
    )

    assert list(string_domain_paths(source)) == [("partner_id.name", "ilike"), ("a", "=")]


@pytest.mark.parametrize(
    "source",
    [
        "[('a', '=', " + "-" * 100_000 + "1)]",  # the parser's stack: MemoryError
        "[('a', '=', x" + ".y" * 100_000 + ")]",  # the tree's depth: RecursionError
    ],
    ids=["deep-unary", "long-attribute-chain"],
)
def test_string_domain_paths_too_complex_for_parser_yields_nothing(source: str) -> None:
    assert list(string_domain_paths(source)) == []


def test_string_domain_paths_null_byte_yields_nothing(mocker: MockerFixture) -> None:
    # Python 3.11 raises ValueError for a NUL in the source (3.12: SyntaxError).
    mocker.patch.object(
        domains_module.ast,
        "parse",
        side_effect=ValueError("source code string cannot contain null bytes"),
    )

    assert list(string_domain_paths("[('a', '=', 1)]\0")) == []


def test_string_domain_paths_invalid_escape_yields_path_without_warning(
    recwarn: pytest.WarningsRecorder,
) -> None:
    found = list(string_domain_paths(r"[('name', '=like', '\d%')]"))

    assert (found, [str(warning.message) for warning in recwarn]) == ([("name", "=like")], [])


@pytest.mark.filterwarnings("error")
def test_string_domain_paths_warnings_as_errors_still_yields_path() -> None:
    # Under ``-W error`` a SyntaxWarning of the parser is a SyntaxError: the path was lost.
    assert list(string_domain_paths(r"[('name', '=like', '\d%')]")) == [("name", "=like")]
