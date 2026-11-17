"""Unit tests of the parameters a function only reads where it found them true."""

from __future__ import annotations

import ast

import pytest

from odoo_typing.falsy_guards import guarded_parameters

GET_LANG = """
def get_lang(env, lang_code=None):
    langs = [code for code, _ in env['res.lang'].get_installed()]
    lang = 'en_US' if 'en_US' in langs else langs[0]
    if lang_code and lang_code in langs:
        lang = lang_code
    elif (context_lang := env.context.get('lang')) in langs:
        lang = context_lang
    return env['res.lang']._get_data(code=lang)
"""

CASES = [
    (GET_LANG, {"lang_code"}),
    ("def f(a, b=None):\n    x = b or 'x'\n    return a + x\n", {"b"}),
    ("def f(b):\n    if not b:\n        return ''\n    return b.strip()\n", {"b"}),
    ("def f(b):\n    if not b:\n        return 1\n    else:\n        return b.x\n", {"b"}),
    ("def f(b):\n    return b.x if b else 0\n", {"b"}),
    ("def f(b):\n    while b and b.x:\n        b.y()\n", {"b"}),
    ("def f(b, c):\n    return [x for x in c if b and b.ok(x)]\n", {"b"}),
    ("def f(a, b):\n    if not a or not b:\n        return 0\n    return a + b\n", {"a", "b"}),
    ("def f(a):\n    x = not a\n    return x\n", {"a"}),
    ("def f(a):\n    assert a\n    return a.x\n", {"a"}),
    # The value itself gets out, or is told from another false value.
    ("def f(b=None):\n    return b and b.strip()\n", set()),
    ("def f(b=None):\n    if b is None:\n        return 1\n    return 2\n", set()),
    ("def f(b):\n    return g(b)\n", set()),
    # The test does not reach the read.
    ("def f(b):\n    if not b:\n        x = 1\n    return b.strip()\n", set()),
    ("def f(b):\n    if b:\n        return lambda: b.x\n", set()),
    # Bound again, or never read.
    ("def f(b):\n    if b:\n        b = b.strip()\n    return 1\n", set()),
    ("def f(a):\n    try:\n        a and a.x()\n    except E as a:\n        pass\n", set()),
    ("def f(a, b):\n    for a in b:\n        if a:\n            a.x()\n", set()),
    ("def f(a):\n    return 1\n", set()),
]


@pytest.mark.parametrize(("source", "expected"), CASES)
def test_parameters_read_only_where_they_are_true(source: str, expected: set[str]) -> None:
    function = ast.parse(source).body[0]
    assert isinstance(function, ast.FunctionDef)
    parameters = {argument.arg for argument in function.args.args}

    assert guarded_parameters(function, parameters) == expected
