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
(:mod:`.class_bodies`), its field definitions (:mod:`.fields`), the model names a
file refers to (:mod:`.referenced`) and the ``odoo.values.*`` / ``odoo.model.*``
annotations it uses (:mod:`.annotations`).
"""

from __future__ import annotations

from odoo_typing.scanner.annotations import (
    MODEL,
    VALUES,
    AnnotationAlias,
    annotation_aliases,
)
from odoo_typing.scanner.fields import POSITIONAL_PARAMETERS
from odoo_typing.scanner.referenced import LOG_ACCESS_USERS, referenced_models
from odoo_typing.scanner.results import (
    AddonScan,
    Contribution,
    FieldInfo,
    ModuleScan,
    SourceError,
)
from odoo_typing.scanner.syntax import parse_bytes, parse_source, read_source
from odoo_typing.scanner.walker import scan_addon, scan_file_classes

__all__ = [
    "LOG_ACCESS_USERS",
    "MODEL",
    "POSITIONAL_PARAMETERS",
    "VALUES",
    "AddonScan",
    "AnnotationAlias",
    "Contribution",
    "FieldInfo",
    "ModuleScan",
    "SourceError",
    "annotation_aliases",
    "parse_bytes",
    "parse_source",
    "read_source",
    "referenced_models",
    "scan_addon",
    "scan_file_classes",
]
