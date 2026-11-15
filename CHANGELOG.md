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
- `env.ref("module.name")` is the record's model, from the XML ids of data files.
- `mapped("a.b")`, `grouped("a")` and `read([...])` / `search_read` return real types.
- Unannotated overrides of `-> Self` methods that return their own records keep the
  model type through `super()`.

### Checks

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

### Tooling

- `odoo-typing-stubs` writes `.pyi` stubs of the models for editors (Pyright,
  PyCharm): `env["res.partner"]` and its fields without mypy.
- `odoo-typing-baseline` reports only the errors that are not in a recorded baseline.
- `odoo-typing-root` generates a `mypy_path` root in which every addon is
  `odoo.addons.<name>`; `--check` reports an out-of-date one.
- mypy's incremental cache and `dmypy` follow edits of other addons, manifests, data
  files and the plugin settings.
- `tools/bench/bench.py`: regression benchmark on real Odoo addons.
