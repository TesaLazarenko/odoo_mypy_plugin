"""``odoo-typing-stubs``: ``.pyi`` stubs for editors (Pyright, PyCharm, VS Code).

The plugin works only inside mypy; in an editor ``self.env["res.partner"]`` stays
``BaseModel``. This command writes, from the same registry the plugin uses, as one
addon sees it (``--addon``, the "top" addon of the project)::

    <output>/odoo_typing_models.pyi       a class per model, its fields declared
    <output>/odoo/api.pyi                 Environment.__getitem__ overloads (Odoo 17-18)
    <output>/odoo/orm/environments.pyi    the same for Odoo 19+

Each model class has the classes of its registry class as bases (like the plugin's
view class), and declares every field with the type reading it gives (a relational
field: the comodel's class), plus a ``<model>__vals`` ``TypedDict`` for ``create`` /
``write``. The ``Environment`` module is mypy's ``stubgen`` output of the real
module (a stub replaces the whole module) with one ``Literal["<model>"]`` overload of
``__getitem__`` per model.

Do not put the output on mypy's ``mypy_path``: it would shadow the real modules for the
plugin. Point the editor at it (``stubPath`` in ``pyrightconfig.json``, a stub root in
PyCharm), and run the command again after adding addons, models or fields.
"""

from __future__ import annotations

import argparse
import keyword
import re
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final

from odoo_typing.compat import profile_for
from odoo_typing.config import ConfigError, Settings, load_settings
from odoo_typing.registry import Registry
from odoo_typing.scanner import FieldInfo

__all__ = ["MODELS_MODULE", "main", "model_class_name", "models_stub", "write_stubs"]

MODELS_MODULE: Final = "odoo_typing_models"
# Qualified names: a field may be called ``float`` or ``datetime`` in the class body.
FALSE = "typing.Literal[False]"
TEXT = f"builtins.str | {FALSE}"
VALUES: Final = {
    "Boolean": "builtins.bool",
    "Integer": "builtins.int",
    "Many2oneReference": "builtins.int",
    "Id": "builtins.int",
    "Float": "builtins.float",
    "Monetary": "builtins.float",
    "Char": TEXT,
    "Text": TEXT,
    "Html": TEXT,
    "Selection": TEXT,
    "Date": f"_datetime.date | {FALSE}",
    "Datetime": f"_datetime.datetime | {FALSE}",
    "Binary": f"builtins.bytes | {FALSE}",
    "Image": f"builtins.bytes | {FALSE}",
    "Reference": "odoo.models.BaseModel | None",
}
ANY: Final = "typing.Any"
# The real classes of a model disagree with each other at times (Odoo does not mind).
SILENCED: Final = (
    '# mypy: disable-error-code="misc, assignment"\n'
    "# pyright: reportIncompatibleVariableOverride=false, reportIncompatibleMethodOverride=false"
)
ENVIRONMENT: Final = re.compile(r"^class Environment\b", re.MULTILINE)
UNTYPED_ALIAS: Final = re.compile(r"^(?P<name>\w+): typing\.TypeAlias$", re.MULTILINE)
RELATIONAL: Final = frozenset({"Many2one", "One2many", "Many2many"})
GETITEM: Final = re.compile(
    r"^(?P<indent>\s+)def __getitem__\(self, model_name[^)]*\)[^\n]*$", re.M
)


def model_class_name(model: str) -> str:
    """The stub class of a model (``res.partner`` → ``res_partner``)."""
    name = model.replace(".", "_")
    return f"{name}_" if keyword.iskeyword(name) else name


def _value(info: FieldInfo, classes: Mapping[str, str]) -> str:
    if info.field_type in RELATIONAL:
        comodel = info.comodel if "comodel" not in info.dynamic else None
        return classes.get(comodel or "", "odoo.models.BaseModel")
    return VALUES.get(info.field_type, ANY)


def _vals_value(info: FieldInfo, classes: Mapping[str, str]) -> str:
    if info.field_type == "Many2one":
        return f"builtins.int | {_value(info, classes)} | {FALSE} | None"
    if info.field_type in {"One2many", "Many2many"}:
        return f"builtins.list[{ANY}]"
    if info.field_type == "Reference":  # ``"model,id"``, a record, or empty either way
        return f"builtins.str | odoo.models.BaseModel | {FALSE} | None"
    value = _value(info, classes)
    return value if value == ANY else f"{value} | None"


def models_stub(registry: Registry, addon_name: str) -> tuple[str, list[str]]:
    """The ``odoo_typing_models`` stub, and the models it declares, as an addon sees them."""
    models = registry.models(addon_name)
    classes = {model: model_class_name(model) for model in models}
    imports: set[str] = set()
    blocks: list[str] = []
    for model in models:
        view = registry.view_class(model, addon_name)
        if view is None:
            continue
        bases = [view.tip] if view.tip is not None else list(view.bases)
        imports.update(base.module for base in bases)
        names = [f"{base.module}.{base.class_name}" for base in bases] or ["odoo.models.BaseModel"]
        fields = sorted(
            (name, info)
            for name, info in registry.fields(model, addon_name).items()
            if name.isidentifier() and not keyword.iskeyword(name)
        )
        lines = [
            f"class {classes[model]}({', '.join(names)}):",
            f"    _name: builtins.str  # {model!r}",
        ]
        lines += [f"    {name}: {_value(info, classes)}" for name, info in fields]
        lines += ["", f"class {classes[model]}__vals(typing.TypedDict, total=False):"]
        lines += [f"    {name}: {_vals_value(info, classes)}" for name, info in fields] or [
            "    pass"
        ]
        blocks.append("\n".join(lines))
    header = [
        f'"""Odoo models as addon {addon_name!r} sees them (generated by odoo-typing-stubs)."""',
        SILENCED,
        "",
        "import builtins",
        "import datetime as _datetime",
        "import typing",
        "",
        "import odoo.models",
        *(f"import {module}" for module in sorted(imports)),
    ]
    return "\n".join(header) + "\n\n\n" + "\n\n\n".join(blocks) + "\n", models


