# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

First release: a mypy plugin for the Odoo ORM, Odoo 17, 18, 19 and 20 (`odoo_version`).
License: LGPL-3.0-or-later. Copyright (C) 2026 Taras Shabaranskyi.

### Models and records

- Model classes get the MRO of Odoo's registry class as their addon sees it
  (`_inherit`, `base`, mixins), so fields and methods of earlier classes and of the
  dependencies are known on `self`; `_inherits` delegation, transitively.
- `env["model.name"]` is the model's class as the addon sees it (also `Final` constants,
  aliases of `env`, `request.env` in controllers); `env[records._name]` the type of
  `records`; `env.user` / `env.company`.
- Relational fields are typed with their comodel (also from `related=`, through chains of
  related fields); `Binary`,
  `Reference` and log-access fields get real types; option `literal_selection`.
- `env.ref("module.name")` is the record's model, from the XML ids of data files (also
  the ids `_update_xmlids` gives, whose model the data does not say).
- `odoo.http.request` has the attributes addons set at run time (`request.website`,
  `.lang`, `.is_frontend`, `.website_routing`...), typed `Any`.
- `mapped("a.b")`, `grouped("a")` and `read([...])` / `search_read` return real types.
- Unannotated overrides of `-> Self` methods that return their own records keep the
  model type through `super()`.

### Checks

- Every check also runs where `self` is a type variable: in an unannotated `create` /
  `copy` override (typed `(self: Self) -> Self`), under `-> Self` and `self: T`.
- Option `required_values`: required fields and `display_name` read without
  `| Literal[False]` (off by default: a new record reads `False`).
- Fewer false errors: a key that only an addon outside the closure adds to a selection
  (`selection_add`) in domains and vals; `vals_ignore_keys` names (`x_*`) in domains,
  `mapped`, `read`, orders and `@api.depends`; `default_x=False` in a context; assigning
  `False`, an id or commands to a field Odoo annotates (`partner.country_id = False`);
  `mapped("m2o")` of a comodel mypy cannot type is no `list`.
- `filtered(lambda r: r.partner_id)` (Odoo 19+, which declares `Callable[[Self], bool]`)
  takes a function of any result; `Command.link` / `update` / `delete` / `unlink` take a
  `NewId` (Odoo 17, 18: `record.id` is `int | NewId`).
- `odoo-model` / `odoo-model-depends`: unknown model names, models outside `depends`
  (not under guards such as `if "sale.order" in self.env:`, also through a flag
  variable or a `return <test>` helper method of the class).
- `odoo-vals` (`odoo-vals-key`, `odoo-vals-type`): `create` / `write` / `copy` vals,
  also `dict(...)` and a variable built in the function (`vals["k"] = v`,
  `vals.update(...)`, `vals_list.append(...)`), x2many commands, field assignments,
  `default_<field>` of `with_context(...).create`.
- `odoo-field-ref`: `related=`, `compute=`, `inverse=`, `search=`, `selection=`,
  `@api.depends` / `constrains` / `onchange`, paths of `mapped` / `filtered` / `sorted`
  / `grouped` (literals and `Final` / `Literal` strings), fields of `read()` / `fetch()`
  / `search_fetch()`; `currency_field=`, `inverse_name=`, `group_expand=`; orders
  (`order=` of `search` and the like, `_order`); `_rec_name`, `_parent_name`,
  `_fold_name`, `_active_name`, `_rec_names_search`; the `groupby` and `aggregates`
  specifications of `_read_group`.
- `odoo-selection`: a literal string compared with a `Selection` field (`==`, `!=`,
  `in`, `not in`) that is not one of its keys.
- `odoo-vals-type` also for x2many commands that carry the ids of another model's
  records, and tuples that are not commands; `odoo-field-ref` also for `ondelete=` of
  a `Many2one`.
- `odoo-xmlid` also for the groups of `has_group()` / `has_groups()` /
  `user_has_groups()` and of a field's `groups=`, and for the action of `_for_xml_id()`.
- A field guarded by `"f" in self._fields`, `_fields.get("f")` or `hasattr(x, "f")` is
  not reported as unknown in vals, domains and record paths under the guard.
