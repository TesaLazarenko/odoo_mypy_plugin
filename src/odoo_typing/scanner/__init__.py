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
    DOMAIN,
    KINDS,
    MODEL,
    VALUES,
    AnnotationAlias,
    annotation_aliases,
)
from odoo_typing.scanner.field_classes import FieldKind, describe_source, field_kind
from odoo_typing.scanner.fields import POSITIONAL_PARAMETERS, linked_field
from odoo_typing.scanner.referenced import LOG_ACCESS_USERS, referenced_models
from odoo_typing.scanner.results import (
    AddonScan,
    Contribution,
    FieldClass,
    FieldInfo,
    ModuleScan,
    SourceError,
    UnlinkedField,
)
from odoo_typing.scanner.syntax import parse_bytes, parse_source, read_source
from odoo_typing.scanner.walker import scan_addon, scan_file_classes

__all__ = [
    "DOMAIN",
    "KINDS",
    "LOG_ACCESS_USERS",
    "MODEL",
    "POSITIONAL_PARAMETERS",
    "VALUES",
    "AddonScan",
    "AnnotationAlias",
    "Contribution",
    "FieldClass",
    "FieldInfo",
    "FieldKind",
    "ModuleScan",
    "SourceError",
    "UnlinkedField",
    "annotation_aliases",
    "describe_source",
    "field_kind",
    "linked_field",
    "parse_bytes",
    "parse_source",
    "read_source",
    "referenced_models",
    "scan_addon",
    "scan_file_classes",
]
