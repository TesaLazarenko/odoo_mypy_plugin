"""Unit tests of ``odoo-typing-baseline`` (roadmap 5.2)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from odoo_typing.baseline import error_keys, main, new_errors

OUTPUT = """\
a/models.py:10: error: Unknown Odoo model "res.partnr"  [odoo-model]
a/models.py:12: note: Revealed type is "int"
a/models.py:20:5: error: Name "x" is not defined  [name-defined]
b/views.py:3: error: Something without a code
Found 3 errors in 2 files (checked 2 source files)
"""


def test_error_keys_drop_lines_and_notes() -> None:
    assert [key for key, _ in error_keys(OUTPUT.splitlines())] == [
        'a/models.py:odoo-model:Unknown Odoo model "res.partnr"',
        'a/models.py:name-defined:Name "x" is not defined',
        "b/views.py:-:Something without a code",
    ]


def test_new_errors_count_each_key() -> None:
    shifted = OUTPUT.replace(":10:", ":14:") + (
        'a/models.py:30: error: Unknown Odoo model "res.partnr"  [odoo-model]\n'
    )
    baseline = Counter(key for key, _ in error_keys(OUTPUT.splitlines()))

    assert new_errors(shifted.splitlines(), baseline) == [
        'a/models.py:30: error: Unknown Odoo model "res.partnr"  [odoo-model]'
    ]


def test_main_record_then_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    baseline = tmp_path / "baseline.json"
    outputs = iter([(OUTPUT, "", 1), (OUTPUT, "", 1), (OUTPUT + OUTPUT.splitlines()[0], "", 1)])

    def run(args: list[str]) -> tuple[str, str, int]:
        assert args[:2] == ["-p", "my"]  # then the output flags (OUTPUT_FLAGS)
        return next(outputs)

    assert main(["record", "--baseline", str(baseline), "--", "-p", "my"], run) == 0
    assert len(json.loads(baseline.read_text())) == 3
    assert main(["check", "--baseline", str(baseline), "--", "-p", "my"], run) == 0
    assert main(["check", "--baseline", str(baseline), "--", "-p", "my"], run) == 1
    assert "1 new errors (3 in the baseline)" in capsys.readouterr().out


def test_main_check_without_baseline_returns_2(tmp_path: Path) -> None:
    status = main(["check", "--baseline", str(tmp_path / "none.json")], lambda _: ("", "", 0))

    assert status == 2


def test_main_runs_mypy_with_one_error_per_line(tmp_path: Path) -> None:
    # ``pretty = true`` in the config wrapped messages at the terminal's width: the keys
    # recorded in a wide window did not match those checked in a narrow one.
    seen: list[list[str]] = []

    def run(args: list[str]) -> tuple[str, str, int]:
        seen.append(args)
        return OUTPUT, "", 1

    main(["record", "--baseline", str(tmp_path / "b.json"), "--", "-p", "x"], run=run)

    assert seen[0][:2] == ["-p", "x"]
    assert {"--no-pretty", "--show-error-codes", "--hide-error-context"} <= set(seen[0])


def test_error_keys_of_a_windows_path() -> None:
    lines = ['C:\\addons\\a\\models.py:10: error: Unknown Odoo model "x"  [odoo-model]']

    assert error_keys(lines) == [
        ('C:\\addons\\a\\models.py:odoo-model:Unknown Odoo model "x"', lines[0])
    ]
