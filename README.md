# odoo-typing

mypy plugin that understands the Odoo ORM (developed and tested against Odoo 18).

Plain mypy treats `env["res.partner"]` as `BaseModel`. It cannot see fields that other
modules add through `_inherit`, and it reads every relational field as `Any`. With this
plugin:

| Code | Plain mypy | With the plugin |
|---|---|---|
| `self.env["res.partner"]` | `BaseModel` | `Partner`, including the `_inherit` extensions visible from the addon (merged when unrelated addons extend it) |
| `self.env["res.partnr"]` | — | `error: Unknown Odoo model "res.partnr"  [odoo-model]` |
| `self.name` inside `_inherit = "res.partner"` | `has no attribute "name"` | `str \| Literal[False]` |
| `fields.Many2one("res.partner")` | `Need type annotation` | `Many2one[Partner]` |
| `self.partner_id` / `self.line_ids` | `Any` | the comodel recordset, with the extensions the reading addon sees |
| `self.x_score + self.name` | silent (`Any`) | `Unsupported operand types for + ("int" and "str")` |
| `_inherit = "sale.order"` without `sale` in `depends` | silent | `Odoo model "sale.order" in _inherit is defined in addon "sale", which is not in the dependencies of "my_addon"  [odoo-model-depends]` |
| `self.env["sale.order"]` without `sale` in `depends` | `BaseModel` | `Odoo model "sale.order" is defined in addon "sale", which is not in the dependencies of "my_addon"  [odoo-model-depends]`, then `Any` |
| `self._phone_format(...)` (added by `phone_validation` to `base`) | `has no attribute` | resolved on every model of an addon that depends on `phone_validation` |
| `self.sudo()`, `.with_context(...)`, `.filtered(...)`, `.search([])`, `.copy()` | `Any` | the receiver's model (also through unannotated overrides of `create`, `copy`, `search`...) |
| `self.env.user` / `self.env.company` | `Any` | `res.users` / `res.company` as the addon sees them |
| `line.product_id.uom_id` (`_inherits` of `product.template`) | silent (`Any`) | the `uom.uom` record: fields of `_inherits` (and `delegate=True`) parents are delegated, methods are not |

Models are resolved **per addon**: a module sees only the models of the addons in its
`depends` closure, just as Odoo loads them.

**Error codes.** `odoo-model`: a model name that no addon on the addons path defines (a
typo), an `_inherit` defined only after the class, an `_inherit` cycle.
`odoo-model-depends` (a sub-code of `odoo-model`): the model exists, but only in addons
outside the `depends` closure. In an `_inherit` that is a real error (Odoo cannot load
the class); in `env["..."]` or a comodel it is often optional-dependency code guarded by
`if "sale.order" in self.env:`, so it can be disabled on its own with
`disable_error_code = ["odoo-model-depends"]` (typos are still reported); `env[...]`
is `Any` there. `# type: ignore[odoo-model]` covers both codes.

**Code outside any addon** (scripts, and Odoo's own `odoo/*.py` modules, which mypy always
analyzes) has no `depends` closure, so the plugin leaves it alone: `env["..."]` keeps
Odoo's declared type `BaseModel`, and model names are not checked there. This keeps such
files free (resolving them would mean scanning every installed addon). Put the code in
an addon to get real model types.

## Setup

```toml
# pyproject.toml of the project that contains your addons
[tool.mypy]
plugins = ["odoo_typing.plugin"]
# 1) a directory that contains the `odoo` package (see the note below)
# 2) your addons directories, so `my_addon.models.x` can be imported
mypy_path = [".odoo-root", "addons"]
check_untyped_defs = true            # Odoo methods are rarely annotated

[[tool.mypy.overrides]]
module = ["odoo", "odoo.*"]
follow_imports = "silent"            # don't report errors inside Odoo itself

[tool.odoo_typing]
odoo_path = ".odoo-root/odoo"        # dir with release.py; auto-detected if omitted
addons_path = ["addons"]             # relative to pyproject.toml
```