def _environment_stub(source: str, models: Iterable[str]) -> str:
    """Add ``Literal["<model>"]`` overloads of ``Environment.__getitem__`` to a stub."""
    start = ENVIRONMENT.search(source)
    match = GETITEM.search(source, start.end()) if start is not None else None
    if match is None:
        raise ValueError("no Environment.__getitem__ in the generated stub")
    indent = match["indent"]
    overloads = [
        f"{indent}@overload\n{indent}def __getitem__(self, model_name: Literal[{model!r}]) "
        f"-> {MODELS_MODULE}.{model_class_name(model)}: ..."
        for model in models
    ]
    fallback = (  # odoo.models re-exports BaseModel in every version
        f"{indent}@overload\n{indent}def __getitem__(self, model_name: str) "
        "-> odoo.models.BaseModel: ..."
    )
    body = "\n".join([*overloads, fallback])
    header = f"from typing import Literal, overload\nimport {MODELS_MODULE}\nimport odoo.models\n"
    stub = header + source[: match.start()] + body + source[match.end() :]
    # stubgen leaves a TypeAlias it cannot spell without a value (IdType = int | NewId).
    return UNTYPED_ALIAS.sub(r"\g<name>: typing.TypeAlias = typing.Any", stub)


def _stubgen(module: str, odoo_path: Path) -> str:
    """Mypy's ``stubgen`` stub of a module of the ``odoo`` package (parsed, not imported).

    The file is given by path, not by module name: another ``odoo`` may be importable.
    """
    from mypy import stubgen

    parts = module.split(".")[1:]  # inside the odoo package
    source = odoo_path.joinpath(*parts).with_suffix(".py")
    if not source.is_file():
        source = odoo_path.joinpath(*parts, "__init__.py")
    with tempfile.TemporaryDirectory() as directory:
        arguments = ["--parse-only", "--include-private", "--quiet", "-o", directory, str(source)]
        stubgen.generate_stubs(stubgen.parse_options(arguments))
        found = list(Path(directory).rglob("*.pyi"))
        if len(found) != 1:
            raise ValueError(f"stubgen wrote {len(found)} stubs for {source}")
        return found[0].read_text()


def write_stubs(settings: Settings, addon_name: str, output: Path) -> int:
    """Write the stubs (see the module docstring); return the number of models.

    Raises:
        ConfigError: For an unknown addon or an Odoo version without a profile.
    """
    profile = profile_for(settings.odoo_version) if settings.odoo_version else None
    if profile is None:
        raise ConfigError("odoo_version is not set or not supported")
    registry = Registry(settings)
    if addon_name not in registry.addons.addons:
        raise ConfigError(f"unknown addon {addon_name!r}")
    models_source, models = models_stub(registry, addon_name)
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{MODELS_MODULE}.pyi").write_text(models_source)
    module = profile.environment.rpartition(".")[0]
    environment = _stubgen(module, settings.odoo_path)
    target = (output / Path(*module.split("."))).with_suffix(".pyi")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_environment_stub(environment, models))
    return len(models)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point of ``odoo-typing-stubs``.

    Returns:
        0 on success, 2 on a configuration error.
    """
    parser = argparse.ArgumentParser(
        prog="odoo-typing-stubs", description="Write .pyi stubs of the Odoo models for editors."
    )
    parser.add_argument("--config", default="pyproject.toml", help="default: %(default)s")
    parser.add_argument("--addon", required=True, help="the addon whose view the stubs show")
    parser.add_argument("--output", default="typings", help="default: %(default)s")
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if the stubs in --output are out of date"
    )
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config, require_overlay=False)
        if args.check:
            return _check(settings, args.addon, Path(args.output))
        count = write_stubs(settings, args.addon, Path(args.output))
    except (ConfigError, OSError, ValueError) as exc:
        print(f"odoo-typing-stubs: error: {exc}", file=sys.stderr)
        return 2
    print(f"odoo-typing-stubs: wrote {args.output} ({count} models seen from {args.addon})")
    return 0


def _check(settings: Settings, addon_name: str, output: Path) -> int:
    """Whether the stubs in ``output`` are what the command would write now (0) or not (1)."""
    with tempfile.TemporaryDirectory() as directory:
        fresh = Path(directory)
        write_stubs(settings, addon_name, fresh)
        stale = [
            path.relative_to(fresh)
            for path in sorted(fresh.rglob("*.pyi"))
            if not (output / path.relative_to(fresh)).is_file()
            or (output / path.relative_to(fresh)).read_text() != path.read_text()
        ]
    for path in stale:
        print(f"odoo-typing-stubs: {output / path} is out of date")
    if stale:
        print("odoo-typing-stubs: run odoo-typing-stubs again")
        return 1
    print(f"odoo-typing-stubs: {output} is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
