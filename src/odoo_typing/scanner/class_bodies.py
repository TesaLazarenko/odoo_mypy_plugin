"""What the body of a class says about its model, derived like ``odoo.models.MetaModel``.

The model name and its ``_inherit`` parents, the methods, the models it delegates to
(``_inherits``), ``_log_access`` and whether the class is abstract.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Mapping
from typing import Final

from odoo_typing.scanner.evaluation import UNKNOWN
from odoo_typing.scanner.syntax import RELATIONAL_FIELDS, callee_name, comodel_argument

__all__ = [
    "FORWARD",
    "ITEM",
    "NAME_TEST",
    "SEQUENCES",
    "VALUE",
    "abstract",
    "as_names",
    "declared_comodels",
    "defined_methods",
    "delegated_models",
    "inherits_pairs",
    "log_access",
    "model_name",
    "parent_store",
    "passed_containers",
    "read_value_keys",
    "record_dispatches",
]

# Bases of the classes of models that have a table (``_abstract = False``).
CONCRETE_BASES: Final = frozenset({"Model", "TransientModel"})


def model_name(class_name: str, attrs: Mapping[str, object]) -> str | None:
    """The model a class defines or extends, derived like ``MetaModel.__new__``.

    ``_name`` if the class sets it; otherwise the single ``_inherit`` entry, or the
    class name when ``_inherit`` lists several models. ``None`` when that cannot be
    known statically, and for classes that set neither attribute: MetaModel would
    name a model after the class, but such classes are usually plain Python bases.
    """
    if "_name" in attrs:
        name = attrs["_name"]
        return name if isinstance(name, str) else None
    inherit = attrs.get("_inherit", UNKNOWN)
    if isinstance(inherit, str):
        return inherit
    if not isinstance(inherit, list | tuple):
        return None
    if len(inherit) == 1:
        return inherit[0] if isinstance(inherit[0], str) else None
    return class_name


def as_names(value: object) -> tuple[str, ...]:
    """The model names of an ``_inherit`` value: one name, or a list of them."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def defined_methods(node: ast.ClassDef) -> tuple[str, ...]:
    """Names of the functions a class body defines, in source order.

    Also ``alias = method`` and ``name = module.function`` assignments
    (``_check_company_domain = models.check_company_domain_parent_of``). Private
    names get Python's mangling (``__x`` in ``Order`` is ``_Order__x``).
    """
    names: dict[str, None] = {}
    for statement in node.body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            names[_mangled(statement.name, node.name)] = None
            continue
        if not isinstance(statement, ast.Assign):
            continue
        value = statement.value
        aliased = isinstance(value, ast.Name) and _mangled(value.id, node.name) in names
        function = isinstance(value, ast.Attribute) and value.attr[:1].islower()
        if aliased or function:  # also ``a = b = method``
            targets = (target for target in statement.targets if isinstance(target, ast.Name))
            names.update(dict.fromkeys(_mangled(target.id, node.name) for target in targets))
    return tuple(names)


def _mangled(name: str, class_name: str) -> str:
    """A name as the class namespace stores it (private name mangling)."""
    if name.startswith("__") and not name.endswith("__") and class_name.strip("_"):
        return f"_{class_name.lstrip('_')}{name}"
    return name


def declared_comodels(node: ast.ClassDef) -> tuple[str, ...]:
    """Comodel literals of the relational fields assigned in a class body."""
    comodels: dict[str, None] = {}
    for statement in node.body:
        value = statement.value if isinstance(statement, ast.Assign | ast.AnnAssign) else None
        if isinstance(value, ast.Call) and callee_name(value.func) in RELATIONAL_FIELDS:
            comodel = comodel_argument(value)
            if comodel:
                comodels[comodel] = None
    return tuple(comodels)


def delegated_models(node: ast.ClassDef, inherits: object) -> tuple[str, ...]:
    """Models a class delegates to: ``_inherits`` keys, then ``delegate=True`` comodels."""
    models: dict[str, None] = {}
    if isinstance(inherits, dict):
        models.update(dict.fromkeys(key for key in inherits if isinstance(key, str)))
    for statement in node.body:
        value = statement.value if isinstance(statement, ast.Assign | ast.AnnAssign) else None
        if isinstance(value, ast.Call) and _is_delegate_field(value):
            comodel = comodel_argument(value)
            if comodel:
                models[comodel] = None
    return tuple(models)


