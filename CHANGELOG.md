# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

First release: a mypy plugin for the Odoo ORM, Odoo 17, 18, 19 and 20 (`odoo_version`).
License: LGPL-3.0-or-later.

### Models and records

- Model classes get the MRO of Odoo's registry class as their addon sees it
  (`_inherit`, `base`, mixins), so fields and methods of earlier classes and of the
  dependencies are known on `self`; `_inherits` delegation, transitively.
- `env["model.name"]` is the model's class as the addon sees it (also `Final` constants,
  aliases of `env`, `request.env` in controllers); `env.user` / `env.company`.
- Relational fields are typed with their comodel (also from `related=`, through chains of
  related fields); `Binary`,
  `Reference` and log-access fields get real types; option `literal_selection`.
- `env.ref("module.name")` is the record's model, from the XML ids of data files.
- `mapped("a.b")`, `grouped("a")` and `read([...])` / `search_read` return real types.
- Unannotated overrides of `-> Self` methods keep the model type through `super()`.

### Checks

- `odoo-model` / `odoo-model-depends`: unknown model names, models outside `depends`
  (not under guards such as `if "sale.order" in self.env:`).
- `odoo-vals` (`odoo-vals-key`, `odoo-vals-type`): `create` / `write` / `copy` vals,
  x2many commands, field assignments, `default_<field>` of `with_context(...).create`.
- `odoo-field-ref`: `related=`, `compute=`, `inverse=`, `search=`, `selection=`,
  `@api.depends` / `constrains` / `onchange`, paths of `mapped` / `filtered` / `sorted`
  / `grouped`, fields of `read()`.
- `odoo-domain`: field paths, operators (per Odoo version) and selection values of
  literal domains, `domain=` of relational fields, `Domain(...)` on Odoo 19+.
- `odoo-xmlid` / `odoo-xmlid-depends`: XML ids of `env.ref(...)`.

### Tooling

- `odoo-typing-stubs` writes `.pyi` stubs of the models for editors (Pyright,
  PyCharm): `env["res.partner"]` and its fields without mypy.
- `odoo-typing-baseline` reports only the errors that are not in a recorded baseline.
- `odoo-typing-root` generates a `mypy_path` root in which every addon is
  `odoo.addons.<name>`; `--check` reports an out-of-date one.
- mypy's incremental cache and `dmypy` follow edits of other addons, manifests, data
  files and the plugin settings.
- `tools/bench/bench.py`: regression benchmark on real Odoo addons.
