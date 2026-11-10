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

Models are resolved **per addon**: a module sees only the models of the addons in its
`depends` closure, just as Odoo loads them.

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

**Note on `mypy_path`:** if Odoo is installed as a deb/pip package, don't put the whole
`site-packages`/`dist-packages` directory on `mypy_path`. Create a directory that holds
only a symlink instead: `mkdir .odoo-root && ln -s /usr/lib/python3/dist-packages/odoo .odoo-root/odoo`.
For a git checkout, use the checkout root.

```bash
mypy addons/my_addon
```

## How it works

- **Registry** ([registry.py](src/odoo_typing/registry.py), [scanner.py](src/odoo_typing/scanner.py)):
  a static AST scan. It reads manifests and follows each addon's `__init__.py` imports, then
  collects the classes with `_name`/`_inherit` in registration order. It never imports Odoo
  or touches a database.
- **`get_additional_deps`**: makes mypy load the modules that define the models a file
  uses, so they are analyzed first.
- **`get_customize_class_mro_hook`**: gives each model class the previous classes of the
  same model (and of its `_inherit` mixins) as extra bases. This mirrors Odoo's runtime
  class chain, so `self.<field>` and `super()` resolve across modules.
- **`get_base_class_hook`**: gives `_inherit`, `_name`, `_order`, ... the type that
  `BaseModel` declares, so `_inherit = "x"` does not clash with `_inherit = ["x", "y"]`
  in a base class.
- **`Environment.__getitem__`**, **`Many2one/One2many/Many2many(...)`** and
  **`__get__`** hooks resolve model names and bind comodels.

## Development

```bash
uv sync
uv run pytest            # needs Odoo installed; set ODOO_PATH=/path/to/odoo if not auto-found
uv run ruff check src tests && uv run mypy
```

The end-to-end tests run the real mypy on throw-away addons against the installed Odoo.
See [docs/odoo-ls-cli.md](docs/odoo-ls-cli.md) for how this project compares with odoo-ls
and pylint-odoo on the probe addon in `tests/fixtures/addons/probe_mod`.

## Not yet supported

- `vals` dicts in `create()`/`write()`: typed keys and values (next milestone)
- `_inherit = "base"` (methods added to every model, e.g. `_phone_format`)
- relational fields declared only with `related=` (no comodel argument)
- `_inherits` delegation, domains, `@api.depends` paths
- when unrelated addons both extend a model, `env[...]` picks one of the classes instead of merging them