def _is_delegate_field(call: ast.Call) -> bool:
    """``fields.Many2one(..., delegate=True)``."""
    return callee_name(call.func) == "Many2one" and any(
        keyword.arg == "delegate"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )


def inherits_pairs(inherits: object) -> tuple[tuple[str, str], ...]:
    """The (model, field name) pairs of an ``_inherits`` dict, in order."""
    if not isinstance(inherits, dict):
        return ()
    return tuple(
        (model, name)
        for model, name in inherits.items()
        if isinstance(model, str) and isinstance(name, str)
    )


def log_access(attrs: dict[str, object]) -> bool | None:
    """``_log_access`` of a class body; MetaModel defaults it to ``_auto``."""
    value = attrs.get("_log_access", attrs.get("_auto"))
    return value if isinstance(value, bool) else None


def parent_store(attrs: dict[str, object]) -> bool | None:
    """``_parent_store`` of a class body, ``None`` if it does not set it."""
    value = attrs.get("_parent_store")
    return value if isinstance(value, bool) else None


def abstract(node: ast.ClassDef, attrs: dict[str, object]) -> bool | None:
    """Whether a class is abstract (see :attr:`Contribution.abstract`)."""
    value = attrs.get("_abstract")
    if isinstance(value, bool):
        return value
    kinds = {callee_name(base) for base in node.bases}
    if kinds & CONCRETE_BASES:
        return False
    return True if "AbstractModel" in kinds else None


# Methods whose first parameter is the values a caller gives (``create(vals_list)``).
VALUES_METHODS: Final = frozenset({"create", "write"})
# Calls of a dict that read a key: ``vals.pop("k", None)``, ``vals.get("k")``.
KEY_READS: Final = frozenset({"pop", "get", "setdefault"})


def read_value_keys(node: ast.ClassDef) -> tuple[str, ...]:
    """The keys the ``create`` / ``write`` of a class body read from the values given.

    ``vals.pop("counterpart_account_id", None)`` in ``account.bank.statement.line``'s
    ``create`` takes a key that is no field: the override consumes it before Odoo sees
    the values. Read are ``vals.pop("k")`` / ``.get`` / ``.setdefault``, ``vals["k"]``,
    ``"k" in vals`` and ``del vals["k"]`` of the first parameter, or of a name a ``for``
    binds to its items (``for vals in vals_list:``).
    """
    keys: dict[str, None] = {}
    for statement in node.body:
        if not isinstance(statement, ast.FunctionDef) or statement.name not in VALUES_METHODS:
            continue
        parameters = statement.args.posonlyargs + statement.args.args
        if len(parameters) < 2:
            continue
        names = {parameters[1].arg}
        nodes = list(ast.walk(statement))
        for item in nodes:
            if isinstance(item, (ast.For, ast.comprehension)) and _names_of(item.iter) & names:
                names |= _names_of(item.target)
        for item in nodes:
            key = _read_key(item, names)
            if key is not None:
                keys[key] = None
    return tuple(keys)


