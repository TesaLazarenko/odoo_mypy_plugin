"""Tests of the ``run_mypy`` fixture itself: it must not hide crashes or config errors."""

from __future__ import annotations

import pytest
from conftest import RunMypy
from pytest_mock import MockerFixture

from odoo_typing.hooks.env import EnvHooks

ENV_LOOKUP = {
    "my_mod": {
        "models.py": """
        from odoo import models

        class Thing(models.Model):
            _name = "my.thing"

            def action(self) -> None:
                self.env["res.partner"]
        """
    }
}


def test_run_mypy_hook_raises_fails_with_internal_error(
    run_mypy: RunMypy, mocker: MockerFixture
) -> None:
    mocker.patch.object(EnvHooks, "getitem", side_effect=RuntimeError("boom in hook"))

    with pytest.raises(AssertionError, match="INTERNAL ERROR"):
        run_mypy(ENV_LOOKUP)


def test_run_mypy_clean_addon_reports_no_config_warnings(run_mypy: RunMypy) -> None:
    result = run_mypy(ENV_LOOKUP)

    assert (result.exit_status, result.stderr) == (0, "")
