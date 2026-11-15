"""The mypy internals the plugin uses beyond the plugin API still exist in this mypy.

The hooks also read attributes of mypy's checker, semantic analyzer, nodes and error
reporting. After a mypy upgrade a renamed one fails here, with its name and the plugin
modules that use it, rather than as an INTERNAL ERROR in many end-to-end tests. A
removed import already fails when the plugin is imported.

Compiled mypy (the mypyc wheels) exposes every attribute of its classes on the class;
a pure-Python mypy only the methods, so the data attributes are checked on compiled
mypy only.
"""

from __future__ import annotations

from pathlib import Path

import mypy.build
import mypy.checker
import pytest
from mypy.checker import CheckerScope, TypeChecker
from mypy.errors import Errors
from mypy.messages import MessageBuilder
from mypy.nodes import MypyFile, TypeInfo
from mypy.semanal import SemanticAnalyzer
from mypy.typestate import type_state
from mypy.version import __version__ as mypy_version

COMPILED = Path(mypy.checker.__file__ or "").suffix != ".py"

# (owner, name, the plugin modules that use it)
METHODS: list[tuple[object, str, str]] = [
    (TypeChecker, "lookup_type_or_none", "vals.py"),
    (CheckerScope, "top_level_function", "hooks/models.py"),
    (CheckerScope, "current_function", "hooks/models.py"),
    (CheckerScope, "active_class", "hooks/field_definitions.py"),
    (MessageBuilder, "filter_errors", "methods.py, hooks/recordsets.py, hooks/vals.py"),
    (SemanticAnalyzer, "is_incomplete_namespace", "hooks/annotations.py, hooks/nodes.py"),
    (TypeInfo, "update_typeddict_type", "classes.py"),
    (TypeInfo, "calculate_metaclass_type", "classes.py"),
    (type_state, "reset_all_subtype_caches_for", "classes.py"),
    (mypy.build, "take_module_snapshot", "plugin.py (__version__)"),
]
ATTRIBUTES: list[tuple[object, str, str]] = [
    (TypeChecker, "scope", "hooks/models.py, hooks/field_definitions.py"),
    (TypeChecker, "msg", "methods.py, hooks/nodes.py, hooks/recordsets.py, hooks/vals.py"),
    (TypeChecker, "expr_checker", "hooks/recordsets.py, hooks/vals.py"),
    (TypeChecker, "local_type_map", "methods.py (a context manager)"),
    (TypeChecker, "tree", "hooks/models.py"),
    (TypeChecker, "errors", "hooks/models.py"),
    (MypyFile, "plugin_deps", "hooks/models.py"),
    (SemanticAnalyzer, "cur_mod_id", "hooks/annotations.py"),
    (Errors, "ignored_files", "hooks/models.py"),
    (Errors, "_watchers", "hooks/nodes.py (fail_unfiltered)"),
    (MessageBuilder, "errors", "hooks/nodes.py"),
    (TypeInfo, "special_alias", "classes.py"),
]


def _name(owner: object, name: str) -> str:
    """``Class.name``, ``module.name``, or the class of an object (``TypeState.name``)."""
    label = getattr(owner, "__qualname__", None) or getattr(owner, "__name__", None)
    return f"{label or type(owner).__name__}.{name}"


def _ids(entries: list[tuple[object, str, str]]) -> list[str]:
    return [_name(owner, name) for owner, name, _ in entries]


@pytest.mark.parametrize(("owner", "name", "users"), METHODS, ids=_ids(METHODS))
def test_mypy_method_used_by_plugin_exists(owner: object, name: str, users: str) -> None:
    found = getattr(owner, name, None)

    assert callable(found), f"mypy {mypy_version} has no {_name(owner, name)} ({users})"


@pytest.mark.skipif(not COMPILED, reason="pure-Python mypy sets these attributes in __init__")
@pytest.mark.parametrize(("owner", "name", "users"), ATTRIBUTES, ids=_ids(ATTRIBUTES))
def test_mypy_attribute_used_by_plugin_exists(owner: object, name: str, users: str) -> None:
    assert hasattr(owner, name), f"mypy {mypy_version} has no {_name(owner, name)} ({users})"