def _names_of(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return {name for item in node.elts for name in _names_of(item)}
    return set()


def _read_key(node: ast.AST, names: set[str]) -> str | None:
    """The key a node reads from one of some dicts, if it reads one."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in KEY_READS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in names
        and node.args
    ):
        return _text(node.args[0])
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, (ast.Load, ast.Del))  # not ``vals["k"] = x``
        and isinstance(node.value, ast.Name)
        and node.value.id in names
    ):
        return _text(node.slice)
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], (ast.In, ast.NotIn))
        and isinstance(node.comparators[0], ast.Name)
        and node.comparators[0].id in names
    ):
        return _text(node.left)
    return None


def _text(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


# A comparison of a name's ``_name`` (``source_ref._name == "mail.template"``).
NAME_TEST: Final = re.compile(rb"\w\._name\s*(?:[=!]=|(?:not\s+)?in\b)")
NAME_OPERATORS: Final = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)


def record_dispatches(node: ast.ClassDef) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """The parameters of a class's methods that take records of the models they test.

    ``_get_source_from_ref(self, source_ref)`` of ``mail.thread`` tests
    ``source_ref._name == "mail.template"`` (and ``"ir.ui.view"``): the parameter takes
    records of those models, whatever it is annotated with. So does a parameter that a
    method passes on to such a parameter of a method of the class
    (``message_post_with_source(source_ref)`` calls ``self._get_source_from_ref(source_ref)``).

    Returns:
        ``(method, parameter, models)`` for each such parameter.
    """
    methods = {
        statement.name: statement
        for statement in node.body
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    positional = {
        name: [argument.arg for argument in (method.args.posonlyargs + method.args.args)[1:]]
        for name, method in methods.items()
    }
    tested: dict[tuple[str, str], set[str]] = {}
    passed: list[tuple[str, str, str, str]] = []  # method, parameter, callee, its parameter
    for name, function in methods.items():
        receiver = function.args.args[0].arg if function.args.args else None
        own = {*positional[name], *(argument.arg for argument in function.args.kwonlyargs)}
        for item in ast.walk(function):
            for parameter, models in _name_tests(item, own):
                tested.setdefault((name, parameter), set()).update(models)
            callee = _own_method(item, receiver, methods)
            if callee is not None and isinstance(item, ast.Call):
                passed += _passed(name, item, own, positional[callee])
    changed = True
    while changed:  # through the methods a method calls, to a fixed point
        changed = False
        for name, parameter, callee, target in passed:
            found = tested.get((callee, target), set())
            known = tested.setdefault((name, parameter), set())
            if not found <= known:
                known |= found
                changed = True
    return tuple(
        (method, parameter, tuple(sorted(models)))
        for (method, parameter), models in tested.items()
        if models
    )


def _own_method(node: ast.AST, receiver: str | None, methods: Mapping[str, object]) -> str | None:
    """The method of the class that a node calls on ``self``, if it is such a call."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == receiver
        and node.func.attr in methods
    ):
        return node.func.attr
    return None


def _name_tests(node: ast.AST, parameters: set[str]) -> Iterator[tuple[str, set[str]]]:
    """``p._name == "m"`` / ``"m" == p._name`` / ``p._name in ("m", "n")`` of a parameter."""
    if not isinstance(node, ast.Compare):
        return
    if len(node.ops) != 1 or not isinstance(node.ops[0], NAME_OPERATORS):
        return
    for tested, other in ((node.left, node.comparators[0]), (node.comparators[0], node.left)):
        if (
            isinstance(tested, ast.Attribute)
            and tested.attr == "_name"
            and isinstance(tested.value, ast.Name)
            and tested.value.id in parameters
        ):
            items = other.elts if isinstance(other, ast.Tuple | ast.List | ast.Set) else [other]
            models = {
                item.value
                for item in items
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            }
            if models:
                yield tested.value.id, models


def _passed(
    name: str, call: ast.Call, parameters: set[str], callee_parameters: list[str]
) -> list[tuple[str, str, str, str]]:
    """The parameters a call of a method of the class passes on, and where they go."""
    callee = call.func.attr if isinstance(call.func, ast.Attribute) else ""
    found = [
        (name, argument.id, callee, callee_parameters[position])
        for position, argument in enumerate(call.args)
        if isinstance(argument, ast.Name)
        and argument.id in parameters
        and position < len(callee_parameters)
    ]
    found += [
        (name, keyword.value.id, callee, keyword.arg)
        for keyword in call.keywords
        if keyword.arg is not None
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id in parameters
    ]
    return found