- `odoo-domain`: field paths, operators (per Odoo version, case-sensitive except in
  Odoo 19) and selection values of literal domains, also `[...] + [...]`,
  `expression.AND/OR`, `a if c else b`, `a or b` and a variable built in the function;
  `domain=` of relational fields (also `domain=lambda self: [...]`), the domain of
  `name_search()`, what a field's `search=` method returns; `Domain(...)`,
  `Domain.AND/OR`, `&`, `|`, `~` on Odoo 19+. The structure of every domain of an addon
  file (three-item terms starting with a field name, `&` / `|` / `!` with their terms,
  no `None` / dict / nested domain as an item), and the rules of each Odoo version
  where it raises or silently does something else: date parts, `Properties` and
  `Binary` paths, `any` / `child_of` on non-relational fields, inequalities on
  relational and Boolean fields, `=like` patterns, numbers, scalars for `in`, records as
  values, dates, `access`, `any!`.
- `odoo-domain-search`: terms that only Odoo's SQL search rejects or ignores, checked
  only in domains a search takes: fields that cannot be searched (Odoo 17/18 silently
  match every record), existence checks of `Binary` attachments, and the version rules
  that `filtered_domain()` accepts.
- `odoo-domain-model`: a domain of another model than the one that takes it (parents,
  mixins and delegated models fit).
- On Odoo 19+, a domain built in a variable goes to `search()` (and every parameter
  Odoo annotates `DomainType`) without a false `arg-type`.
- `odoo-xmlid` / `odoo-xmlid-depends`: XML ids of `env.ref(...)`.

### Annotated helpers

