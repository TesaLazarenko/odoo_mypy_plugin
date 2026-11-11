"""Unit tests for the per-version Odoo profiles (names of the classes the hooks target)."""

from __future__ import annotations

import pytest

from odoo_typing.compat import SUPPORTED_VERSIONS, profile_for


def test_supported_versions_are_17_to_20() -> None:
    assert SUPPORTED_VERSIONS == (17, 18, 19, 20)


def test_profile_for_18_targets_odoo_18_modules() -> None:
    profile = profile_for(18)

    assert profile.version == 18
    assert profile.base_model == "odoo.models.BaseModel"
    assert profile.env_getitem == "odoo.api.Environment.__getitem__"
    assert profile.relational_classes == frozenset(
        {"odoo.fields.Many2one", "odoo.fields.One2many", "odoo.fields.Many2many"}
    )
    assert profile.is_field_class("odoo.fields.Char")
    assert not profile.is_field_class("odoo.models.Model")


@pytest.mark.parametrize("version", [17, 19, 20])
def test_profile_for_version_not_implemented_yet_returns_none(version: int) -> None:
    assert profile_for(version) is None
