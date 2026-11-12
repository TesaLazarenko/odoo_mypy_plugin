"""Static scan of an addon's Python files for Odoo model classes.

Only files reachable through the addon's ``__init__.py`` imports are scanned. The
scan follows Python's execution order (imports run where they appear, also inside
``try``/``if`` blocks), which is the order in which Odoo registers the classes, and
derives model names the way ``odoo.models.MetaModel`` does.

Sources are parsed from bytes, like Python and mypy do, so a UTF-8 BOM or a PEP 263
coding cookie is honoured.

The scan of an addon is :func:`scan_addon` (:mod:`.walker`); its result types are in
:mod:`.results`. The rest reads one part of the sources each: the syntax trees
(:mod:`.syntax`), constant expressions (:mod:`.evaluation`), the body of a model class
(:mod:`.class_bodies`), its field definitions (:mod:`.fields`) and the model names a
file refers to (:mod:`.referenced`).
"""

from __future__ import annotations

from odoo_typing.scanner.fields import POSITIONAL_PARAMETERS
from odoo_typing.scanner.referenced import LOG_ACCESS_USERS, referenced_models
from odoo_typing.scanner.results import (
    AddonScan,
    Contribution,
    FieldInfo,
    ModuleScan,
    SourceError,
)
from odoo_typing.scanner.syntax import parse_source
from odoo_typing.scanner.walker import scan_addon

__all__ = [
    "LOG_ACCESS_USERS",
    "POSITIONAL_PARAMETERS",
    "AddonScan",
    "Contribution",
    "FieldInfo",
    "ModuleScan",
    "SourceError",
    "parse_source",
    "referenced_models",
    "scan_addon",
]
