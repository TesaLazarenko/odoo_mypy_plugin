"""The model names a file refers to: ``env[...]`` literals, comodels, ``env.user``."""

from __future__ import annotations

import ast
import re
from typing import Final

from odoo_typing.node_index import NodeIndex, index_nodes
from odoo_typing.scanner.syntax import (
    RELATIONAL_FIELDS,
    callee_name,
    comodel_argument,
    single_target,
    string_literal,
)

__all__ = ["ENV_RECORDS", "LOG_ACCESS_USERS", "referenced_models"]

# Environment attributes that are records of a model (``self.env.user``).
ENV_RECORDS: Final = {"user": "res.users", "company": "res.company", "companies": "res.company"}
# Log-access fields that are res.users records (``record.create_uid``).
LOG_ACCESS_USERS: Final = frozenset({"create_uid", "write_uid"})
# The shape of a model name (``res.partner``), for subscripts of ``env`` aliases.
MODEL_NAME: Final = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+")


def referenced_models(tree: ast.Module, nodes: NodeIndex | None = None) -> set[str]:
    """Model names a file uses: literals in ``env[...]`` and relational field comodels.

    ``env[NAME]`` counts when ``NAME`` is a module-level string constant, and
    ``env.user`` / ``env.company`` / ``env.companies`` name ``res.users`` and
    ``res.company``, and so do ``.create_uid`` / ``.write_uid`` (``res.users``). Any other
    ``env.<name>`` names the model ``<name>``, if there is one (``env.website``).
    A subscript of any other expression counts when its key looks like a model name
    (``ENV["res.partner"]``, ``environment[...]``: aliases of ``env``); a key that is
    no model only adds an edge to nothing.
    """
    constants = _string_constants(tree)
    index = index_nodes(tree) if nodes is None else nodes
    models: set[str] = set()
    for subscript in index.of(ast.Subscript):
        key = subscript.slice
        name = constants.get(key.id) if isinstance(key, ast.Name) else string_literal(key)
        if name and (_is_env(subscript.value) or MODEL_NAME.fullmatch(name)):
            models.add(name)
    for call in index.of(ast.Call):
        if callee_name(call.func) in RELATIONAL_FIELDS and (name := comodel_argument(call)):
            models.add(name)
    for attribute in index.of(ast.Attribute):
        if attribute.attr in ENV_RECORDS and _is_env(attribute.value):
            models.add(ENV_RECORDS[attribute.attr])
        elif _is_env(attribute.value):
            # ``env.website`` (Odoo 20): records of the model the attribute is named
            # after. Any other name (``env.cr``) only adds an edge to nothing.
            models.add(attribute.attr)
        elif attribute.attr in LOG_ACCESS_USERS:
            models.add("res.users")  # ``record.create_uid``: a res.users record
    return models


def _string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME: Final = "text"`` constants (mypy knows their value)."""
    constants: dict[str, str] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.AnnAssign) or not _is_final(statement.annotation):
            continue
        name = single_target(statement)
        text = string_literal(statement.value) if statement.value is not None else None
        if name is not None and text is not None:
            constants[name] = text
    return constants


def _is_final(annotation: ast.expr) -> bool:
    """``Final``, ``typing.Final`` or ``Final[str]``."""
    if isinstance(annotation, ast.Subscript):
        annotation = annotation.value
    return callee_name(annotation) == "Final"


def _is_env(node: ast.expr) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "env") or (
        isinstance(node, ast.Name) and node.id == "env"
    )