- The annotations of the PyCharm plugin "Odoo IDE" are types:
  `Annotated[dict, "odoo.values.<model>"]` and `"odoo.values.<model>"` are the model's
  `vals` (a named `TypedDict`: keys and value types checked wherever the values are
  built; `vals.get(...)` typed per key), `"odoo.model.<model>"` its records as
  `env[...]` gives them. Bare strings work in the annotations of functions, variables
  and type comments of a module that has a regular class, on every path mypy analyzes
  it (incremental runs, import cycles, `--num-workers`, the daemon). The keys are the
  fields any addon on the addons paths gives the model and its `_inherit` parents and
  mixins, so `super()` in a later addon keeps them; a field added anywhere makes mypy
  check the annotating files again. `create` (also a list of values, except through
  `super().create(...)` on Odoo 18+, where a `cast` is needed) / `write` / `copy` /
  x2many commands report values of an unrelated model (`odoo-vals-type`; parents' and
  `_inherits` models' values fit); unknown, ambiguous or out-of-`depends` model names
  are `odoo-model` / `odoo-model-depends`.
- `"odoo.domain.<model>"` / `Annotated[list | Domain | DomainType, "odoo.domain.<model>"]`
  (the plugin's own) is a domain of the model: its literals are checked in a `return`,
  an annotated variable or parameter and the changes of either, its model wherever it
  goes (search methods, annotated parameters, also of inherited methods and of a
  mixin's method called on a model that inherits it, `any`, `domain=`, returns,
  assignments); a variable without an annotation has the model its assignments agree
  on.

### Daemon

- Checks of domains, `vals` and field paths follow edits of the fields they read in
  other modules (a rename, `store=`, `search=`, a delegated model's field), from the
  daemon's start or its fine-grained cache.
- Checks of a domain or `vals` built in a variable no longer disappear after an edit of
  the same file.

### Performance

- Addon scans are kept in `scan_cache_dir` (`~/.cache/odoo-typing/scans` by default),
  shared by every project and mypy cache: a new cache, worktree or project on the same
  Odoo skips the scans of Odoo's addons (20–26 % less time on the cold benchmark runs).
- Scanning sorts the nodes of a file by class once (guards, model references), encodes
  stored scans and fingerprints faster, and keeps each hook lookup per name: cold runs
  take 3–6 % less time.
- Less memory: equal field maps, fields, MROs, tips and name sets are kept once, method
  names are looked up instead of stored per view, scan strings are interned, and addons
  read only to tell whether they define a model keep that answer, not their whole scan.
  The data the plugin keeps drops from 18 to 12.5 MB on `sale`; peak RSS of a cold run
  6–9 MB lower (sale 298 → 292 MB, account 355 → 348 MB), about a fifth of what the
  plugin adds.

### Robustness

- The plugin's records sit in a subdirectory per interpreter, since a scan parses with
  the running Python's grammar: `<cache_dir>/odoo_typing/<cache_tag>/`
  (`.mypy_cache/odoo_typing/cpython-312/`) and `<scan_cache_dir>/<cache_tag>/`.
  Records of earlier versions, directly in `odoo_typing/` or in `scan_cache_dir`, are
  never read again and can be deleted.
- The first run after an upgrade (any change of the plugin's source) is a full cold run,
  the standard library and Odoo's own modules included (`plugins differ` in `mypy -v`).
  Before, modules outside addons kept the results of the earlier version.
- Odoo's own modules (`odoo`, `odoo.*`, not its addons) carry `odoo_version` in their
  cache data, in every layout: correcting it checks them again.
- Read on the next run, where they used to go unnoticed: a file saved during a run
  (stamps are taken before each read), any manifest edit (each scan includes its
  manifest, so adding or removing data files updates the XML ids), a new `__init__.py`
  in an imported directory, and a `depends` edit that changes whether a model outside
  the closure is `odoo-model` or `odoo-model-depends`.
- A file, manifest or directory that cannot be read gets a stamp no file has. A source
  file is reported (`cannot parse ...: Permission denied`, also as
  `<dir>/__init__.py` for a package directory that cannot be searched). What was
  computed without it is kept while it stays unreadable, and it is read again once it
  can be (after `chmod` or `chown`), in runs and in the daemon, without a new registry
  on every daemon update. This holds for scans, data files, the class-file index and
  the addon index.
- Subdirectories of an addons root that cannot be read (`lost+found`) are not addons;
  they were a configuration error or an INTERNAL ERROR. Nor are `__pycache__` and the
  directories whose name starts with `.`, which are not stamped either: git or IDE
  activity in an addons root (`.git`, `.idea`) no longer invalidates the addon index.
  A symlink loop skips only its entry, and the class-file index does not follow it.
- A file nested too deeply for Python's parser is reported as
  `cannot parse ...: too complex to parse (RecursionError)` (or `MemoryError`).
- Compiler warnings (`SyntaxWarning: invalid escape sequence '\d'`) are no longer
  printed for scanned files, manifests and string domains, and `-W error` does not make
  them errors.
- Data files in a multi-byte encoding (Shift_JIS, GBK, EUC-JP) are decoded with their
  codec. A data file in an unknown encoding, a data entry with a NUL, a manifest that is
  no literal dict and a stored record that cannot be decoded are skipped instead of
  crashing mypy.
- A symlink loop in `odoo_path`, `overlay_root` or an `addons_path` entry
  (`... cannot be resolved: ...`), and a `pyproject.toml` that is not UTF-8 beside a
  `mypy.ini` (`cannot read ...`), are configuration errors instead of crashes.
- New warnings on stderr (mypy goes on):
  - a mypy outside the tested range `>=2.1,<2.5`: the plugin relies on mypy internals;
  - an intermediate (SaaS or development) Odoo such as `saas~18.3`, also of the
    configured major, and a major the plugin does not support (the advice
    `set it to <N>` is given only for a supported final release);
  - an Odoo that does not define the classes the profile of `odoo_version` expects.
- An exception the plugin does not expect while it finds the models a module relies on,
  computes a module's cache data or checks the addons for changes prints
  `odoo-typing: internal error while <doing>: <Type>: <message> (please report it to odoo-typing)`
  before mypy's INTERNAL ERROR (the exit status stays 2).
- `odoo.http.request` is a `Request` on Odoo 17-19 in every module, also one without
  model classes and on warm runs, so `request.env[...]` in controller helpers is
  checked. A project that checks Odoo's own `http.py` (without
  `follow_imports = "silent"`) may see new errors there.
- dmypy: the hooks chosen from declared signatures (`odoo.domain.*` and `DomainType`
  parameters) are chosen again on each update, so declarations in files the registry
  does not scan (an addon's `tests/`, a helper package) follow edits.
- Known dmypy limit: after a module that defines model classes is deleted or renamed,
  the files that used its models get a spurious `import-not-found` error without a line
  number until they are edited or the daemon restarts.
- The CI tests Python 3.11 (on Odoo 17) and 3.13 (on Odoo 20) besides 3.12.

### Tooling

- `odoo-typing-stubs` writes `.pyi` stubs of the models for editors (Pyright,
  PyCharm): `env["res.partner"]` and its fields without mypy.
- `odoo-typing-baseline` reports only the errors that are not in a recorded baseline.
- `odoo-typing-root` generates a `mypy_path` root in which every addon is
  `odoo.addons.<name>`; `--check` reports an out-of-date one.
- mypy's incremental cache and `dmypy` follow edits of other addons, manifests, data
  files and the plugin settings.
- `tools/bench/bench.py`: regression benchmark on real Odoo addons.
