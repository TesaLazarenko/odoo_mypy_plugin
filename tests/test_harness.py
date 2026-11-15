"""Tests of the test harness itself: it must not hide crashes, warnings or skipped tests."""

from __future__ import annotations

import contextlib
import gc
import io
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import conftest
import pytest
from conftest import (
    GC_FROZEN,
    GC_STATE,
    GC_THRESHOLD,
    RunMypy,
    check_odoo_marker,
    check_rss_budget,
    collect_mypy_garbage,
    restore_gc,
    rss_budget_mb,
    run_mypy_in_process,
)
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


def test_run_mypy_plugin_warning_is_in_stderr(run_mypy: RunMypy, mocker: MockerFixture) -> None:
    mocker.patch(
        "odoo_typing.plugin.version_mismatch_warning", return_value="odoo-typing: warning: probe"
    )

    result = run_mypy(ENV_LOOKUP)

    assert "odoo-typing: warning: probe\n" in result.stderr


def test_find_odoo_empty_odoo_path_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_PATH", "")  # ODOO_PATH=$(tools/odoo/fetch.sh 19.0) that failed

    with pytest.raises(pytest.fail.Exception, match="ODOO_PATH is set but empty"):
        conftest._find_odoo()


def test_profile_or_fail_unknown_version_fails_when_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ODOO_REQUIRED", "1")

    with pytest.raises(pytest.fail.Exception, match="no profile for Odoo 21"):
        conftest._profile_or_fail(21)


def test_profile_or_fail_unknown_version_skips_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ODOO_REQUIRED", raising=False)

    with pytest.raises(pytest.skip.Exception, match="no profile for Odoo 21"):
        conftest._profile_or_fail(21)


def test_profile_or_fail_supported_version_returns_its_profile() -> None:
    assert conftest._profile_or_fail(19).version == 19


@pytest.mark.parametrize("versions", [(190,), ("19",), (18, 21), ()])
def test_check_odoo_marker_unknown_or_no_version_fails(versions: tuple[object, ...]) -> None:
    with pytest.raises(pytest.fail.Exception, match=r"@pytest\.mark\.odoo"):
        check_odoo_marker(versions)


def test_check_odoo_marker_supported_versions_pass() -> None:
    check_odoo_marker((17, 18, 19, 20))


def test_restore_gc_tuned_and_frozen_collector_is_reset() -> None:
    gc.set_threshold(200_000, 30, 30)  # what a mypy build leaves
    gc.disable()
    gc.freeze()

    restore_gc()

    frozen = gc.get_freeze_count() <= GC_FROZEN
    assert (gc.get_threshold(), gc.isenabled(), frozen) == (GC_THRESHOLD, True, True)


def _tune_gc_like_mypy(_args: list[str]) -> tuple[str, str, int]:
    """A stand-in for ``mypy.api.run`` that leaves the collector as a build does."""
    gc.set_threshold(200_000, 30, 30)
    gc.freeze()
    return "", "", 0


@pytest.fixture
def no_marked_run(monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> MagicMock:
    """No in-process mypy run marked yet, and a stand-in budget check."""
    monkeypatch.setattr(GC_STATE, "mypy_ran", False)
    return mocker.patch("conftest.check_rss_budget")


def test_collect_mypy_garbage_after_in_process_run_restores_the_collector(
    request: pytest.FixtureRequest, no_marked_run: MagicMock, mocker: MockerFixture
) -> None:
    mocker.patch.object(conftest.mypy_api, "run", side_effect=_tune_gc_like_mypy)
    run_mypy_in_process(["probe.py"])

    collect_mypy_garbage(request)

    frozen = gc.get_freeze_count() <= GC_FROZEN
    restored = (gc.get_threshold(), gc.isenabled(), frozen, GC_STATE.mypy_ran)
    assert restored == (GC_THRESHOLD, True, True, False)
    no_marked_run.assert_called_once_with(request)


def test_collect_mypy_garbage_marked_run_checks_the_budget(
    request: pytest.FixtureRequest, no_marked_run: MagicMock, mocker: MockerFixture
) -> None:
    mocker.patch.object(conftest.mypy_api, "run", return_value=("", "", 0))  # gc untouched
    run_mypy_in_process(["probe.py"])

    collect_mypy_garbage(request)

    assert GC_STATE.mypy_ran is False
    no_marked_run.assert_called_once_with(request)


def test_collect_mypy_garbage_without_run_checks_nothing(
    request: pytest.FixtureRequest, no_marked_run: MagicMock
) -> None:
    collect_mypy_garbage(request)

    no_marked_run.assert_not_called()


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> MagicMock:
    """No budget in the environment and no xdist; returns the memory reader's mock."""
    monkeypatch.delenv("ODOO_TYPING_TEST_RSS_MB", raising=False)
    monkeypatch.delenv("PYTEST_XDIST_WORKER_COUNT", raising=False)
    return mocker.patch("conftest.physical_memory_mb", return_value=12_000)


def test_rss_budget_mb_xdist_workers_share_the_memory(
    machine: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "6")

    assert rss_budget_mb() == 1500  # 12 GB for 6 workers, the controller and the system


@pytest.mark.parametrize("memory", [64_000, None], ids=["large", "unknown"])
def test_rss_budget_mb_one_process_is_at_most_3000(machine: MagicMock, memory: int | None) -> None:
    machine.return_value = memory

    assert rss_budget_mb() == 3000


def test_rss_budget_mb_env_overrides_the_default(
    machine: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "6")
    monkeypatch.setenv("ODOO_TYPING_TEST_RSS_MB", "5000")

    assert rss_budget_mb() == 5000


def test_physical_memory_mb_reads_this_machine() -> None:
    memory = conftest.physical_memory_mb()

    assert memory is None or memory > 100


@pytest.fixture
def session_request(monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> SimpleNamespace:
    """A stand-in request whose session runs, a budget of 1000 MB and a captured stderr."""
    monkeypatch.setenv("ODOO_TYPING_TEST_RSS_MB", "1000")
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "__stderr__", stderr)
    capture = mocker.Mock()
    capture.global_and_fixture_disabled.return_value = contextlib.nullcontext()
    plugins = mocker.Mock()
    plugins.getplugin.return_value = capture
    return SimpleNamespace(
        session=SimpleNamespace(shouldstop=False),
        config=SimpleNamespace(pluginmanager=plugins),
        capture=capture,
        stderr=stderr,
    )


def test_check_rss_budget_crossed_fails_prints_and_stops_the_session(
    session_request: SimpleNamespace, mocker: MockerFixture
) -> None:
    mocker.patch("conftest.rss_mb", return_value=1001)

    with pytest.raises(pytest.fail.Exception, match="holds 1001 MB") as failure:
        check_rss_budget(session_request)

    assert session_request.session.shouldstop == str(failure.value)
    assert str(failure.value) in session_request.stderr.getvalue()
    session_request.capture.global_and_fixture_disabled.assert_called_once_with()


@pytest.mark.parametrize("rss", [1000, None], ids=["within", "unknown"])
def test_check_rss_budget_within_passes(
    session_request: SimpleNamespace, mocker: MockerFixture, rss: int | None
) -> None:
    mocker.patch("conftest.rss_mb", return_value=rss)

    check_rss_budget(session_request)

    assert (session_request.session.shouldstop, session_request.stderr.getvalue()) == (False, "")


def test_rss_mb_reads_this_process() -> None:
    rss = conftest.rss_mb()

    assert rss is None or 10 < rss < 100_000