With `mypy.ini`, `.mypy.ini` or `setup.cfg`, use an `[odoo_typing]` section; separate
`addons_path` entries with commas or newlines:

```ini
[mypy]
plugins = odoo_typing.plugin
mypy_path = .odoo-root:addons

[mypy-odoo.*]
follow_imports = silent

[odoo_typing]
odoo_path = .odoo-root/odoo
addons_path = addons, ../enterprise
```

An INI config without that section uses the `[tool.odoo_typing]` table of a
`pyproject.toml` next to it, if there is one. Relative paths are relative to the file
that contains them. Without `odoo_path`, the plugin looks for an `odoo` package (a
directory with `release.py`) on `mypy_path`, then tries to import `odoo` in mypy's own
interpreter, which usually fails in a virtualenv.

A configuration error (Odoo not found, a missing directory, a malformed table) stops mypy
with exit status 2 and one line on stderr, e.g.
`odoo-typing: error: addons_path entries are not directories: /src/missing`.

Each `addons_path` entry must be a directory that *contains* addons (subdirectories with
a `__manifest__.py`), like Odoo's `--addons-path`. Listing an addon directory itself, or a
directory without any addon, is a configuration error.

**Note on `mypy_path`:** if Odoo is installed as a deb/pip package, don't put the whole
`site-packages`/`dist-packages` directory on `mypy_path`. Create a directory that holds
only a symlink instead: `mkdir .odoo-root && ln -s /usr/lib/python3/dist-packages/odoo .odoo-root/odoo`.
For a git checkout, use the checkout root.

```bash
mypy addons/my_addon
```

## How it works

