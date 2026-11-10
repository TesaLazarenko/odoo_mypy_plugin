# odoo-typing

mypy plugin that understands the Odoo ORM (developed and tested against Odoo 18).

Plain mypy treats `env["res.partner"]` as `BaseModel`. It cannot see fields that other
modules add through `_inherit`, and it reads every relational field as `Any`. With this
plugin:

| Code | Plain mypy | With the plugin |
|---|---|---|
| `self.env["res.partner"]` | `BaseModel` | `Partner`, including the `_inherit` extensions visible from the addon |
| `self.env["res.partnr"]` | — | `error: Unknown Odoo model "res.partnr"  [odoo-model]` |
| `self.name` inside `_inherit = "res.partner"` | `has no attribute "name"` | `str \| Literal[False]` |
| `fields.Many2one("res.partner")` | `Need type annotation` | `Many2one[Partner]` |
| `self.partner_id` / `self.line_ids` | `Any` | the comodel recordset |
| `self.x_score + self.name` | silent (`Any`) | `Unsupported operand types for + ("int" and "str")` |
| `_inherit = "sale.order"` without `sale` in `depends` | silent | `Odoo model "sale.order" in _inherit is defined in addon "sale", which is not in the dependencies of "my_addon"` |
| `self._phone_format(...)` (added by `phone_validation` to `base`) | `has no attribute` | resolved on every model of an addon that depends on `phone_validation` |

Models are resolved **per addon**: a module sees only the models of the addons in its
`depends` closure, just as Odoo loads them.

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
    with `_inherit = "base"` exist on `self` and on extensions of other models. Everything is lazy: manifests are read on the first question (a
  fully cached mypy run reads none), and an addon is scanned only when a file whose
  `depends` closure contains it needs a model. Sources are parsed as bytes, so a UTF-8 BOM
  or a `# -*- coding: ... -*-` cookie works; a file that cannot be parsed is reported once
  on stderr (`odoo-typing: warning: cannot parse ...`), since its models are then missing.
- **`get_additional_deps`**: makes mypy load the modules that define the models a file
  uses, so they are analyzed first.
- **`get_customize_class_mro_hook`**: gives each model class the previous classes of the
  same model (and of its `_inherit` mixins) as extra bases. This mirrors Odoo's runtime
  class chain, so `self.<field>` and `super()` resolve across modules. It also reports, with
  code `odoo-model`, what Odoo would refuse to load: an `_inherit` entry that no addon in
  the `depends` closure defines (naming the addon that does, if any: the fix is usually a
  missing `depends`), one that is only defined after the class, and `_inherit` cycles.
  Finding the defining addon outside the closure is a text search over all addons
  (about 0.25 s, only when such an error exists).
- **`get_base_class_hook`**: gives `_inherit`, `_name`, `_order`, ... the type that
  `BaseModel` declares, so `_inherit = "x"` does not clash with `_inherit = ["x", "y"]`
  in a base class.
- **`Environment.__getitem__`**, **`Many2one/One2many/Many2many(...)`** and
  **`__get__`** hooks resolve model names and bind comodels.

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
- `env["res.partner"]` is the class as seen from the addon that defined it: extensions
  of `base` or of mixins from addons it does not depend on are not part of it (they are on
  `self` in your own `_inherit` classes)
- relational fields declared only with `related=` (no comodel argument)
- `_inherits` delegation, domains, `@api.depends` paths
- when unrelated addons both extend a model, `env[...]` picks one of the classes instead of merging them
