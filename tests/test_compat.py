"""Unit tests for the per-version Odoo profiles (names of the classes the hooks target)."""

from __future__ import annotations

import pytest

from odoo_typing.compat import SUPPORTED_VERSIONS, OdooProfile, profile_for


def _profile(version: int) -> OdooProfile:
    profile = profile_for(version)
    assert profile is not None
    return profile


def test_supported_versions_are_17_to_20() -> None:
    assert SUPPORTED_VERSIONS == (17, 18, 19, 20)


def test_profile_for_18_targets_odoo_18_modules() -> None:
    profile = _profile(18)

    assert profile.version == 18
    assert profile.base_model == "odoo.models.BaseModel"
    assert profile.env_getitem == "odoo.api.Environment.__getitem__"
    assert profile.relational_classes == frozenset(
        {"odoo.fields.Many2one", "odoo.fields.One2many", "odoo.fields.Many2many"}
    )
    assert profile.is_field_class("odoo.fields.Char")
    assert not profile.is_field_class("odoo.models.Model")


def test_profile_for_19_targets_odoo_orm_modules() -> None:
    profile = _profile(19)

    assert profile.version == 19
    assert profile.base_model == "odoo.orm.models.BaseModel"
    assert profile.env_getitem == "odoo.orm.environments.Environment.__getitem__"
    assert "odoo.orm.fields_relational.Many2one" in profile.relational_classes
    assert profile.is_field_class("odoo.orm.fields.Field")
    assert profile.is_field_class("odoo.orm.fields_textual.Char")
    assert not profile.is_field_class("odoo.orm.models.Model")


def test_profile_for_19_hooks_only_decorator_factories() -> None:
    # Odoo 19 types api.model... as ``C -> C``, but api.depends... as ``-> Decorator``.
    profile = _profile(19)

    assert profile.identity_decorators == frozenset()
    assert "odoo.orm.decorators.depends" in profile.identity_factories
    assert "odoo.orm.decorators.onchange" in profile.identity_factories


def test_profile_for_20_keeps_names_of_19_and_needs_python_3_12() -> None:
    profile_19, profile_20 = _profile(19), _profile(20)

    assert profile_20.version == 20
    assert profile_20.min_python == (3, 12)
    assert profile_20.base_model == profile_19.base_model
    assert profile_20.comodel_generic == profile_19.comodel_generic
    assert profile_20.identity_factories == profile_19.identity_factories


@pytest.mark.parametrize("version", [18, 19])
def test_profile_for_18_and_19_parse_with_python_3_10(version: int) -> None:
    assert _profile(version).min_python == (3, 10)


def test_profile_for_version_not_implemented_yet_returns_none() -> None:
    assert profile_for(17) is None
