"""Shared fixtures: locate Odoo and run mypy with the plugin on throw-away addons."""

from __future__ import annotations

import contextlib
import gc
import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import pytest
from mypy import api as mypy_api

from odoo_typing.compat import SUPPORTED_VERSIONS, OdooProfile, profile_for
from odoo_typing.errors import RAISE_VARIABLE
from odoo_typing.odoo_version import read_odoo_version

# A hook that raises must fail the test, not fall back to mypy's result (also in the mypy
# subprocesses, which inherit the environment).
os.environ[RAISE_VARIABLE] = "1"

FIND_ODOO = "import os, odoo; print(os.path.dirname(odoo.__file__))"


def _find_odoo() -> Path | None:
    if "ODOO_PATH" in os.environ:
        if not (env := os.environ["ODOO_PATH"]):
            # ODOO_PATH=$(tools/odoo/fetch.sh 19.0) whose fetch failed: never test whatever
            # Odoo the system Python has instead (another version, silently)
            pytest.fail("ODOO_PATH is set but empty (did tools/odoo/fetch.sh fail?)")
        return Path(env)
    for python in ("/usr/bin/python3", "python3"):
        try:
            out = subprocess.run(
                [python, "-c", FIND_ODOO], capture_output=True, text=True, check=True
            )
        except (OSError, subprocess.CalledProcessError):
            continue
        return Path(out.stdout.strip())
    return None


def _skip_or_fail(reason: str) -> NoReturn:
    """Skip an e2e test that needs Odoo, or fail it in CI (``ODOO_REQUIRED=1``)."""
    if os.environ.get("ODOO_REQUIRED") == "1":
        pytest.fail(f"{reason} (ODOO_REQUIRED=1)")
    pytest.skip(reason)


def _profile_or_fail(version: int) -> OdooProfile:
    """The plugin's profile of an Odoo version; skip the test without one, or fail it in CI."""
    profile = profile_for(version)
    if profile is None:
        _skip_or_fail(f"the plugin has no profile for Odoo {version} yet")
    return profile


@pytest.fixture(scope="session")
def odoo_path() -> Path:
    path = _find_odoo()
    if path is None or not (path / "release.py").is_file():
        _skip_or_fail("Odoo not found; set ODOO_PATH to the odoo package directory")
    return path


@pytest.fixture(scope="session")
def odoo_version(odoo_path: Path) -> int:
    """The Odoo major version the e2e tests target: ``ODOO_VERSION``, else ``release.py``.

    Other versions: ``tools/odoo/fetch.sh 19.0`` prints an ``ODOO_PATH`` to use.
    """
    if env := os.environ.get("ODOO_VERSION"):
        return int(env)
    detected = read_odoo_version(odoo_path)
    if detected is None:
        _skip_or_fail(f"cannot read the Odoo version of {odoo_path}; set ODOO_VERSION")
    return detected[0]


@dataclass(frozen=True)
class OdooNames:
    """Full names that e2e expectations print and that differ between Odoo versions."""

    version: int
    partner: str  # model res.partner
    partner_category: str  # model res.partner.category
    base_model: str
    many2one: str


@pytest.fixture(scope="session")
def names(odoo_version: int) -> OdooNames:
    """Names to expect in mypy output for the targeted Odoo version."""
    profile = _profile_or_fail(odoo_version)
    # Odoo 19 names classes after their model (ResPartner, not Partner); in Odoo 17 the
    # last class of res.partner is base's populate extension.
    prefix = "Res" if odoo_version >= 19 else ""
    module = "odoo.addons.base.models.res_partner"
    tip = "odoo.addons.base.populate.res_partner" if odoo_version == 17 else module
    return OdooNames(
        version=odoo_version,
        partner=f"{tip}.{prefix}Partner",
        partner_category=f"{module}.{prefix}PartnerCategory",
        base_model=profile.base_model,
        many2one=profile.many2one,
    )


