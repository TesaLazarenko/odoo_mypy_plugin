"""Unit tests for detecting the Odoo version from ``release.py`` without importing Odoo."""

from __future__ import annotations

from pathlib import Path

import pytest

from odoo_typing.odoo_version import read_odoo_version, version_mismatch_warning


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


@pytest.mark.parametrize(
    "release",
    ["version_info = (18, 0, 0, 'final', 0, '')\n", "version_info = ('saas~18', 2)\n", ""],
)
def test_version_mismatch_warning_same_major_or_unknown_returns_none(
    tmp_path: Path, release: str
) -> None:
    assert version_mismatch_warning(18, _odoo(tmp_path, release)) is None