- **Registry** ([registry.py](src/odoo_typing/registry.py), [scanner.py](src/odoo_typing/scanner.py)):
  a static AST scan that never imports Odoo or touches a database. It mirrors Odoo 18's
  loading rules:
  - addons are directories with a `__manifest__.py` (or the deprecated `__openerp__.py`),
    named after their entry in the addons path (a symlink may point to any directory);
  - load order is Odoo's module graph: `base`, then by dependency depth, then by name;
  - within an addon, classes are ordered as Python creates them: the scan replays the
    `__init__.py` imports statement by statement, so an imported module's classes come at
    the import, and imports or classes inside `try:`/`if` blocks count (not
    `if TYPE_CHECKING:` ones);
  - model names follow `MetaModel`: `_name = _description = "x"`, `_name = CONST`,
    `_inherit = _name` and f-strings of module constants are understood; several
    `_inherit` entries without `_name` define a new model named after the class; a
    `_name` that cannot be computed statically (for example in a loop) is skipped;
  - every model inherits `base` last, like `_build_model` does, so methods that addons add
    with `_inherit = "base"` exist on `self` and on extensions of other models;
  - a model's class chain is built like Odoo's registry class (`_build_model`'s
    `LastOrderedSet` of bases, linearized with C3): the model's earlier classes, then the
    registry classes of its `_inherit` parents, `base` last. A parent's registry class
    holds *all* its classes that the addon sees, also later ones: Odoo extends it in
    place, so a method that `sms` adds to `mail.thread` is on `project.task` in every
    addon that depends on both `project` and `sms`.

  Everything is lazy: manifests are read on the first question (a
  fully cached mypy run reads the stored index instead, see
  [Incremental mode](#incremental-mode-and-the-daemon)), and an addon is scanned only when a file whose
  `depends` closure contains it needs a model. Sources are parsed as bytes, so a UTF-8 BOM
  or a `# -*- coding: ... -*-` cookie works; a file that cannot be parsed is reported once
  on stderr (`odoo-typing: warning: cannot parse ...`), since its models are then missing.
- **`get_additional_deps`**: makes mypy load (and analyze first) the modules of every
  class a file's models are made of: the class chains of its own classes, the models
  named in `env["..."]` and comodel literals, and the comodels of the relational fields
  its classes inherit (so `self.partner_id.<field added by a dependency>` resolves).
- **`get_customize_class_mro_hook`**: gives each model class the MRO of its registry
  class, computed by the registry; it is never copied from a base class, so it does not
  depend on the order in which mypy analyzes the classes of an import cycle. When a class
  of the chain is not analyzed yet, mypy is asked to analyze the class again (classes
  outside the run, or still missing on mypy's final pass, are left out, and unknown
  attributes then give `Any`). Python's own MRO of the written bases is respected; when
  it contradicts the `_inherit` order (Odoo cannot load such a class either), all bases
  are kept and a note says so. It also reports, with
  code `odoo-model`, what Odoo would refuse to load: an `_inherit` entry that no addon in
  the `depends` closure defines (naming the addon that does, if any, with code
  `odoo-model-depends`: the fix is usually a missing `depends`), one that is only
  defined after the class, and `_inherit` cycles (whose edges are then ignored). Finding
  the defining addon outside the closure is one text search over all addons (about
  0.3 s, only in a run that reports an unknown model name).
- **`_inherits`** and **`Many2one(..., delegate=True)`**: once a model class's body is
  analyzed, the fields (not the methods) of the delegated models' registry classes, as
  the class's addon sees them, are added to it unless the class already has the name,
  like Odoo's `_add_inherited_fields`. `_inherits` of mixins count (`mail.alias.mixin`).
  The field variables are shared with the parent's classes, so their types are inferred
  once. A model whose delegated parent is extended outside the tip's view gets a merged
  `env[...]` class.
- **Decorators and `-> Self` methods**: `@api.private` and `@api.returns(...)` have no
  annotations, which made `sudo`, `with_context`, `filtered`, `search`, `copy`...
  `Any`. A function hook keeps the decorated signature (also for `api.model`,
  `api.readonly`, `api.model_create_multi`). An override of `create`, `copy`, `search`,
  `browse`, `sudo`, `with_*`, `filtered`, `sorted`, `exists` or `ensure_one` written
  without annotations returns the receiver's model; `create` also accepts one dict,
  as `model_create_multi` does at runtime.
- **`env.user` / `env.company` / `env.companies`**: records of `res.users` /
  `res.company` as the file's addon sees them; `env[NAME]` accepts a `Final` string
  constant (a plain variable stays `BaseModel`).
- **`get_base_class_hook`**: private class attributes of model classes (`_inherit`,
  `_order`, `_rec_name`, `_sequence_index`...) get the type a model base class declares
  with an annotation (`BaseModel._inherit: str | list[str] | ...`), `Any` otherwise. So
  `_inherit = "x"` does not clash with `_inherit = ["x", "y"]` in a base class, nor
  `_sequence_index = "journal_id"` with `sequence.mixin`'s `False`, nor a property with a
  mixin's string. `_name` is a `str`; private fields (`_x = fields.Char()`) keep their type.
- **`Environment.__getitem__`**: `env["x"]` is the last class of `x` when its MRO is the
  whole registry class as the addon sees it. Otherwise (unrelated addons extend `x`, or
  its mixins are extended outside that class's view) it is a merged class named
  `<subclass of "x" seen from "my_addon">` (shown as `"x"` in messages), stored in the
  checked module like mypy's own ad hoc intersections, so the incremental cache keeps it.
- **`Many2one/One2many/Many2many(...)`** declare the field with the class that defines
  the comodel (the same class in every addon, so redeclaring a field elsewhere is
  compatible); **`__get__`** and an attribute hook resolve the comodel again when the
  field is read, from the reading addon (or the record's own class, if the reading
  module does not load all the comodel's classes), so the result has every extension the
  reader sees. `Model.field` (read on the class) is the field itself.

## Incremental mode and the daemon

mypy's cache only knows a module's own source and imports, but the plugin's result for a
file also depends on its addon's `depends` closure. So the plugin gives mypy, for every
module in an addon (`report_config_data`), a digest of that closure: each addon's name,
path, manifest `depends` and what its scan found (model classes, `_name`/`_inherit`,
registration order, comodels, model names used in `env[...]`), plus the plugin's version.
When a dependency gains an `_inherit` extension, a manifest changes `depends`, an
extension module is deleted or moved, or `addons_path` changes, mypy checks the affected
modules again instead of replaying stale errors. Editing a method body changes no
digest: mypy's own dependency tracking handles it.

The digests are kept in `<cache_dir>/odoo_typing/`, together with `stat` stamps (mtime
and size) of the manifests, addons directories and scanned files they come from. A run
in which nothing changed reads these small files and calls `stat` on the files they
list, but parses nothing.

Limits: a structural change (a new class, `_inherit`, comodel or `env["..."]` literal) in
an addon makes mypy recheck every module of the addons that depend on it, not only the
ones that use the changed model. The text of the "defined in addon ..., which is not in
the dependencies" message can stay outdated when an addon *outside* the closure is
added, until the file is checked again (the error itself is right).

**dmypy:** the daemon keeps one plugin instance. Before the files of each update are
processed, the plugin compares the stamps of everything its registry has read (addons
roots, manifests, scanned files) with the disk and starts over with a fresh registry if
any changed. The daemon only rechecks files whose source changed (and their
dependents), and the plugin reads its settings once: after editing only a manifest, or
the `[tool.odoo_typing]`/`[odoo_typing]` settings, run `dmypy restart`.

## Development

```bash
uv sync
uv run pytest            # needs Odoo installed; set ODOO_PATH=/path/to/odoo if not auto-found
uv run ruff check src tests tools && uv run ruff format --check src tests tools && uv run mypy
```

The end-to-end tests run the real mypy on throw-away addons against the installed Odoo.
They fail when mypy crashes (`INTERNAL ERROR`, exit status 2), not only on wrong messages.

### Regression benchmark

[tools/bench/bench.py](tools/bench/bench.py) runs mypy on real Odoo core addons (default:
`crm sale account website_sale project_sms sale_stock`, each as its own target), with and
without the plugin. For every addon it records the cold and warm wall time (fresh cache,
then the same cache again), the peak RSS, the exit status and the normalized errors. The
*plugin-only* errors are those that appear only with the plugin.

```bash
uv run python tools/bench/bench.py --label baseline              # measure and save
uv run python tools/bench/bench.py --label wp1 --compare baseline
uv run python tools/bench/bench.py --compare-only wp1 --compare baseline   # no mypy run
```

- `--odoo-root DIR`: a directory that holds only an `odoo` symlink (see the note above).
- `--bench-dir DIR`: where `<label>.json`, the generated configs (`config/`) and the caches
  (`cache/<label>/<addon>`) go.
- `--addons a b ...`, `--jobs N` (addons at once; 1 gives the most stable timings).
- The run without the plugin is saved once as `noplugin.json` and reused. Pass
  `--refresh-baseline` after upgrading mypy or Odoo.

`--compare` prints per-addon counts and times, then the plugin-only errors that are NEW or
GONE compared with the earlier label. They are grouped by error code and message pattern
(quoted names replaced by `"_"`), with 3 examples per group. The script exits with status 1
if any mypy run crashed.
See [docs/odoo-ls-cli.md](docs/odoo-ls-cli.md) for how this project compares with odoo-ls
and pylint-odoo on the probe addon in `tests/fixtures/addons/probe_mod`.

## Not yet supported

- `vals` dicts in `create()`/`write()`: typed keys and values (next milestone)
- relational fields declared only with `related=` (no comodel argument)
- domains, `@api.depends` paths; assignments to fields (`record.partner_id = "x"`) are
  not checked (`Field.__set__` is untyped)
- fields a delegated parent itself delegates (`_inherits` of an `_inherits` parent)
- the log-access fields Odoo adds at runtime (`create_uid`, `create_date`, `write_uid`,
  `write_date`) are not declared, so reading them is an `attr-defined` error, as in plain
  mypy
- `@api.onchange`, `@api.ondelete`, `@api.depends_context` methods stay `Any`
  (untyped decorators); `super().create(...)` in an unannotated override is `Any`
- `self` is the class being written, so inside a class `self` sees the earlier classes of
  its model, not later ones of the same addon (`env[...]` does see them). The classes of
  one model are distinct types: a variable that first holds `env["x"]` cannot later be
  assigned `self` of an earlier class of `x` (an `assignment` error)