# What a display or a comprehension builds, as the annotation of its value names it.
DISPLAYS: Final = {
    ast.Dict: "dict",
    ast.DictComp: "dict",
    ast.List: "list",
    ast.ListComp: "list",
    ast.Set: "set",
    ast.SetComp: "set",
    ast.Tuple: "tuple",
}
# The steps of a path into a value: the values of a dict, the items of a list or a set;
# an item of a tuple is its index.
VALUE: Final = "v"
ITEM: Final = "i"
FORWARD: Final = "="  # (callee, its parameter, ("=", method), parameter): passed on as given
# The containers an annotation names that another display does not build.
SEQUENCES: Final = frozenset({"list", "set", "frozenset"})
GIVEN: Final = frozenset({"list", "set", "tuple"})
MAX_PATH: Final = 4

Passed = tuple[str, str, tuple[str, ...], str]


def passed_containers(node: ast.ClassDef) -> tuple[Passed, ...]:
    """The containers a class passes its own methods where their annotations name others.

    ``_track_execute(self, ..., trackings: dict[int, tuple[set[str], list[ValuesType]]])``
    of ``mail.track.mixin`` (Odoo 20) is called by the mixin itself with ``{parent.id:
    (filtered_changes, filtered_trackings) for parent in parents}``, where
    ``filtered_changes`` is a list comprehension: the class that wrote the annotation
    passes a list where it names a set, so the annotation is narrower than the code.

    Read are the calls, in the methods of the class, of the methods it defines with
    annotated parameters: the displays and comprehensions of their arguments, also
    through the names the calling method binds.

    Returns:
        ``(method, parameter, path, kind)``: ``parameter`` of ``method`` is given a
        ``kind`` (``list``, ``set``, ``tuple``) at ``path`` (``("v", "0")``: the first
        item of the tuples that are the values of a dict) where its annotation names
        another of them. A parameter that a method passes on as it got it is
        ``(callee, its parameter, ("=", method), parameter)``; one that a method passes
        on to itself (a recursion, ``super()``) is left out.
    """
    methods = {
        statement.name: statement
        for statement in node.body
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    annotations = {
        name: {
            argument.arg: argument.annotation
            for argument in _parameters(function)
            if argument.annotation is not None and _names_sequence(argument.annotation)
        }
        for name, function in methods.items()
    }
    if not any(annotations.values()):
        return ()
    positional = {
        name: [argument.arg for argument in (method.args.posonlyargs + method.args.args)[1:]]
        for name, method in methods.items()
    }
    found: dict[Passed, None] = {}
    for name, function in methods.items():
        own = {argument.arg for argument in _parameters(function)}
        calls = [
            item
            for item in ast.walk(function)
            if isinstance(item, ast.Call)
            and isinstance(item.func, ast.Attribute)
            and annotations.get(item.func.attr)
        ]
        bindings = _bindings(function) if calls else {}
        for call in calls:
            callee = call.func.attr if isinstance(call.func, ast.Attribute) else ""
            annotated = annotations[callee]
            arguments = [
                (parameter, argument)
                for parameter, argument in _arguments(call, positional[callee])
                if parameter in annotated
            ]
            for parameter, argument in arguments:
                given = _given(argument, annotated[parameter], name, own, bindings)
                found.update(dict.fromkeys((callee, parameter, *item) for item in given))
    return tuple(entry for entry in found if not _to_itself(entry))


def _to_itself(entry: Passed) -> bool:
    """Whether an entry says that a method passes a parameter on to itself."""
    method, parameter, path, kind = entry
    return path == (FORWARD, method) and kind == parameter


def _given(
    argument: ast.expr,
    annotation: ast.expr,
    method: str,
    own: set[str],
    bindings: Mapping[str, list[ast.expr]],
) -> Iterator[tuple[tuple[str, ...], str]]:
    """What an argument gives an annotated parameter: ``(path, kind)`` of :data:`Passed`."""
    if isinstance(argument, ast.Name) and argument.id in own:
        yield (FORWARD, method), argument.id  # the method's own parameter, as it got it
        return
    for path, kind in _displays(argument, bindings, (), frozenset()):
        named = _annotated(annotation, path)
        if named in SEQUENCES and kind in GIVEN and kind != named:
            yield path, kind


def _names_sequence(annotation: ast.expr) -> bool:
    """Whether an annotation names a container that another display does not build."""
    if isinstance(annotation, ast.Constant):
        text = annotation.value
        return isinstance(text, str) and any(name in text for name in SEQUENCES)
    return any(isinstance(item, ast.Name) and item.id in SEQUENCES for item in ast.walk(annotation))


def _parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    """The parameters of a method but its receiver."""
    arguments = function.args
    return [*(arguments.posonlyargs + arguments.args)[1:], *arguments.kwonlyargs]


def _arguments(call: ast.Call, positional: list[str]) -> Iterator[tuple[str, ast.expr]]:
    """The arguments of a call of a method, each with the parameter it is given for."""
    for position, argument in enumerate(call.args):
        if isinstance(argument, ast.Starred):
            break
        if position < len(positional):
            yield positional[position], argument
    for keyword in call.keywords:
        if keyword.arg is not None:
            yield keyword.arg, keyword.value


def _bindings(function: ast.AST) -> dict[str, list[ast.expr]]:
    """The values a function binds its names to (``name = value``)."""
    bound: dict[str, list[ast.expr]] = {}
    for item in ast.walk(function):
        if isinstance(item, ast.Assign):
            targets, value = item.targets, item.value
        elif isinstance(item, ast.AnnAssign | ast.NamedExpr) and item.value is not None:
            targets, value = [item.target], item.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bound.setdefault(target.id, []).append(value)
    return bound


def _displays(
    node: ast.expr,
    bindings: Mapping[str, list[ast.expr]],
    path: tuple[str, ...],
    seen: frozenset[str],
) -> Iterator[tuple[tuple[str, ...], str]]:
    """The containers a value is built of, by syntax: ``(path, kind)`` for each."""
    if len(path) > MAX_PATH:
        return
    if isinstance(node, ast.Name):
        if node.id not in seen:
            for value in bindings.get(node.id, ()):
                yield from _displays(value, bindings, path, seen | {node.id})
        return
    if isinstance(node, ast.IfExp):
        yield from _displays(node.body, bindings, path, seen)
        yield from _displays(node.orelse, bindings, path, seen)
        return
    kind = DISPLAYS.get(type(node))
    if kind is None:
        return
    yield path, kind
    inner: list[tuple[str, ast.expr]] = []
    if isinstance(node, ast.Dict):
        inner = [(VALUE, value) for value in node.values]
    elif isinstance(node, ast.DictComp):
        inner = [(VALUE, node.value)]
    elif isinstance(node, ast.Tuple):
        if not any(isinstance(item, ast.Starred) for item in node.elts):
            inner = [(str(index), item) for index, item in enumerate(node.elts)]
    elif isinstance(node, ast.List | ast.Set):
        inner = [(ITEM, item) for item in node.elts if not isinstance(item, ast.Starred)]
    elif isinstance(node, ast.ListComp | ast.SetComp):
        inner = [(ITEM, node.elt)]
    for step, value in inner:
        yield from _displays(value, bindings, (*path, step), seen)


def _annotated(annotation: ast.expr, path: tuple[str, ...]) -> str | None:
    """The container an annotation names at a path (``dict[int, set[str]]``, ``v``: ``set``).

    By the name it is written with; ``None`` where the annotation names no container
    there, or one that the path does not go through.
    """
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return None
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotated(annotation.left, path) or _annotated(annotation.right, path)
    if isinstance(annotation, ast.Name):
        return annotation.id if not path else None
    if not isinstance(annotation, ast.Subscript) or not isinstance(annotation.value, ast.Name):
        return None
    name = annotation.value.id
    if not path:
        return name
    items = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
    step = path[0]
    variadic = any(isinstance(item, ast.Constant) and item.value is ... for item in items)
    inner: ast.expr | None = None
    if step == VALUE:
        inner = items[1] if name == "dict" and len(items) == 2 else None
    elif step == ITEM:
        if name in SEQUENCES or (name == "tuple" and variadic):
            inner = items[0]
    elif name == "tuple" and not variadic and int(step) < len(items):
        inner = items[int(step)]
    return _annotated(inner, path[1:]) if inner is not None else None
