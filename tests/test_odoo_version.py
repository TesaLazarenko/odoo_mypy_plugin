"""Unit tests for detecting the Odoo version from ``release.py`` without importing Odoo."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from odoo_typing.compat import SUPPORTED_VERSIONS, profile_for
from odoo_typing.odoo_version import (
    profile_mismatch_warning,
    read_odoo_version,
    version_mismatch_warning,
)


def _odoo(tmp_path: Path, release: str) -> Path:
    path = tmp_path / "odoo"
    path.mkdir()
    (path / "release.py").write_text(release)
    return path


def test_read_odoo_version_final_release_returns_major_minor(tmp_path: Path) -> None:
    odoo = _odoo(tmp_path, "FINAL = 'final'\nversion_info = (18, 0, 0, FINAL, 0, '')\n")

    assert read_odoo_version(odoo) == (18, 0)


def test_read_odoo_version_saas_release_returns_major_minor(tmp_path: Path) -> None:
    odoo = _odoo(tmp_path, "version_info = ('saas~17', 4, 0, 'final', 0, '')\n")

    assert read_odoo_version(odoo) == (17, 4)


@pytest.mark.parametrize(
    "release",
    ["", "version_info = make_version()\n", "version_info = ('x', 1)\n", "def broken(:\n"],
)
def test_read_odoo_version_unknown_format_returns_none(tmp_path: Path, release: str) -> None:
    assert read_odoo_version(_odoo(tmp_path, release)) is None


def test_read_odoo_version_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_odoo_version(tmp_path) is None


def test_version_mismatch_warning_other_major_names_both(tmp_path: Path) -> None:
    odoo = _odoo(tmp_path, "version_info = (17, 0, 0, 'final', 0, '')\n")

    message = version_mismatch_warning(18, odoo)

    assert message is not None
    assert "odoo_version = 18" in message
    assert "Odoo 17.0" in message
    assert str(odoo) in message


@pytest.mark.parametrize("release", ["version_info = (18, 0, 0, 'final', 0, '')\n", ""])
def test_version_mismatch_warning_same_major_or_unknown_returns_none(
    tmp_path: Path, release: str
) -> None:
    assert version_mismatch_warning(18, _odoo(tmp_path, release)) is None


@pytest.mark.parametrize("configured", [18, 19])
def test_version_mismatch_warning_saas_release_warns_intermediate_without_advice(
    tmp_path: Path, configured: int
) -> None:
    # A SaaS release may already have the layout of the next major: no version is advised.
    odoo = _odoo(tmp_path, "version_info = ('saas~18', 3, 0, 'final', 0, '')\n")

    message = version_mismatch_warning(configured, odoo)

    assert message is not None
    assert "is Odoo 18.3, an intermediate (SaaS or development) version" in message
    assert "set it to" not in message


def test_version_mismatch_warning_unsupported_major_says_not_supported(tmp_path: Path) -> None:
    odoo = _odoo(tmp_path, "version_info = (21, 0, 0, 'final', 0, '')\n")

    message = version_mismatch_warning(20, odoo)

    assert message is not None
    assert "is Odoo 21.0, which odoo-typing does not support (only 17.0 to 20.0)" in message
    assert "set it to 21" not in message


# The classes of each profile (compat.py) where Odoo 19 defines them.
ORM_19 = {
    "orm/models.py": "class BaseModel(metaclass=MetaModel):\n    pass\n",
    "orm/identifiers.py": "class NewId:\n    pass\n",
    "orm/environments.py": "class Environment(Mapping):\n    pass\n",
    "orm/fields_relational.py": "class _Relational:\n    pass\nclass Many2one(_Relational):\n",
    "orm/domains.py": "class Domain:\n    pass\n",
    "http.py": "class Request:\n    pass\n",
    # Odoo's re-exports: names, not definitions.
    "models.py": "from .orm.models import BaseModel\n",
    "api.py": "from .orm.environments import Environment\n",
    "fields.py": "from .orm.fields_relational import Many2one\n",
}


def _tree(tmp_path: Path, release: str, files: dict[str, str]) -> Path:
    odoo = _odoo(tmp_path, release)
    for name, source in files.items():
        (odoo / name).parent.mkdir(parents=True, exist_ok=True)
        (odoo / name).write_text(source)
    return odoo


@pytest.mark.parametrize("release", ["", "version_info = ('saas~18', 3, 0, 'final', 0, '')\n"])
def test_profile_mismatch_warning_classes_defined_elsewhere_names_them(
    tmp_path: Path, release: str
) -> None:
    profile = profile_for(18)
    assert profile is not None

    message = profile_mismatch_warning(profile, _tree(tmp_path, release, ORM_19))

    assert message is not None
    assert "does not define odoo.models.BaseModel, odoo.api.NewId, odoo.api.Environment, ..." in (
        message
    )
    assert "the Odoo 18 profile expects" in message


def test_profile_mismatch_warning_classes_of_profile_defined_returns_none(tmp_path: Path) -> None:
    profile = profile_for(19)
    assert profile is not None

    assert profile_mismatch_warning(profile, _tree(tmp_path, "", ORM_19)) is None


@pytest.mark.parametrize(
    ("release", "files"),
    [
        ("version_info = (19, 0, 0, 'final', 0, '')\n", ORM_19),  # version_mismatch_warning
        ("", {}),  # a test tree: no sources of the ORM
    ],
)
def test_profile_mismatch_warning_other_final_release_or_no_orm_returns_none(
    tmp_path: Path, release: str, files: dict[str, str]
) -> None:
    profile = profile_for(18)
    assert profile is not None

    assert profile_mismatch_warning(profile, _tree(tmp_path, release, files)) is None


def test_profile_mismatch_warning_unsearchable_orm_directory_warns(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root searches every directory")
    profile = profile_for(19)
    assert profile is not None
    odoo = _tree(tmp_path, "", ORM_19)
    (odoo / "orm").chmod(0)
    try:
        message = profile_mismatch_warning(profile, odoo)
    finally:
        (odoo / "orm").chmod(0o755)

    assert message is not None
    assert "odoo.orm.models.BaseModel" in message


# -- the real checkout of the targeted Odoo (end to end) -------------------------------


def test_profile_mismatch_warning_real_checkout_own_profile_returns_none(
    odoo_path: Path, odoo_version: int
) -> None:
    profile = profile_for(odoo_version)
    assert profile is not None

    assert profile_mismatch_warning(profile, odoo_path) is None


def test_profile_mismatch_warning_real_sources_other_profiles_warn(
    tmp_path: Path, odoo_path: Path, odoo_version: int
) -> None:
    # The real sources without a readable release.py, as in a patched checkout.
    tree = tmp_path / "odoo"
    tree.mkdir()
    for entry in odoo_path.iterdir():
        if entry.name != "release.py":
            (tree / entry.name).symlink_to(entry)
    (tree / "release.py").write_text("")

    warned = {
        version: profile_mismatch_warning(profile, tree) is not None
        for version in SUPPORTED_VERSIONS
        if (profile := profile_for(version)) is not None
    }

    assert warned == {version: version != odoo_version for version in SUPPORTED_VERSIONS}
