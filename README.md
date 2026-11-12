# odoo-typing

mypy plugin that understands the Odoo ORM, for mypy 2.1-2.4. The target Odoo version is
set in the config (`odoo_version`); supported: Odoo 18, 19 and 20. Odoo 17 is planned
(see [ROADMAP.md](ROADMAP.md), «Версії Odoo»).

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
| `from odoo.addons.my_base.models.x import Y` (a custom addon) | `import-not-found`, `Any` | the real class, with the overlay root ([Setup](#setup)) |
| `line.product_id.uom_id` (`_inherits` of `product.template`) | silent (`Any`) | the `uom.uom` record: fields of `_inherits` (and `delegate=True`) parents are delegated, methods are not |
| `self.create_date`, `self.create_uid.name` | `has no attribute` | `datetime \| Literal[False]`, the `res.users` record: Odoo's log-access fields, unless `_log_access` / `_auto` is `False` |
| `fields.Char(related="partner_id.citty")`, `fields.Float(compute="_compute_totl")`, `@api.depends("partner_id.nmae")` | silent | `Odoo model "res.partner" has no field "citty" in related="partner_id.citty"; did you mean "city"?`, `Odoo model "res.partner" has no method "_compute_totl" ...`  (both `odoo-field-ref`) |
| `self.search([("x_scroe", "=", 1), ("state", "in", ["draft", "dun"])])`, `fields.Many2one("res.partner", domain="[('nme', '=', 'x')]")` | silent | `Odoo model "res.partner" has no field "x_scroe" in domain; did you mean "x_score"?`, `Value "dun" is not in the selection of field "state" ...` (both `odoo-domain`) |
| `self.create({"x_score": "oops", "nmae": "x"})` | silent | `Incompatible type for field "x_score" of Odoo model "res.partner" (expression has type "str", Integer field takes "int \| NewId \| Literal[False] \| None")  [odoo-vals-type]`, `Odoo model "res.partner" has no field "nmae"; did you mean "name"?  [odoo-vals-key]` |
| `self.write({"state": "cancelled", "partner_id": self.env.user})` | silent | `Value "cancelled" is not in the selection of field "state" ...`, `Incompatible records for field "partner_id" ... (expression has "res.users" records, Many2one field takes "res.partner" records)` (both `odoo-vals-type`) |

Models are resolved **per addon**: a module sees only the models of the addons in its
`depends` closure, just as Odoo loads them.

**Error codes.** `odoo-model`: a model name that no addon on the addons path defines (a
typo), an `_inherit` defined only after the class, an `_inherit` cycle.
`odoo-model-depends` (a sub-code of `odoo-model`): the model exists, but only in addons
outside the `depends` closure. In an `_inherit` that is a real error (Odoo cannot load
the class); in `env["..."]` or a comodel it may be optional-dependency code, so it can
be disabled on its own with `disable_error_code = ["odoo-model-depends"]` (typos are
still reported); `env[...]` is `Any` there. `# type: ignore[odoo-model]` covers both
codes. Code that runs only when the model is installed is not reported at all (with
either code) and its records are `Any`: the body of `if "sale.order" in self.env:` (also
`self.env.get("sale.order")`, `self.pool`, `env.registry`, inside `and`), the `else` of
`not in`, the rest of the block after `if "sale.order" not in self.env: return` (or
`raise` / `continue` / `break`), and `x if "sale.order" in self.env else y`.

`odoo-field-ref` covers the names in field definitions, which Odoo resolves when it loads
the registry: each field of a `related="a.b.c"` path (followed through the comodels,
and the related field must have the class of the path's last field), and the method
names of `compute=`, `inverse=`, `search=` and `selection=` (a string), looked up in the
declaring addon's view of the model. The paths of `@api.depends(...)` are checked the
same way; `@api.constrains(...)` and `@api.onchange(...)` take field names of the model
only (Odoo ignores a dotted name there). Not in abstract models, whose fields refer to
the models that use them. A relational field declared only with `related=` gets the
comodel of the path's last field. The string argument of `mapped("a.b")`,
`filtered("a.b")`, `sorted(...)` and `grouped("a")` must name existing fields too
(`grouped`, and `sorted` before Odoo 19, take one field name; `sorted` in 19+ takes an
order such as `"name desc, id"`); `mapped("partner_id.name")` is then
`list[str | Literal[False]]`, `mapped("line_ids")` the comodel's records, and
`grouped("partner_id")` a `dict` from the field's values to records.
`read(["name", "partner_id"])` and `search_read(domain, ["name"])` with a literal list
return a list of `TypedDict`s with `id` and those fields, typed as `read()` gives them
(`partner_id` is `tuple[int, str] | Literal[False]`, x2many fields `list[int]`); a
name that is not a field is an `odoo-field-ref` error.

`odoo-vals` covers the `vals` of `create()`, `write()` and `copy(default=...)`, the
vals inside x2many commands (`Command.create({...})`, `Command.update(id, {...})`,
`(0, 0, {...})`, checked against the comodel) and values assigned to fields
(`record.partner_id = "x"`): its sub-codes `odoo-vals-key`
(a key that is not a field of the model, as the addon sees it: fields of later addons
and of `_inherits` parents included) and `odoo-vals-type` (a value the field does not
take). The `default_<field>` keys of `records.with_context(...)` chained straight into
`.create(...)` are checked the same way (keywords, or a literal dict); a key of a
field the model lacks is reported only when it is a near miss of one (a typo), since
Odoo ignores it and code passes defaults for fields of optional addons. Only dict
displays written in the call are checked (for `create`, also a list of
them); a `vals` variable, `dict(...)`, `**spread` and computed keys are not. In
abstract models (mixins, `base`) keys are not checked, since their methods run on models
with more fields. What each field class takes follows Odoo's `convert_to_cache`
(`False` and `None` clear a field; `Many2one` takes an id, a record of its comodel or a
`(id, name)` pair; a `Selection` literal must be one of its keys, including
`selection_add` of the addon's view). A plain `bool` is taken wherever `False` is, since
`x = cond and value` and `min(..., default=False)` are typed `bool`. `Boolean`
(`bool(value)`), computed `Binary` fields that are not stored (widget data), `Json`,
`Properties` and custom field classes are not checked.

`odoo-domain` covers literal domains: the `domain` of `search()`, `search_count()`,
`search_read()`, `search_fetch()`, `filtered_domain()`, `read_group()`, `_read_group()`,
`_search()` (a list, or `Domain([...])` / `Domain(path, operator, value)` on Odoo 19+),
and the `domain=` of relational fields, checked against the comodel (a string domain
is evaluated by the client, so only its field paths and operators are checked, and
`parent.x` is skipped). Each term with a literal path is checked: its fields
(through the comodels; a date field may end with a date part such as
`date.year_number`), its operator (the operators of the configured Odoo version: `<>`
is gone in 20, `any!` is new in 19), `any` / `not any` sub-domains against the comodel,
and a string value of a `Selection` field with literal keys. Logical operators other
than `&`, `|`, `!` are errors. Terms built from variables, `(1, "=", 1)` and domains
of abstract models are not checked.

`odoo-xmlid` covers `env.ref("module.name")`: the XML id must be created by a data
file (`data` / `demo` in a manifest: `<record>`, `<template>`, `<menuitem>`,
`<act_window>`, `<report>`, the `id` column of `<model>.csv`) of an addon in the
`depends` closure, or be one Odoo makes itself (`module.model_res_partner`,
`module.field_res_partner__name`, `base.module_sale`); its sub-code
`odoo-xmlid-depends` is an id that only an addon outside the closure creates. The
result is the record's model as the addon sees it (`env.ref("base.main_company")` is a
`res.company` record), `| None` with `raise_if_not_found=False`, which also turns off
the check (the code expects the id may be missing), and so does a line under
`if env.ref("sale.x", raise_if_not_found=False):` for every id of `sale`. An id whose
module is not an addon of the closure is reported only when an addon outside it
creates it: ids made by Python code (`local_es.x` of a chart template, hooks) are not
in any data file.

**Code outside any addon** (scripts, and Odoo's own `odoo/*.py` modules, which mypy always
analyzes) has no `depends` closure, so the plugin leaves it alone: `env["..."]` keeps
Odoo's declared type `BaseModel`, and model names are not checked there. This keeps such
files free (resolving them would mean scanning every installed addon). Put the code in
an addon to get real model types.

## Setup

Odoo 18 makes an addon importable only as `odoo.addons.<name>`, and addons import each
other (and their own `tests.common`) that way: `from odoo.addons.my_base.models.x import Y`.
mypy can resolve these imports only from a `mypy_path` root in which `odoo/addons` holds
your addons next to Odoo's. `odoo-typing-root` (installed with the plugin) generates that
root, made of symlinks, from the `[tool.odoo_typing]` settings:

```toml
# pyproject.toml of the project that contains your addons
[tool.mypy]
plugins = ["odoo_typing.plugin"]
mypy_path = [".odoo-root"]           # the generated root, and nothing else of Odoo's
check_untyped_defs = true            # Odoo methods are rarely annotated

[[tool.mypy.overrides]]
module = ["odoo", "odoo.*"]
follow_imports = "silent"            # don't report errors inside Odoo (or unchecked addons)

[tool.odoo_typing]
odoo_version = 18                    # required: the Odoo version the checks target
odoo_path = "/usr/lib/python3/dist-packages/odoo"   # the real package (release.py); auto-detected if omitted
addons_path = ["addons"]             # relative to pyproject.toml
overlay_root = ".odoo-root"          # generated by odoo-typing-root
dynamic_env = "any"                  # env[name] with a non-literal name: "any" (default) or "base"
vals_ignore_keys = ["x_*"]           # vals keys never reported as unknown fields (globs)
literal_selection = false            # read Selection fields as Literal[<keys>] | Literal[False]
```

`odoo_version` is the single source of truth for the version: the plugin picks the
names of Odoo's classes for its hooks from it (Odoo 19 moved the ORM to `odoo.orm`). The
Odoo found at `odoo_path` is only compared with it, and a different major version gives
a warning. Without `odoo_version`, or with a version that is not supported yet, mypy
stops with a message (exit status 2).

**Odoo 19+**: `odoo` and `odoo.addons` have no `__init__.py` (namespace packages), so
mypy names a file inside them `odoo.addons.<name>...` only with
`explicit_package_bases = true` in `[tool.mypy]`. Set it with `overlay_root` (the plugin
warns if it is missing), and whenever you give mypy files under the Odoo tree. Odoo 19
declares relational fields `Field[BaseModel]`; the plugin makes `Many2one`, `One2many`
and `Many2many` generic in the comodel (`fields.Many2one[ResPartner]`), as Odoo 18
declares them.

**Odoo 20** has the names of Odoo 19 plus PEP 695 type parameters (`class Field[T]`), so
mypy must target Python 3.12 or newer (`python_version = "3.12"`, or run mypy on 3.12+);
with an older target the plugin stops with a message. Some addons ship migration scripts
(`migrations/<version>/pre-migrate.py`) that mypy sees as duplicate modules: add
`exclude = "/migrations/"` to `[tool.mypy]`.

Unknown keys in this table are a configuration error, so a typo such as `addon_path`
does not go unnoticed. `dynamic_env = "base"` keeps plain mypy's `BaseModel` for
`env[name]` when `name` is not a literal (or a `Final`/`Literal` str): every field read
on it is then an `attr-defined` error. The default `"any"` leaves such code unchecked.
`vals_ignore_keys` lists glob patterns (case-sensitive) of `vals` keys that are not
reported as unknown fields; the default `["x_*"]` covers fields created in the database
(manual and Studio fields). Their values are still checked when the code declares them.
`literal_selection = true` reads a `Selection` field whose keys are literal (including
the `selection_add` of the addon's view) as `Literal["draft"] | ... | Literal[False]`
rather than `str | Literal[False]`, so comparing it with a key that does not exist is
an error; it is off by default because assigning the value to a variable then fixes
the variable's type to those literals.

**Controllers.** `odoo.http.request` is a `Request` and `request.env` an `Environment`
(Odoo 18-19 build `request` with werkzeug, which mypy does not follow; Odoo 20 declares
`env` as possibly `None`), so `request.env["res.partner"]` is checked like `self.env`.

**Reading fields that Odoo leaves untyped.** `Binary` and `Image` read as
`bytes | Literal[False]` (Odoo 18-19 declare them without a value type; Odoo 20's own
type is kept), except computed fields that are not stored: Odoo uses those to hold any
value for a widget (a dict), so they stay `Any`. `Reference` reads as a record of the models of its literal `selection`
(`BaseModel` when the selection is a method), or `False`: Odoo types it as the `str`
of `Selection`, but the value is a record.

```bash
odoo-typing-root                     # (re)generate .odoo-root; again after adding/removing addons
mypy -p odoo.addons.my_addon         # or: mypy .odoo-root/odoo/addons/my_addon
```

The root contains `odoo/<entry>` symlinks to every entry of the Odoo package except
`addons/`, and `odoo/addons/<name>` symlinks to every core addon and every `addons_path`
addon (a core addon wins a name clash, as in Odoo). `--config FILE` and `--output DIR`
override the defaults (`pyproject.toml`, `overlay_root`). Existing files in the root are
left alone, except an earlier generated `odoo/` or a plain `odoo` symlink.

Check addons through their `odoo.addons.<name>` name (`-p`, or their path under the
root), **not** through their real directory: mypy names `mypy addons/my_addon` as
`my_addon.*`, and the same files are then analyzed a second time as
`odoo.addons.my_addon.*` wherever they are imported. The plugin warns about this on
stderr (`odoo-typing: warning: mypy names module "my_addon" ...`) and adds no
dependencies for such files.

With `mypy.ini`, `.mypy.ini` or `setup.cfg`, use an `[odoo_typing]` section; separate
`addons_path` entries with commas or newlines:

```ini
[mypy]
plugins = odoo_typing.plugin
mypy_path = .odoo-root
check_untyped_defs = true

[mypy-odoo.*]
follow_imports = silent

[odoo_typing]
odoo_version = 18
odoo_path = /usr/lib/python3/dist-packages/odoo
addons_path = addons, ../enterprise
overlay_root = .odoo-root
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

### Simple setup (no `odoo.addons.<custom addon>` imports)

Without `overlay_root`, your addons are top-level packages (`my_addon.models.x`):
`from odoo.addons.my_addon...` imports of them are then `import-not-found` and give `Any`.

```toml
[tool.mypy]
plugins = ["odoo_typing.plugin"]
# 1) a directory that contains the `odoo` package (see the note below)
# 2) your addons directories, so `my_addon.models.x` can be imported
mypy_path = [".odoo-root", "addons"]
check_untyped_defs = true

[[tool.mypy.overrides]]
module = ["odoo", "odoo.*"]
follow_imports = "silent"

[tool.odoo_typing]
odoo_version = 18
odoo_path = ".odoo-root/odoo"        # dir with release.py; auto-detected if omitted
addons_path = ["addons"]
```

**Note on `mypy_path`:** if Odoo is installed as a deb/pip package, don't put the whole
`site-packages`/`dist-packages` directory on `mypy_path`. Create a directory that holds
only a symlink instead: `mkdir .odoo-root && ln -s /usr/lib/python3/dist-packages/odoo .odoo-root/odoo`.
For a git checkout, use the checkout root. Then run `mypy addons/my_addon`.

## How it works

- **Registry** ([registry.py](src/odoo_typing/registry.py), [scanner.py](src/odoo_typing/scanner.py)):
  a static AST scan that never imports Odoo or touches a database. It mirrors Odoo 18's
  loading rules:
  - addons are directories with a `__manifest__.py` (or the deprecated `__openerp__.py`),
    named after their entry in the addons path (a symlink may point to any directory);
    files are matched to addons by their resolved directory, so a path through the
    overlay root, a symlink or the real directory finds the same addon. Module names of
    an `addons_path` addon are recognized in both spellings (`my_addon.x`,
    `odoo.addons.my_addon.x`); the plugin itself only ever names the configured one;
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
  attributes then give `Any`). A class that mypy never analyzes although its module is
  done (an unreachable `if sys.version_info < ...` branch) does not exist for mypy and
  is simply left out. Written bases keep their type arguments (`class N(Mix[int])`).
  Python's own MRO of the written bases is respected; when
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
  A module reuses the merged class of a module it imports from, so records passed
  between them (`RECS = env["x"]` in one, `from .a import RECS` in the other) keep one
  type.
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
ones that use the changed model. Whether an unknown name is reported as `odoo-model` or
as `odoo-model-depends` ("defined in addon ..., which is not in the dependencies"), and
the addon that message names, can stay outdated when an addon *outside* the closure is
added or removed, until the file is checked again (that the name is not visible stays
right). Switching `overlay_root` on or off changes every addon's module names and
invalidates the whole cache.

**dmypy:** the daemon keeps one plugin instance. Before the files of each update are
processed, the plugin compares the stamps of everything its registry has read (addons
roots, manifests, scanned files) with the disk and starts over with a fresh registry if
any changed. The daemon only rechecks files whose source changed (and their
dependents), and the plugin reads its settings once: after editing only a manifest, or
the `[tool.odoo_typing]`/`[odoo_typing]` settings, run `dmypy restart`.

## Verification

The test suite (`uv run pytest`, 246 tests, most of them end to end) runs the real mypy against the
installed Odoo 18 on throw-away addons, also with two runs on one cache and through
`dmypy`. The regression benchmark below (mypy 2.4.1, Odoo 18 core, each addon checked
on its own with a fresh cache, then again warm) compares plain mypy with the plugin.
*Plugin-only* errors are those that only appear with the plugin; most are real under
Odoo's declared field types (`str | Literal[False]`, `int | NewId` ids) or mypy
inference complaints that real recordset types expose. Times are wall-clock on a shared
machine and vary by about 10 %.

| Addon | Errors without plugin | Errors with plugin | Plugin-only | Cold s (without → with) | Warm s (without → with) |
|---|---:|---:|---:|---:|---:|
| crm | 1797 | 1155 | 20 | 3.45 → 5.06 | 0.20 → 0.27 |
| sale | 2743 | 1847 | 30 | 4.33 → 6.73 | 0.25 → 0.33 |
| account | 10051 | 7267 | 140 | 9.31 → 11.54 | 0.33 → 0.30 |
| website_sale | 1853 | 1060 | 14 | 4.63 → 7.18 | 0.26 → 0.36 |
| project_sms | 65 | 51 | 0 | 2.82 → 4.15 | 0.20 → 0.28 |
| sale_stock | 1708 | 780 | 16 | 4.44 → 7.09 | 0.25 → 0.35 |

Warm runs report exactly the errors of the cold ones, and no run crashed. Peak RSS with
the plugin is 230-340 MB, 5-9 % more than plain mypy.

## Development

```bash
uv sync
uv run pytest            # needs Odoo installed; set ODOO_PATH=/path/to/odoo if not auto-found
uv run ruff check src tests tools && uv run ruff format --check src tests tools && uv run mypy
```

The end-to-end tests run the real mypy on throw-away addons against the installed Odoo.
They fail when mypy crashes (`INTERNAL ERROR`, exit status 2), not only on wrong messages.

Other Odoo versions: `tools/odoo/fetch.sh` makes a shallow, sparse checkout (no `static/`
or `i18n/`, about 150-200 MB) under `~/.cache/odoo-typing/odoo/<version>/` and prints the
`odoo` package directory to test against. The tests read the version from its
`release.py` (or `ODOO_VERSION`); for a version the plugin has no profile for yet, the
end-to-end tests are skipped. `@pytest.mark.odoo(18, 19)` limits a test to some versions.

```bash
ODOO_PATH=$(tools/odoo/fetch.sh 19.0) uv run pytest
```

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

- `--odoo-version N` (default 18): `odoo_version` of the plugin config. Other versions
  default to the root `tools/odoo/fetch.sh N.0` builds and keep their reports and baseline
  in `<bench-dir>/odoo-N/`.
- `--odoo-root DIR`: a directory that holds only an `odoo` symlink (see the note above).
- `--bench-dir DIR`: where `<label>.json`, the generated configs (`config/`) and the caches
  (`cache/<label>/<addon>`) go.
- `--addons a b ...`, `--jobs N` (addons at once; 1 gives the most stable timings).
- The run without the plugin is saved once as `noplugin.json` and reused. Pass
  `--refresh-baseline` after upgrading mypy or Odoo; it rebuilds the baseline from the
  `--addons` given, so pass all of them.

`--compare` prints per-addon counts and times, then the plugin-only errors that are NEW or
GONE compared with the earlier label. They are grouped by error code and message pattern
(quoted names replaced by `"_"`), with 3 examples per group. The script exits with status 1
if any mypy run crashed.
See [docs/odoo-ls-cli.md](docs/odoo-ls-cli.md) for how this project compares with odoo-ls
and pylint-odoo on the probe addon in `tests/fixtures/addons/probe_mod`.

## Not yet supported

- `vals` built in a variable or with `dict(...)`, and the `default_<field>` keys of a
  context that is not passed straight to `.create(...)`; a key guarded by
  `if "x" in self._fields:` is still reported
- domains built in a variable or with `expression.AND(...)`, and string arguments of
  `mapped()` / `filtered()` that are not literals
- an unannotated override of a `-> Self` method (`create`, `copy`, `search`, `sudo`...)
  is typed `(self: Self, ...) -> Self`, so `super().create(...)` keeps the model type; in
  its body, returning a concrete class (`return self.env["account.account"].union(...)`)
  is a `return-value` error ("expected Self")
- `self` is the class being written, so inside a class `self` sees the earlier classes of
  its model, not later ones of the same addon (`env[...]` does see them). The classes of
  one model are distinct types: a variable that first holds `env["x"]` (or a relational
  field of model `x`, as in `for line in self.move_id.line_ids:` then
  `for line in self:` in an earlier `account.move.line` class) cannot later be
  assigned `self` of an earlier class of `x` (an `assignment` error)
- only the guard forms listed above are detected (a guard in a helper function, or a
  flag computed earlier, is not)
- `env[name]` with a name only known at run time is `Any` (option `dynamic_env`), so
  code on it is not checked
- the generated overlay root is not checked against the addons: rerun `odoo-typing-root`
  after adding an addon, or `odoo.addons.<new>` imports are `import-not-found`
