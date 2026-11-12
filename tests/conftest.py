"""Shared fixtures: locate Odoo and run mypy with the plugin on throw-away addons."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from mypy import api as mypy_api

from odoo_typing.compat import profile_for
from odoo_typing.odoo_version import read_odoo_version

FIND_ODOO = "import os, odoo; print(os.path.dirname(odoo.__file__))"


def _find_odoo() -> Path | None:
    if env := os.environ.get("ODOO_PATH"):
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


@pytest.fixture(scope="session")
def odoo_path() -> Path:
    path = _find_odoo()
    if path is None or not (path / "release.py").is_file():
        pytest.skip("Odoo not found; set ODOO_PATH to the odoo package directory")
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
        pytest.skip(f"cannot read the Odoo version of {odoo_path}; set ODOO_VERSION")
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
    profile = profile_for(odoo_version)
    if profile is None:
        pytest.skip(f"the plugin has no profile for Odoo {odoo_version} yet")
    # Odoo 19 names classes after their model (ResPartner, not Partner).
    prefix = "Res" if odoo_version >= 19 else ""
    module = "odoo.addons.base.models.res_partner"
    return OdooNames(
        version=odoo_version,
        partner=f"{module}.{prefix}Partner",
        partner_category=f"{module}.{prefix}PartnerCategory",
        base_model=profile.base_model,
        many2one=profile.many2one,
    )


@pytest.fixture(autouse=True)
def _only_for_marked_versions(request: pytest.FixtureRequest) -> None:
    """``@pytest.mark.odoo(18, 19)``: run the test only for those Odoo versions."""
    marker = request.node.get_closest_marker("odoo")
    if marker is not None:
        version = request.getfixturevalue("odoo_version")
        if version not in marker.args:
            pytest.skip(f"only for Odoo {', '.join(map(str, marker.args))}")


@pytest.fixture(scope="session")
def odoo_root(odoo_path: Path, odoo_version: int, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Directory that contains only an ``odoo`` symlink, for ``mypy_path``.

    Every e2e test needs it, so they are all skipped for a version without a profile.
    """
    if profile_for(odoo_version) is None:
        pytest.skip(f"the plugin has no profile for Odoo {odoo_version} yet")
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
    stderr: str

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
        stdout, stderr, status = mypy_api.run(
            ["--config-file", str(config), "--cache-dir", str(cache), *(flags or []), *targets]
        )
        captured = capfd.readouterr()
        _assert_no_crash(status, stdout, stderr, captured.out, captured.err)
        return MypyResult(lines=stdout.splitlines(), exit_status=status, stderr=stderr)

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
