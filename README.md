# odoo-typing

**A mypy plugin that understands the Odoo ORM.**

[![Odoo 17–20](https://img.shields.io/badge/Odoo-17%20%7C%2018%20%7C%2019%20%7C%2020-714B67)](docs/configuration.md#options)
[![mypy 2.1–2.4](https://img.shields.io/badge/mypy-2.1%E2%80%932.4-2a6db2)](https://mypy-lang.org/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab)](https://www.python.org/)
[![License: LGPL v3+](https://img.shields.io/badge/license-LGPL--3.0--or--later-blue)](LICENSE)

Plain mypy sees `self.env["res.partner"]` as a bare `BaseModel`, every relational field
as `Any`, and nothing that other addons add through `_inherit`. So most Odoo code goes
unchecked. odoo-typing teaches mypy how Odoo builds its models: per addon, following
`depends`, exactly as Odoo loads them.

```python
class SaleOrder(models.Model):
    _inherit = "sale.order"

    x_score = fields.Integer()

    def action_check(self):
        partner = self.env["res.partner"]   # ResPartner, with every extension this addon sees
        self.partner_id.citty
        self.x_score + self.name
        self.search([("stte", "=", "draft")])
        self.write({"state": "cancelled"})
        self.env.ref("sale.email_template_edi_sales")
        self.env["crm.lead"]
```

Plain mypy only complains that `SaleOrder` has no attribute `partner_id` or `name` (they
come from `sale`) and misses every real bug. With the plugin, on Odoo 18:

```text
error: "ResPartner" has no attribute "citty"  [attr-defined]
error: Unsupported operand types for + ("int" and "str")  [operator]
error: Odoo model "sale.order" has no field "stte" in domain; did you mean "state"?  [odoo-domain]
error: Value "cancelled" is not in the selection of field "state" of Odoo model "sale.order"
       ("draft", "sent", "sale", "cancel")  [odoo-vals-type]
error: Unknown XML id "sale.email_template_edi_sales"; did you mean "sale.email_template_edi_sale"?  [odoo-xmlid]
error: Odoo model "crm.lead" is defined in addon "crm", which is not in the dependencies
       of "my_addon"  [odoo-model-depends]
```

## Features

- **Real model types.** `env["model"]`, `self`, relational fields, `sudo()`,
  `with_context()`, `filtered()`, `search()`, `env.user`, `env.ref(...)` all get the
  model's class, with every field and method the addon sees.
- **Per-addon view.** A module sees only the models of the addons in its `depends`
  closure. A model used without its addon in `depends` is reported.
- **Odoo-specific checks.** Model names, field references (`related=`, `compute=`,
  `@api.depends`), `create` / `write` values, domains and XML ids.
- **Typed helper methods.** The annotations of the PyCharm plugin "Odoo IDE",
  `"odoo.values.res_partner"` and `"odoo.model.res_partner"`, are real types: the keys
  and value types of `vals` built in helpers are checked too.
- **No Odoo runtime needed.** The plugin reads addons statically: no database, no import
  of Odoo.
- **Fast on repeated runs.** It works with mypy's incremental cache and the `dmypy`
  daemon, and keeps addon scans in a cache shared between projects.
- **Editor support.** `odoo-typing-stubs` writes `.pyi` stubs for Pyright, PyCharm and
  VS Code.

## Installation

The package is not on PyPI yet; install it from GitHub into the environment that runs
mypy:

```bash
pip install "git+https://github.com/TesaLazarenko/odoo_mypy_plugin.git"
```

or, with uv:

```bash
uv add --dev "git+https://github.com/TesaLazarenko/odoo_mypy_plugin.git"
```

## Quick start

**1. Configure** mypy and the plugin in the `pyproject.toml` next to your addons:

```toml
[tool.mypy]
plugins = ["odoo_typing.plugin"]
mypy_path = [".odoo-root"]
check_untyped_defs = true            # Odoo methods are rarely annotated
explicit_package_bases = true        # needed on Odoo 19+

[[tool.mypy.overrides]]
module = ["odoo", "odoo.*"]
follow_imports = "silent"            # don't report errors inside Odoo itself

[tool.odoo_typing]
odoo_version = 18                    # 17, 18, 19 or 20
odoo_path = "/usr/lib/python3/dist-packages/odoo"
addons_path = ["addons"]
overlay_root = ".odoo-root"
```

**2. Generate the overlay root**, a directory of symlinks that lets mypy import your
addons as `odoo.addons.<name>`, like Odoo does:

```bash
odoo-typing-root
```

Run it again after adding or removing an addon.

**3. Run mypy** on an addon by its module name:

```bash
mypy -p odoo.addons.my_addon
```

See [Configuration](docs/configuration.md) for every option, `mypy.ini` support and the
notes for Odoo 19 and 20.

## Error codes

| Code | What it reports |
|---|---|
| `odoo-model` | an unknown model name (a typo), a broken `_inherit` |
| ↳ `odoo-model-depends` | a model whose addon is missing from `depends` |
| `odoo-field-ref` | a wrong field or method name in `related=`, `compute=`, `@api.depends`, `mapped()`, `order=`, `_order`, `_read_group()`... |
| `odoo-vals` | wrong `create()` / `write()` values: `odoo-vals-key` (no such field), `odoo-vals-type` (wrong value type) |
| `odoo-domain` | a wrong field, operator, value or structure in a domain |
| ↳ `odoo-domain-model` | a domain of another model than the one that takes it |
| ↳ `odoo-domain-search` | a term that Odoo's search rejects or silently ignores (an unsearchable field...) |
| `odoo-selection` | a string compared with a `Selection` field that is not one of its keys |
| `odoo-xmlid` | an XML id of `env.ref()`, `has_group()`, `groups=` that no data file in the closure creates |

Turn a code off with `disable_error_code = ["odoo-domain"]` in `[tool.mypy]`, or silence
one line with `# type: ignore[odoo-domain]`. Code guarded by
`if "sale.order" in self.env:` is not reported at all.

[What each check covers, and how to handle false positives →](docs/checks.md)

## Annotating helpers

`vals`, records and domains that pass through helper methods have no type mypy could
check. Annotate them the way the PyCharm plugin "Odoo IDE" does (`odoo.values.*`,
`odoo.model.*`; `odoo.domain.*` is this plugin's), and the plugin checks them:

```python
from typing import Annotated

class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _prepare_note(self) -> Annotated[dict, "odoo.values.sale_order"]:
        return {"note": "x", "nmae": "y"}
        # error: Extra key "nmae" for TypedDict "sale.order values"  [typeddict-unknown-key]

    def _partners(self) -> "odoo.model.res_partner":   # records, as env["res.partner"]
        return self.partner_id

    def _open_domain(self) -> "odoo.domain.sale_order":
        return [("stte", "=", "draft")]
        # error: Odoo model "sale.order" has no field "stte" in domain; did you mean "state"?

    def action(self) -> None:
        self.env["account.move"].search(self._open_domain())
        # error: Domain of Odoo model "sale.order" given to search() of Odoo model
        # "account.move"  [odoo-domain-model]
```

- `odoo.values.<model>` is the model's `vals`: every key must be a field of the model,
  and each value must fit the field. `create()` / `write()` take it (and lists of it,
  except through `super().create(...)` on Odoo 18+), and values of an unrelated model
  are reported there.
- `odoo.model.<model>` is the model's records as the addon sees them.
- `odoo.domain.<model>` is a domain of the model: its literals are checked where they
  are written (a `return`, an annotated variable or parameter), and its model where it
  goes (`search()`, a parameter, `any`, `domain=`). A domain of a parent model, a mixin
  or a delegated model fits too. `Annotated[list, ...]` and, on Odoo 19+,
  `Annotated[Domain, ...]` are the forms for variables that use `.append()` or `&`.
- The model is named with `_` for `.` (`sale_order`). The bare string works in
  annotations of functions and variables in a module that has a regular class; in a
  module of plain functions and in `cast(...)` use `Annotated[dict, "odoo.values.x"]`
  (`Annotated[list, "odoo.domain.x"]`), which other tools read as a plain `dict`
  (`list`). A type alias of either is a plain `dict` (`list`).

[The rules, and what is not checked in them →](docs/checks.md#annotated-helpers-odoovalues-and-odoomodel)

## Using it in a project

- **Editors**: `odoo-typing-stubs --addon my_addon --output typings` gives Pyright,
  PyCharm and VS Code the same model types.
- **pre-commit**: the repository provides an `odoo-typing` hook.
- **Existing code base**: `odoo-typing-baseline` records today's errors and reports only
  new ones, so you can adopt the plugin gradually.

[Editors, pre-commit, CI and baselines →](docs/integration.md)

## Documentation

| | |
|---|---|
| [What the plugin checks](docs/checks.md) | every check, error code, guard and known limitation |
| [Configuration](docs/configuration.md) | options, the overlay root, Odoo 19/20, `mypy.ini` |
| [Editors, pre-commit and CI](docs/integration.md) | stubs, hooks, CI image, baselines |
| [How it works](docs/how-it-works.md) | the registry, mypy hooks, caching, `dmypy` |
| [Development](docs/development.md) | tests, other Odoo versions, the benchmark |
| [Changelog](CHANGELOG.md) | what changed between versions |

## Limitations

- `vals` and domains returned by unannotated helpers (`self._prepare_vals()`,
  `self._get_domain()`) are not checked; annotate the helper with `odoo.values.*` /
  `odoo.domain.*`.
- Model names known only at run time (`env[name]`) give `Any`.
- Code outside any addon (scripts, Odoo's own `odoo/*.py`) is left as plain mypy sees
  it.
- Only mypy 2.1–2.4 and the final Odoo releases 17.0–20.0 are supported: another mypy
  version or an Odoo SaaS checkout (`saas~18.3`) gets a warning.

[The full list →](docs/checks.md#not-yet-supported)

## License

Copyright (C) 2026 Taras Shabaranskyi

Licensed under the [GNU Lesser General Public License v3.0 or later](LICENSE), the
license of Odoo Community. [COPYING](COPYING) holds the GNU GPL v3 that it supplements.
