"""Shared fixtures: locate Odoo and run mypy with the plugin on throw-away addons."""

from __future__ import annotations

import os
import subprocess
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from mypy import api as mypy_api

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
def odoo_root(odoo_path: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Directory that contains only an ``odoo`` symlink, for ``mypy_path``."""
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

    def messages(self, filename: str) -> list[str]:
        """``"<line>: <severity>: <message>"`` entries reported for one file."""
        prefix = f"{filename}:"
        return [line.split(prefix, 1)[1] for line in self.lines if prefix in line]


RunMypy = Callable[..., MypyResult]

PYPROJECT = """\
[tool.mypy]
plugins = ["odoo_typing.plugin"]
mypy_path = ["{odoo_root}", "{addons}"]
check_untyped_defs = true
show_error_codes = true
hide_error_context = true

[[tool.mypy.overrides]]
module = ["odoo", "odoo.*"]
follow_imports = "silent"

[tool.odoo_typing]
odoo_path = "{odoo_root}/odoo"
addons_path = ["{addons}"]
"""


@pytest.fixture
def run_mypy(tmp_path: Path, odoo_root: Path, mypy_cache: Path) -> RunMypy:
    """Write addons to a temp dir and type-check them.

    Usage: ``run_mypy({"my_mod": {"models.py": "...", "depends": ["sale"]}})``.
    Each addon gets a manifest and an ``__init__.py`` importing its modules.
    Pass ``check=["my_mod"]`` to limit which addons are given to mypy.
    """

    def run(addons: dict[str, dict[str, object]], check: list[str] | None = None) -> MypyResult:
        root = tmp_path / "addons"
        for name, spec in addons.items():
            _write_addon(root / name, spec)
        config = tmp_path / "pyproject.toml"
        config.write_text(PYPROJECT.format(odoo_root=odoo_root, addons=root))
        targets = [str(root / name) for name in (check or list(addons))]
        stdout, stderr, status = mypy_api.run(
            ["--config-file", str(config), "--cache-dir", str(mypy_cache), *targets]
        )
        assert "Traceback" not in stderr, stderr
        return MypyResult(lines=stdout.splitlines(), exit_status=status)

    return run


def _write_addon(path: Path, spec: dict[str, object]) -> None:
    path.mkdir(parents=True)
    depends = spec.get("depends", ["base"])
    (path / "__manifest__.py").write_text(f"{{'name': '{path.name}', 'depends': {depends!r}}}\n")
    modules = [key for key in spec if key.endswith(".py")]
    imports = "".join(f"from . import {name.removesuffix('.py')}\n" for name in modules)
    (path / "__init__.py").write_text(imports)
    for name in modules:
        source = spec[name]
        assert isinstance(source, str)
        (path / name).write_text(textwrap.dedent(source))