@pytest.fixture(autouse=True)
def _own_scan_cache(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each test its own shared scan cache (``scan_cache_dir`` defaults to the user's)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("xdg-cache")))


# The collector's thresholds and frozen objects before any test ran mypy in this process
# (Python 3.12 counts a few hundred static objects as frozen from the start).
GC_THRESHOLD = gc.get_threshold()
GC_FROZEN = gc.get_freeze_count()
# The most memory (MB) a pytest process may hold after collecting: about 0.4-0.7 GB per
# worker is normal, the leak this guards against reached 8 GB. By default the smaller of
# MAX_RSS_BUDGET_MB and the machine's memory shared by the xdist workers plus 2 (the
# controller, the system and the dmypy daemons).
RSS_BUDGET_ENV = "ODOO_TYPING_TEST_RSS_MB"
MAX_RSS_BUDGET_MB = 3000


@dataclass
class GcState:
    """What the in-process mypy runs of this pytest process left to clean up."""

    mypy_ran: bool = False  # run_mypy_in_process ran since the last collection


GC_STATE = GcState()


def run_mypy_in_process(args: list[str]) -> tuple[str, str, int]:
    """``mypy.api.run``; the test's teardown then frees what the run left behind."""
    GC_STATE.mypy_ran = True
    return mypy_api.run(args)


def restore_gc() -> None:
    """Undo mypy's tuning of the cyclic collector, then free the cycles of its runs."""
    gc.set_threshold(*GC_THRESHOLD)
    gc.enable()
    if gc.get_freeze_count() > GC_FROZEN:  # frozen objects are never collected
        gc.unfreeze()
    gc.collect()


def rss_mb() -> int | None:
    """The resident memory of this process in MB, or ``None`` without ``/proc`` (not Linux)."""
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return pages * os.sysconf("SC_PAGE_SIZE") // 2**20


def physical_memory_mb() -> int | None:
    """The machine's physical memory in MB, or ``None`` where ``os.sysconf`` cannot tell."""
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // 2**20
    except (AttributeError, OSError, ValueError):
        return None


def rss_budget_mb() -> int:
    """The RSS budget of this process: ``ODOO_TYPING_TEST_RSS_MB``, else the default.

    The default is ``min(3000, memory // (workers + 2))``: 1500 MB for ``-n 6`` on 12 GB,
    close to 2000 MB for ``-n 2`` on an 8 GB CI runner, 3000 MB without xdist.
    """
    if env := os.environ.get(RSS_BUDGET_ENV):
        return int(env)
    memory = physical_memory_mb()
    if memory is None:
        return MAX_RSS_BUDGET_MB
    workers = int(os.environ.get("PYTEST_XDIST_WORKER_COUNT") or 1)
    return min(MAX_RSS_BUDGET_MB, memory // (workers + 2))


def _print_now(request: pytest.FixtureRequest, message: str) -> None:
    """Write ``message`` to this process's real stderr now, past pytest's capturing."""
    stream = sys.__stderr__
    if stream is None:
        return
    capture = request.config.pluginmanager.getplugin("capturemanager")
    suspended = capture.global_and_fixture_disabled() if capture else contextlib.nullcontext()
    with suspended:
        stream.write(f"\n{message}\n")
        stream.flush()


def check_rss_budget(request: pytest.FixtureRequest) -> None:
    """Fail the test after which this process holds more than its budget, and stop the run.

    The message goes to stderr at once and the session stops after this test (xdist then
    stops the other workers after their current test), so that it is seen while the
    machine still has memory: the run's final summary may never come.
    """
    budget = rss_budget_mb()
    rss = rss_mb()
    if rss is None or rss <= budget:
        return
    message = (
        f"the pytest process holds {rss} MB after collecting the garbage, more than its"
        f" budget of {budget} MB ({RSS_BUDGET_ENV}): in-process mypy runs leak (see"
        " _collect_mypy_garbage in tests/conftest.py); the run stops"
    )
    _print_now(request, f"odoo-typing tests: {message}")
    request.session.shouldstop = message
    pytest.fail(message)


def collect_mypy_garbage(request: pytest.FixtureRequest) -> None:
    """After a test that ran mypy in this process: restore the collector, check the budget.

    ``run_mypy_in_process`` marks such a test; the collector's state also gives away a
    test that calls ``mypy.api.run`` itself.
    """
    tuned = (
        gc.get_threshold() != GC_THRESHOLD
        or not gc.isenabled()
        or gc.get_freeze_count() > GC_FROZEN
    )
    if GC_STATE.mypy_ran or tuned:
        GC_STATE.mypy_ran = False
        restore_gc()
        check_rss_budget(request)


@pytest.fixture(autouse=True)
def _collect_mypy_garbage(request: pytest.FixtureRequest) -> Iterator[None]:
    """Free what a mypy run in this process left behind, after each test that made one.

    mypy is written for a process of its own: a build all but turns the cyclic collector
    off (``gc.set_threshold(200_000, 30, 30)``, ``gc.freeze()``) and never turns it back
    on, so the module graph of every run (cycles: no reference counting frees them) stayed
    in memory. Some 450 runs in one pytest process grew it to 8 GB on Odoo 19, the whole
    memory of a GitHub runner; collected after each test it stays near 400 MB. Should a
    mypy release tune the collector differently, the RSS budget (``check_rss_budget``),
    checked after each such test, fails the test that crosses it and stops the run. It
    sees only the memory of this process after a test: it cannot stop one test that takes
    the machine's memory by itself, nor memory taken outside pytest.
    """
    yield
    collect_mypy_garbage(request)


def check_odoo_marker(versions: tuple[object, ...]) -> None:
    """Fail a test whose ``@pytest.mark.odoo(...)`` names no supported version or a typo."""
    unknown = [version for version in versions if version not in SUPPORTED_VERSIONS]
    if unknown or not versions:
        supported = ", ".join(map(str, SUPPORTED_VERSIONS))
        pytest.fail(f"@pytest.mark.odoo{versions!r}: give Odoo versions among {supported}")


@pytest.fixture(autouse=True)
def _only_for_marked_versions(request: pytest.FixtureRequest) -> None:
    """``@pytest.mark.odoo(18, 19)``: run the test only for those Odoo versions."""
    marker = request.node.get_closest_marker("odoo")
    if marker is not None:
        check_odoo_marker(marker.args)
        version = request.getfixturevalue("odoo_version")
        if version not in marker.args:
            pytest.skip(f"only for Odoo {', '.join(map(str, marker.args))}")


@pytest.fixture(scope="session")
def odoo_root(odoo_path: Path, odoo_version: int, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Directory that contains only an ``odoo`` symlink, for ``mypy_path``.

    Every e2e test needs it, so they are all skipped (failed with ``ODOO_REQUIRED=1``) for
    a version without a profile.
    """
    _profile_or_fail(odoo_version)
    root = tmp_path_factory.mktemp("odoo_root")
    (root / "odoo").symlink_to(odoo_path)
    return root


@pytest.fixture(scope="session")
def mypy_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("mypy_cache")


@dataclass(frozen=True)
class MypyResult:
    lines: list[str]
    exit_status: int
    stderr: str  # mypy's, then the plugin's warnings: empty for a clean run

    def messages(self, filename: str) -> list[str]:
        """``"<line>: <severity>: <message>"`` entries reported for one file."""
        prefix = f"{filename}:"
        return [line.split(prefix, 1)[1] for line in self.lines if prefix in line]


RunMypy = Callable[..., MypyResult]

PYPROJECT = """\
[tool.mypy]
plugins = ["odoo_typing.plugin"]
mypy_path = {mypy_path}
check_untyped_defs = true
show_error_codes = true
show_error_context = false
show_traceback = true

[[tool.mypy.overrides]]
module = ["odoo", "odoo.*"]
follow_imports = "silent"

[tool.odoo_typing]
odoo_path = "{odoo_root}/odoo"
odoo_version = {odoo_version}
addons_path = {addons_path}
"""


# mypy.api.run() returns only the regular output; on a crash mypy prints the
# "INTERNAL ERROR" banner (and the traceback) straight to the real stderr and exits 2.
CRASH_MARKERS = ("INTERNAL ERROR", "Traceback")


def _assert_no_crash(status: int, *outputs: str) -> None:
    text = "\n".join(outputs)
    for marker in CRASH_MARKERS:
        assert marker not in text, text
    assert status in (0, 1), f"mypy exited with status {status}:\n{text}"


@pytest.fixture
def run_mypy(
    tmp_path: Path,
    odoo_root: Path,
    odoo_version: int,
    mypy_cache: Path,
    capfd: pytest.CaptureFixture[str],
) -> RunMypy:
    """Write addons to a temp dir and type-check them.

    Usage: ``run_mypy({"my_mod": {"models.py": "...", "depends": ["sale"]}})``.
    Each addon gets a manifest (``"manifest": "__openerp__.py"`` picks another file
    name) and, unless the spec has its own ``"__init__.py"``, one that imports its
    top-level modules. Keys may contain ``/`` (``"models/a.py"``); a source given as
    ``bytes`` is written unchanged (BOM, other encodings).
    Pass ``check=["my_mod"]`` to limit which addons are given to mypy,
    ``scripts={"tool.py": "..."}`` to also check files outside any addon, and
    ``fresh_cache=True`` to use a cache of the test's own, empty on the first call
    (so that Odoo's own modules are analyzed again in this run; a second call in the
    same test reuses it, for warm-cache checks), and ``flags=[...]`` for extra mypy
    command-line options. ``workdir="x"`` puts the addons and the config in a
    subdirectory ``x`` of the test's directory (two working copies sharing a cache);
    ``addons_path=[...]`` replaces the configured addons directories (default: the
    one the addons are written to) and ``mypy_path=[...]`` adds directories to mypy's.
    The call fails the test if mypy crashes (internal error, exit status 2).
    """

    def run(
        addons: dict[str, dict[str, object]],
        check: list[str] | None = None,
        *,
        scripts: dict[str, str] | None = None,
        fresh_cache: bool = False,
        flags: list[str] | None = None,
        workdir: str = "",
        addons_path: list[Path] | None = None,
        mypy_path: list[Path] | None = None,
        options: str = "",
    ) -> MypyResult:
        base = tmp_path / workdir
        root = base / "addons"
        root.mkdir(parents=True, exist_ok=True)
        for name, spec in addons.items():
            write_addon(root / name, spec)
        targets = [str(root / name) for name in (check or list(addons))]
        for name, source in (scripts or {}).items():
            script = tmp_path / "scripts" / name
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text(textwrap.dedent(source))
            targets.append(str(script))
        config = base / "pyproject.toml"
        search_path = [odoo_root, root, *(mypy_path or [])]
        config.write_text(
            PYPROJECT.format(
                odoo_root=odoo_root,
                odoo_version=odoo_version,
                mypy_path=_toml_paths(search_path),
                addons_path=_toml_paths(addons_path or [root]),
            )
            + options
        )
        cache = tmp_path / "mypy_cache" if fresh_cache else mypy_cache
        stdout, stderr, status = run_mypy_in_process(
            ["--config-file", str(config), "--cache-dir", str(cache), *(flags or []), *targets]
        )
        captured = capfd.readouterr()
        _assert_no_crash(status, stdout, stderr, captured.out, captured.err)
        # mypy's own stderr, then what the plugin printed (its warnings go to sys.stderr)
        return MypyResult(
            lines=stdout.splitlines(), exit_status=status, stderr=stderr + captured.err
        )

    return run


def _toml_paths(paths: list[Path]) -> str:
    return json.dumps([str(path) for path in paths])  # a JSON string list is valid TOML


def write_addon(path: Path, spec: dict[str, object]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    depends = spec.get("depends", ["base"])
    manifest = spec.get("manifest", "__manifest__.py")
    assert isinstance(manifest, str)
    data = spec.get("data", {})
    assert isinstance(data, dict)
    (path / manifest).write_text(
        f"{{'name': '{path.name}', 'depends': {depends!r}, 'data': {list(data)!r}}}\n"
    )
    for name, content in data.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(textwrap.dedent(content))
    files = {key: spec[key] for key in spec if key.endswith(".py")}
    if "__init__.py" not in files:
        top_level = [name.removesuffix(".py") for name in files if "/" not in name]
        files["__init__.py"] = "".join(f"from . import {name}\n" for name in top_level)
    for name, source in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(source, bytes):
            target.write_bytes(source)
        else:
            assert isinstance(source, str)
            target.write_text(textwrap.dedent(source))
