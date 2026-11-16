# What the plugin checks

[← README](../README.md)

Models are resolved **per addon**: a module sees only the models of the addons in its
`depends` closure, just as Odoo loads them.

- [Types the plugin gives](#types-the-plugin-gives)
- [Error codes](#error-codes)
- [Unknown models: `odoo-model`](#unknown-models-odoo-model)
- [Optional dependencies: guards](#optional-dependencies-guards)
- [Field definitions and field names: `odoo-field-ref`](#field-definitions-and-field-names-odoo-field-ref)
- [Values of `create` and `write`: `odoo-vals`](#values-of-create-and-write-odoo-vals)
- [Domains: `odoo-domain`](#domains-odoo-domain)
- [XML ids: `odoo-xmlid`](#xml-ids-odoo-xmlid)
- [Fields that Odoo leaves untyped](#fields-that-odoo-leaves-untyped)
- [Annotated helpers: `odoo.values` and `odoo.model`](#annotated-helpers-odoovalues-and-odoomodel)
- [Code outside any addon](#code-outside-any-addon)
- [Plain mypy errors you will see more often](#plain-mypy-errors-you-will-see-more-often)
- [Not yet supported](#not-yet-supported)

## Types the plugin gives

Plain mypy treats `env["res.partner"]` as `BaseModel`. It cannot see fields that other
modules add through `_inherit`, and it reads every relational field as `Any`. With the
plugin:

- **`self.env["res.partner"]`** is `Partner`, with the `_inherit` extensions visible from
  the addon (merged when unrelated addons extend it), not `BaseModel`.
- **`self.name`** inside `_inherit = "res.partner"` is `str | Literal[False]`, not
  `has no attribute "name"`; a required field (`required=True`) and `display_name` are
  `str` (option [`required_values`](configuration.md#options)).
- **`fields.Many2one("res.partner")`** is `Many2one[Partner]`, not `Need type annotation`.
- **`self.partner_id`, `self.line_ids`** are the comodel's records, with the extensions
  the reading addon sees, not `Any`. So `self.x_score + self.name` is an error
  (`Unsupported operand types for + ("int" and "str")`) instead of silence. Where mypy
  has not loaded the reader's classes of a comodel (a file reaches it through another
  model and never names it: `partner.user_ids`) and the classes it has are fewer than
  the reader sees, the records are `Any` rather than a class without the reader's own
  extensions.
- **`self.sudo()`, `.with_context(...)`, `.filtered(...)`, `.search([])`, `.copy()`**
  keep the receiver's model, also through unannotated overrides of `create`, `copy`,
  `search`...
- **`self.env.user`, `self.env.company`** are `res.users` / `res.company` records as the
  addon sees them; so is `self.env.website` on Odoo 20 (declared as `BaseModel`: an
  attribute of the environment declared as records of no model is the model it is named
  after).
- **Methods added to `base`** (`self._phone_format(...)` from `phone_validation`) exist
  on every model of an addon that depends on the adding addon.
- **`_inherits` and `delegate=True`**: `line.product_id.uom_id` (through `_inherits` of
  `product.template`) is the `uom.uom` record. Fields of the parents are delegated,
  methods are not. A delegated field is required when the parent's field is, as in Odoo:
  `product.name` of a `product.product` is `str`.
- **Log-access fields**: `self.create_date` is `datetime` (`create` and `write` set the
  dates on every record; `datetime | Literal[False]` with `required_values = false`) and
  `self.create_uid` a `res.users` record, unless `_log_access` / `_auto` is `False`.
- **Attributes a class method defines**: `cls.partner = cls.env["res.partner"].create(...)`
  in `setUpClass` defines `partner` for the class and its subclasses, with the type of
  the value. Plain mypy takes only `self.name = ...` of an instance method for a
  definition, so every such line and every later `self.partner` of a test was
  `has no attribute "partner"`, and the records were `Any`: nearly all errors of a test
  module. The records a test class of another addon keeps (`self.product` of
  `product`'s common test class, read in a test of `sale`) are the model as the reading
  addon sees it. The tests are then checked like any other code.
- **`from odoo.addons.my_base.models.x import Y`** (a custom addon) is the real class
  with the [overlay root](configuration.md#the-overlay-root), not `import-not-found`.
- **`mapped("partner_id.name")`** is `list[str | Literal[False]]`, `mapped("line_ids")`
  the comodel's records, and `grouped("partner_id")` a `dict` from the field's values to
  records. `filtered("default_code").mapped("default_code")` is `list[str]`: the
  records kept have the field set (a path through a relational field maps the values of
  other records too, so it keeps `False`).
- **`mapped(lambda r: r.amount)`** is `list[float]`, and the function's records when it
  returns records. Odoo 19 and 20 declare `list[T] | BaseModel` for both, which fits no
  parameter that takes a list (`sum(...)`, `", ".join(...)`). `mapped(name)` of a path
  known only at run time is `Any`.
- **`read(["name", "partner_id"])`** and `search_read(domain, ["name"])` with a literal
  list return a list of `TypedDict`s with `id` and those fields, typed as `read()` gives
  them (`partner_id` is `tuple[int, str] | Literal[False]`, x2many fields `list[int]`).
  A required field reads without `False` there too (`required_values`): a required
  `Many2one` is `tuple[int, str]` (`int` with `load=None`).
- **`env[records._name]`** has the type of `records` (`self` in a mixin is the model
  that uses it at run time).
- **`self.env.cr`** has the methods of a cursor (`execute`, `fetchall`, `dbname`): Odoo
  18 declares it as `BaseCursor`, a base class that has none of them, so every
  `env.cr.execute(...)` was `"BaseCursor" has no attribute "execute"`.
- **`self.env.context`** is a mapping on Odoo 20 (`.get(...)`, `["key"]`, `dict(...)`,
  `**context`). Odoo 20 declares it as `frozendict`, a class whose `__new__` returns a
  `MappingProxyType` and that has no method of its own, so every
  `env.context.get(...)` was `"frozendict" has no attribute "get"`. A class without
  bases whose `__new__` builds another class's object has that class as its base.
- **Controllers**: `odoo.http.request` is a `Request` and `request.env` an
  `Environment`, so `request.env["res.partner"]` is checked like `self.env`, in every
  module, also one without model classes (a controller helper) and on warm runs. (Odoo
  17-19 build `request` with werkzeug, which mypy does not follow; Odoo 20 declares
  `env` as possibly `None`.) A controller's own `self.env` (Odoo 20, declared
  `Environment | None` by Odoo) is an `Environment` too; an `env` that an addon's own
  class declares as possibly `None` stays so.
- **`from odoo import Command, _, SUPERUSER_ID`** on Odoo 19 and 20: `odoo` has no
  `__init__.py` there, and gets these names from `odoo/init.py`
  (`odoo.Command = Command`), which mypy does not follow: each such import was
  `Module "odoo" has no attribute "Command"`, and the name was `Any`. The package gets
  the names a module of its own binds on it.
- **`table.field`** of a query's table (Odoo 20's `TableSQL`): a class that names its
  attribute hook after another method (`__getattr__ = __getitem__`) has that method as
  the hook; mypy alone reports every attribute of such a class. A hook declared to
  return a base class of its own class (`-> SQL`) gives `Any`, so
  `table.order_id.partner_id` is no error.
- **Lists of commands**: `commands = [Command.update(...)]` takes
  `commands.append((2, line.id))` too (on Odoo 19 and 20 `Command` is an `int`
  enumeration, and the plain tuple is no `tuple[Command, ...]`).

**Odoo 17** declares no types at all: the plugin gives its fields, `self.env`,
`self.id`, record iteration, `create()` and the `api` decorators the types Odoo 18
declares.

## Error codes

Every check has its own error code; a sub-code is also disabled with its parent.

| Code | What it reports |
|---|---|
| `odoo-model` | a model name that no addon on the addons path defines; `_inherit` defined later; `_inherit` cycles |
| ↳ `odoo-model-depends` | a model that only addons outside the `depends` closure define |
| `odoo-field-ref` | names in field definitions, `@api.depends` / `constrains` / `onchange`, `mapped` / `filtered` / `sorted` / `grouped`, `read()` / `fetch()`, orders (`order=`, `_order`), `_rec_name` and the like, `_read_group` specifications |
| `odoo-selection` | a string compared with a `Selection` field that is not one of its keys |
| `odoo-vals` | the `vals` of `create` / `write` / `copy(default=)`, x2many commands, field assignments |
| ↳ `odoo-vals-key` | a key that is not a field of the model |
| ↳ `odoo-vals-type` | a value the field does not take |
| `odoo-domain` | the structure, field paths, operators and values of literal domains |
| ↳ `odoo-domain-model` | a domain of another model than the one that takes it |
| ↳ `odoo-domain-search` | terms that only Odoo's SQL search rejects or silently ignores |
| `odoo-xmlid` | an `env.ref(...)` id that no data file of the closure creates |
| ↳ `odoo-xmlid-depends` | an id that only an addon outside the closure creates |

Typical false positives and what to do about them:

| Code | Typical false positive | Workaround |
|---|---|---|
| `odoo-model` | a model of an addon that is not on the addons path (enterprise) | add the addon's directory to `addons_path`, or `# type: ignore[odoo-model]` |
| `odoo-model-depends` | optional-dependency code under a guard the plugin does not detect | a [detected guard](#optional-dependencies-guards), or `disable_error_code = ["odoo-model-depends"]` |
| `odoo-vals-key` | a field created in the database (`x_*` is ignored by default), or added by an optional addon | `vals_ignore_keys = ["x_*", "studio_*"]`, or a guard (`if "f" in self._fields:`) |
| `odoo-vals-type` | a value of a dynamic type the plugin cannot narrow | `typing.cast`, or `# type: ignore[odoo-vals-type]` |
| `odoo-field-ref` | a method or field added by a later addon the declaring addon does not depend on (Odoo would not load it either) | add the dependency |
| `odoo-domain` | an operator added by a custom `@operator_optimization` (Odoo 19+) | `# type: ignore[odoo-domain]` |
| `odoo-domain-search` | a field that a custom field class makes searchable | `disable_error_code = ["odoo-domain-search"]` |
| `odoo-domain-model` | a domain written for one model, run on another through code the plugin does not see | `cast(Annotated[list, "odoo.domain.x"], domain)` |
| `odoo-xmlid` | an id created by Python code (hooks, `_create_xmlid`) in a closure addon | `raise_if_not_found=False`, or `# type: ignore[odoo-xmlid]` |
| `odoo-xmlid-depends` | test code that runs only with that addon installed | guard it with `if env.ref("x.y", raise_if_not_found=False):` |

Disable a code everywhere with `disable_error_code = ["odoo-domain"]` in `[tool.mypy]`,
or for some modules in a `[[tool.mypy.overrides]]` table. Silence one line with
`# type: ignore[odoo-xmlid]`.

## Unknown models: `odoo-model`

`odoo-model` reports a model name that no addon on the addons path defines (a typo), an
`_inherit` defined only after the class, and an `_inherit` cycle:

```text
error: Unknown Odoo model "res.partnr"  [odoo-model]
```

`odoo-model-depends` (a sub-code of `odoo-model`) means the model exists, but only in
addons outside the `depends` closure:

```text
error: Odoo model "sale.order" in _inherit is defined in addon "sale", which is not in
the dependencies of "my_addon"  [odoo-model-depends]
```

- In an `_inherit` that is a real error: Odoo cannot load the class.
- In `env["..."]` or a comodel it may be optional-dependency code, so the code can be
  disabled on its own with `disable_error_code = ["odoo-model-depends"]` (typos are
  still reported). `env[...]` is `Any` there.
- `# type: ignore[odoo-model]` covers both codes.

## Optional dependencies: guards

Code that runs only when a model is installed is not reported at all (with either code),
and its records are `Any`:

- the body of `if "sale.order" in self.env:` (also `self.env.get("sale.order")`,
  `self.pool`, `env.registry`, inside `and`);
- the `else` of `not in`;
- the rest of the block after `if "sale.order" not in self.env: return` (or `raise` /
  `continue` / `break`);
- `x if "sale.order" in self.env else y`;
- the operands of `and` after the test (`"intrastat_code_id" in self._fields and
  self.env["account.intrastat.code"].search(...)`), and those of `or` after a test that
  is false there.

The test may also be a flag assigned once in the function (`has_sale = "sale.order" in
self.env`, then `if has_sale:`) or a method of the same class whose body is
`return <test>` (`if self._has_sale():`).

A **field** that another addon may add is guarded the same way by
`"x_flag" in self._fields`, `self._fields.get("x_flag")`, `"x_flag" in self.env["model"]`
or `hasattr(record, "x_flag")`. There the name is not reported as an unknown key of
`vals`, a domain field, or a field of `mapped()` / `filtered()` / `sorted()` /
`grouped()` / `read()` (whatever the records are).

**Reading** such a field needs the question to be about the records that are read:
`if "phone_sanitized" in partner:` (or `in partner._fields`) gives `partner` the
attribute from there on, in the block the question is in, as mypy does for
`hasattr(partner, "phone_sanitized")`; its type is not known. A question about the
model (`"f" in self.env["model"]`, `"f" in records._fields`) also covers the records of
that model which the function gets from `env[...]` or from the attributes of a test
case after it. A variable that got its records before the question keeps its type: ask
the variable itself. A field the model has keeps its type.

Code written **for an addon outside the dependencies** also uses that addon's models.
A model is not reported (and its records are `Any`):

- under the guard of a field that no model of the dependencies has
  (`if "payment_provider_id" in lines._fields:` in `account`, then
  `self.env["payment.provider"]`): only another addon can add it;
- in a function that passes the name of the addon that defines the model to a call
  (`cls.ensure_installed("sale")`, `modules._get("sale").state == "installed"`, then
  `cls.env["sale.order"]`), or names it in the test of an `if` the line is under
  (`if modules.search([("name", "=", "sale"), ("state", "=", "installed")]):`, also
  `if not ...: return` before the line). A name inside a domain or a comparison
  anywhere else (`("type", "=", "sale")`) does not count.

Records of a model that such an addon **extends** are `Any` where it runs, so what it
adds is no unknown attribute (`self.env.company.sale_automatic_invoice`,
`self.env.ref("payment.payment_provider_transfer")._transfer_ensure_pending_msg_is_set()`):

- under the test of an `if` about installed modules that names the addon, or that ends
  the function (`return`, `raise`, `self.skipTest(...)`) before the line:
  `if self.env["ir.module.module"]._get("sale").state == "installed":`,
  `if ..._get("payment_custom").state != "installed": self.skipTest(...)`, also through
  a name bound once (`module = self.env["ir.module.module"].search([("name", "=",
  "l10n_eu_oss")])`, then `if module:`); a test that holds neither `"ir.module.module"`
  nor `"installed"` names no addon;
- under the guard of a field that no model of the dependencies has
  (`if "payment_state_before_switch" not in self.env["account.move"]._fields: return`);
- in the block of `hasattr(records, "name")` of a name their class lacks: the records
  are `Any` there (`if hasattr(ResPartner, "check_vat"): ResPartner.fix_eu_vat_number()`).

A test names a module where Odoo's API takes one: `_get("sale")`,
`ensure_installed("sale")`, `"sale" in modules._installed()`, a `("name", "=", "sale")`
term. A module that is not on the addons path (Odoo Enterprise's `accountant`) may
extend any model: all records are `Any` where such a test decides the line. The test may
also read an attribute that the methods of the class bind once
(`cls.document_installed = "documents_account" in modules._installed()` in
`setUpClass`, then `if self.document_installed:`).

What an addon **runs with** is there with its own dependencies: after
`self.ensure_installed("sale")` (the rest of its block), or under such a test, the
models, XML ids and fields of `sale`'s closure (`sales_team.group_sale_salesman`, the
`formula` that `account_tax_python` adds) are not reported. So are those of the addons
Odoo installs by itself with the closure: an addon whose `auto_install` addons are all
in it (`account_payment`, `auto_install: ["account"]`, adds `payment_token_id`;
`base_import`, `auto_install: True` with `depends: ["web"]`), and its dependencies
(`payment`). A model that no addon on the path defines is not reported where an addon
off the path is tested for; what such an addon depends on is not known.

## Field definitions and field names: `odoo-field-ref`

`odoo-field-ref` covers the names that Odoo resolves when it loads the registry:

```text
error: Odoo model "res.partner" has no field "citty" in related="partner_id.citty";
did you mean "city"?  [odoo-field-ref]
```

- Each field of a `related="a.b.c"` path, followed through the comodels; the related
  field must have the class of the path's last field. A relational field declared only
  with `related=` gets the comodel of the path's last field.
- The method names of `compute=`, `inverse=`, `search=` and `selection=` (a string),
  looked up in the declaring addon's view of the model.
- The paths of `@api.depends(...)`, checked the same way. `@api.constrains(...)` and
  `@api.onchange(...)` take field names of the model only (Odoo ignores a dotted name
  there).
- The string argument of `mapped("a.b")`, `filtered("a.b")`, `sorted(...)` and
  `grouped("a")` (a literal, or a `Final` / `Literal` str). `grouped`, and `sorted`
  before Odoo 19, take one field name; `sorted` in 19+ takes an order such as
  `"name desc, id"`.
- The field names of `read([...])`, `search_read(domain, [...])`, `fetch([...])` and
  `search_fetch(domain, [...])`.
- `ondelete=` of a `Many2one`: `"set null"`, `"restrict"` or `"cascade"`, and not
  `"set null"` together with `required=True` (Odoo raises when it sets the field up).
- `currency_field=` of a `Monetary` field (a field of the model), `inverse_name=` of a
  `One2many` (a field of the comodel) and the method name of `group_expand=`.
- Orders: the `order=` of `search` / `search_read` / `search_fetch` / `_search` (a
  literal, or a `Final` / `Literal` str) and `_order` in a class body. The string must
  be an order (`"date desc, partner_id.name nulls last, id"`) and each term must start
  with a field of the model:

  ```text
  error: Odoo model "sale.order" has no field "date_ordr" in search(order=...); did you
  mean "date_order"?  [odoo-field-ref]
  ```

- The class attributes that name fields: `_rec_name`, `_parent_name`, `_fold_name`,
  `_active_name` and the items of `_rec_names_search`.
- The specifications of `_read_group(domain, groupby, aggregates)` (and of
  `read_group` in Odoo 20): each names a field; a `groupby` item of a date field may add
  a granularity (`"date:month"`), an `aggregates` item is `"__count"` or
  `"field:function"` (`"amount:sum"`). Of the `read_group` of Odoo 17-19 only the
  fields of `groupby` are checked.

Abstract models are not checked: their fields refer to the models that use them.

## Selection keys in comparisons: `odoo-selection`

A literal string compared with a `Selection` field must be one of its keys, or the
condition is silently always false (always true for `!=` / `not in`):

```text
error: Value "confirmd" is not in the selection of field "state" of Odoo model
"sale.order" ("draft", "sent", "sale", "cancel")  [odoo-selection]
```

- `record.state == "x"`, `"x" != record.state`, `record.state in ("x", "y")` and
  `not in`, with a literal tuple, list or set.
- Only for a selection given as a literal list (with the keys every `selection_add` of
  the addon's view adds); not for a method or a constant the scan cannot read.
- A key that an addon outside the `depends` closure adds with `selection_add` is not
  reported: `provider.code == "custom"` is how code asks about an addon that may not be
  installed.

## Values of `create` and `write`: `odoo-vals`

`odoo-vals` covers:

- the `vals` of `create()`, `write()` and `copy(default=...)`;
- the vals inside x2many commands (`Command.create({...})`, `Command.update(id, {...})`,
  `(0, 0, {...})`), checked against the comodel;
- the ids in x2many commands: `Command.link(partner.id)`, `(4, partner.id)`,
  `Command.set(partners.ids)`, `(6, 0, partners.ids)` on a field of another comodel
  link whatever records have those ids; and a tuple whose first item is not 0 to 6;
- values assigned to fields (`record.partner_id = "x"`);
- the `default_<field>` keys of `records.with_context(...)` followed by `.create(...)`
  (keywords, a dict, or a variable holding such records). A key of a field the model
  lacks is reported here only when it is a near miss of one (a typo), since Odoo ignores
  it and code passes defaults for fields of optional addons.

It has two sub-codes:

- `odoo-vals-key`: a key that is not a field of the model as the addon sees it (fields
  of later addons and of `_inherits` parents included). A key that the `create` /
  `write` of a class of the model reads from the values (`vals.pop("counterpart_account_id",
  None)`, `.get`, `vals["k"]`, `"k" in vals`, also through `for vals in vals_list:`) is
  taken: the override consumes it;
- `odoo-vals-type`: a value the field does not take.

```text
error: Incompatible type for field "x_score" of Odoo model "res.partner" (expression has
type "str", Integer field takes "int | NewId | Literal[False] | None")  [odoo-vals-type]
error: Odoo model "res.partner" has no field "nmae"; did you mean "name"?  [odoo-vals-key]
error: Incompatible records for field "partner_id" ... (expression has "res.users"
records, Many2one field takes "res.partner" records)  [odoo-vals-type]
```

**What is read.** Dict displays and `dict(k=v, ...)` (for `create`, also a list of them),
and a variable built before the call in the same function: its literal value and what is
added to it in place (`vals["k"] = v`, `vals.update(...)`, `vals.setdefault(...)`,
`vals_list.append({...})` / `+= [...]`, through other variables too).

- The flow is read in source order: the last assignment before the call, then the
  changes after it.
- A variable assigned from a call (`vals = self._prepare_vals()`) keeps only the keys
  added afterwards.
- `**spread` and computed keys are not checked.
- In abstract models (mixins, `base`) keys are not checked, since their methods run on
  models with more fields.

**What each field takes** follows Odoo's `convert_to_cache`:

- `False` and `None` clear a field;
- `Many2one` takes an id, a record of its comodel or an `(id, name)` pair;
- a `Selection` literal must be one of its keys, including the `selection_add` of the
  addon's view;
- a plain `bool` is taken wherever `False` is, since `x = cond and value` and
  `min(..., default=False)` are typed `bool`;
- `Boolean` (`bool(value)`), computed `Binary` fields that are not stored (widget data),
  `Json` and `Properties` are not checked;
- a field of a class that an addon defines (`class BigInteger(fields.Integer)`) is
  checked as the Odoo class it derives from, unless the class or one of its bases
  defines a `convert_to_*` method: its values are then its own, and not checked.

## Domains: `odoo-domain`

`odoo-domain` covers the `domain` of `search()`, `search_count()`, `search_read()`,
`search_fetch()`, `filtered_domain()`, `read_group()`, `_read_group()`, `_search()`, the
domain parameter of `name_search()` / `_name_search()` (Odoo 17) /
`_read_grouping_sets()` (Odoo 19+), the `domain=` of relational fields (checked against
the comodel, also as `domain=lambda self: [...]`), what a field's `search=` method
returns (a domain of its model), and the domains of
[`odoo.domain.*` annotations](#annotated-domains-odoodomain).

```text
error: Odoo model "res.partner" has no field "x_scroe" in domain; did you mean
"x_score"?  [odoo-domain]
error: Value "dun" is not in the selection of field "state" ...  [odoo-domain]
error: Field "total" of Odoo model "sale.order" cannot be searched (not stored, no search
method): Odoo 18 ignores the condition and the search matches every record
[odoo-domain-search]
```

**Domain forms understood:** a list, `[...] + [...]`, `expression.AND([...])` / `OR`,
`a if c else b`, `a or b`, and on Odoo 19+ `Domain([...])`, `Domain(path, operator,
value)`, `Domain.AND` / `OR`, `&`, `|`, `~`. Also a variable built before the call in
the same function (`domain = [...]`, `domain += [...]`, `domain.append(term)`,
`domain &= Domain(...)`), read in source order as for `vals`, and a helper, parameter or
variable [annotated](#annotated-domains-odoodomain) with its model.

**Structure**, in every domain of an addon file, whatever its model (also of a mixin):

- a term has three items (`Domain term must be (field, operator, value), not 2 items`)
  and starts with a field name (`(1, "=", 1)` / `(0, "=", 1)` are Odoo's constants);
- an item is a term or `&` / `|` / `!`: `None`, a dict, a number or a domain where a term
  should be (`[[...], ...]`, a variable typed `list[tuple[...]]`) is an error;
- in a whole domain (an argument, a returned value, an item of `AND([...])`, a branch of
  `a if c else b`), `&` and `|` have two terms after them and `!` one
  (`Domain operator "|" needs 2 terms, has 1`). A piece joined with `+`
  (`["|"] + a + b`) is not a whole domain; a variable extended in place with lists is
  checked as the list it becomes.

**Each term with a literal path is checked** on a concrete model (or the model of an
annotation):

- its fields, through the comodels;
- its operator: the operators of the configured Odoo version, case-sensitive except in
  Odoo 19, which lowers them (`"IN"` is an error in 17, 18 and 20); Odoo's rewrites
  come first (`<>` to `!=` before 20, `==` to `=` in 19, `=` with a list to `in`);
- `any` / `not any` sub-domains, against the comodel (`("id", "any", ...)` against the
  model itself on Odoo 19+);
- a string value of a `Selection` field with literal keys;
- the rules below, taken from Odoo's code where it raises or silently does something
  else, on literal values (and, for records as a value, on the value's type).

Some terms fail only in Odoo's SQL search: `filtered_domain()` runs its own Python code
in Odoo 17/18 and accepts them. Those rules have the `odoo-domain-search` code and are
checked only in domains that a search takes (the search methods, `domain=` of fields,
what a `search=` method returns), not in `filtered_domain()` nor in what an annotated
helper returns, which may go to either.

| Rule | Versions | Code |
|---|---|---|
| a date part after a date field (`date.month_number`): none in 17; the `READ_GROUP_NUMBER_GRANULARITY` names in 18; with `tz` in 19; with `hour`, `day`, `week`, `month`, `quarter`, `year` in 20 | all | `odoo-domain` |
| a date part with `=?`, `child_of`, `parent_of` | 18 | `odoo-domain` |
| a `Properties` path: exactly `field.property` (17/18); the operators a property takes | all | `odoo-domain` |
| a `Binary` / `Image` path may end with `.size` / `.filename` | 20 | (allowed) |
| `any` / `not any` with a domain on a non-relational field | all | `odoo-domain` |
| `any` with a domain on `id` | 17, 18 | `odoo-domain-search` |
| `child_of` / `parent_of` on a non-relational field other than `id` | all | `odoo-domain` |
| `<`, `>`, `<=`, `>=` on a One2many / Many2many field | all | `odoo-domain-search` |
| `<`, `>`, `<=`, `>=` with a string on a Many2one field | 17, 18 / 19, 20 | `odoo-domain-search` / `odoo-domain` |
| `<`, `>`, `<=`, `>=`, or a `like` operator with a value, on a Boolean field | 19, 20 | `odoo-domain` |
| `=like` / `=ilike` (and `not ...`) with a value that is not a string | 19, 20 | `odoo-domain` |
| a string that is no number for an Integer / Float field | all | `odoo-domain-search` |
| `in` / `not in` with a scalar (or a set) on a stored non-relational field or `id` | 17, 18 | `odoo-domain-search` |
| records as the value of a field other than an x2many (`id` too), with a comparison operator | 17, 18 | `odoo-domain-search` |
| an integer for a Date / Datetime field (not `=?` with `0`, which Odoo makes a true condition) | 19, 20 | `odoo-domain` |
| `access` on a field other than a Many2one or `id`, or with a value other than `read`, `write`, `create`, `unlink` | 20 | `odoo-domain` |
| `any!` / `not any!` in a list term (allowed in `Domain(path, "any!", value)` and in what a `search=` method returns) | 19, 20 | `odoo-domain` |
| `inselect` / `not inselect` anywhere but in what a `search=` method returns (Odoo reads that as an internal domain) | 17 | `odoo-domain` |
| a field of the path that cannot be searched: not stored, no `search=` (or `compute_sql=` in 20), not a related field whose path can be searched; `display_name` in 17 | all | `odoo-domain-search` |
| a stored `Binary` / `Image` kept as an attachment (any stored `Binary` in 20) with anything but an existence check (`=` / `!=` with a falsy value, `in` / `not in` of falsy values) | all | `odoo-domain-search` |

Not checked, on purpose: a Boolean compared with a string (Odoo reads `"True"` /
`"False"` differently between versions, and `"True"` works everywhere), selection keys
outside a literal selection, the text of date values (`"2024-13-01"`, Odoo 19's
`"today +3d"`), `having=` of `_read_group`, and terms whose failure depends on the data
(`child_of` on a comodel without a parent field).

A string domain (`domain="[('nme', '=', 'x')]"` of a field) is evaluated by the client,
so only its field paths and operators are checked, and `parent.x` is skipped. Terms
built from variables and the terms of domains of abstract models are not checked.

On Odoo 19+, the parameters Odoo annotates `DomainType` (`search(domain=...)`...) take a
`list` of any items in addon files: a domain built in a variable is inferred as
`list[tuple[str, str, str]]`, which plain mypy rejects there (`list` is invariant). The
items are the plugin's checks.

## XML ids: `odoo-xmlid`

`odoo-xmlid` covers `env.ref("module.name")`, the groups of `user.has_group("...")`,
`user.has_groups("a.b,!c.d")`, `self.user_has_groups(...)` (Odoo 17) and of a field's
`groups="..."`, and the action of `_for_xml_id("...")`. The XML id must either:

- be created by a data file (`data` / `demo` in a manifest: `<record>`, `<template>`,
  `<menuitem>`, `<act_window>`, `<report>`, the `id` column of `<model>.csv`, the
  `'xml_id'` keys of `<function model="ir.model.data" name="_update_xmlids">`) of an
  addon in the `depends` closure, or
- be one Odoo makes itself (`module.model_res_partner`,
  `module.field_res_partner__name`, `base.module_sale`).

Its sub-code `odoo-xmlid-depends` is an id that only an addon outside the closure
creates.

- The result is the record's model as the addon sees it: `env.ref("base.main_company")`
  is a `res.company` record. An id given by `_update_xmlids` does not say its model,
  nor does an id made at run time (`env.ref(f"account.{company.id}_tax")`): the record
  is `Any` there (Odoo 19 and 20 declare `BaseModel`, records of no model, whose every
  field would be an unknown attribute; `dynamic_env = "base"` keeps that).
- With `raise_if_not_found=False` the result is `| None`, and the check is off (the code
  expects the id may be missing). So is a line under
  `if env.ref("sale.x", raise_if_not_found=False):` for every id of `sale`.
- An id whose module is not an addon of the closure is reported only when an addon
  outside it creates it: ids made by Python code (`local_es.x` of a chart template,
  hooks) are not in any data file.
- An id given as a group must be a `res.groups` record, and the id of `_for_xml_id` an
  action (`ir.actions.*`): Odoo answers `False` for a group that does not exist, which
  hides a misspelled name. A group of an addon outside the `depends` closure is not
  reported: `has_group("website.group_website_designer")` is how code asks about an
  addon that may not be installed.
- Data files are read as data, never executed. An XML file is decoded in the encoding
  it declares, also a multi-byte one (`<?xml version="1.0" encoding="Shift_JIS"?>`, GBK,
  EUC-JP). An XML file in an encoding Python does not know, or not in the encoding it
  declares, or not well-formed, and a CSV file that is not UTF-8 are skipped: the ids
  they create are then reported as unknown.

## Fields that Odoo leaves untyped

- `Binary` and `Image` read as `bytes | Literal[False]` (Odoo 18-19 declare them without
  a value type; Odoo 20's own type is kept), except computed fields that are not stored:
  Odoo uses those to hold any value for a widget (a dict), so they stay `Any`.
- `Reference` reads as a record of the models of its literal `selection` (`BaseModel`
  when the selection is a method), or `False`. Odoo types it as the `str` of
  `Selection`, but the value is a record.
- With `literal_selection = true`, a `Selection` field with literal keys reads as
  `Literal["draft"] | ... | Literal[False]` (see [configuration](configuration.md#options)).
  Without it, a `Selection` read where a union of string literals that holds every key
  of the field is expected reads as those keys: Odoo 19+ declares
  `float_round(..., rounding_method: Literal["UP", "DOWN", "HALF-UP", ...])`, and
  `rounding_method=self.rounding_method` (keys `UP`, `DOWN`, `HALF-UP`) fits it, where
  `str` fitted no overload. `False` stays where the field may be empty (and is
  reported); a key the literals lack keeps the value `str`.

## Annotated helpers: `odoo.values` and `odoo.model`

A helper method that builds `vals` (`_prepare_invoice_line()`) or returns records has
no type mypy could check. The PyCharm plugin "Odoo IDE" names them with annotations;
the plugin gives the same annotations real types:

| Annotation | Type |
|---|---|
| `Annotated[dict, "odoo.values.res_partner"]` | the `vals` of the model: a `TypedDict` named `res.partner values` |
| `"odoo.values.res_partner"` | the same |
| `"odoo.model.res_partner"` | the model's records, as `env["res.partner"]` gives them in the addon |

```python
def _prepare_line(self) -> "odoo.values.account_move_line":
    vals: Annotated[dict, "odoo.values.account_move_line"] = {"name": self.name}
    vals["quantity"] = self.product_uom_qty
    vals["quantty"] = 1
    # error: TypedDict "account.move.line values" has no key "quantty"  [typeddict-unknown-key]
    # note: Did you mean "quantity"?
    return vals
```

**Where they work.** The model is named with `_` for `.` (`account_move_line`;
`"odoo.values.account.move.line"` works too). The bare string works in the annotations
of functions (parameters, return), variables (module, class, function) and type
comments, also inside other types (`"odoo.values.x | None"`,
`"Mapping[str, odoo.model.x]"`), in a module that has a regular class at its top level
(the plugin rewrites the module's annotations when mypy reaches it; a class-syntax
`TypedDict` or `NamedTuple`, a class whose bases mypy rejects and a class defined in a
function do not count). Use `Annotated[dict, "odoo.values.x"]` instead:

- in a module without such a class, where mypy reports `Name "odoo" is not defined`;
- in `cast(...)`, an expression, which mypy turns into a type later.

A type alias (`V = Annotated[dict, "odoo.values.x"]`, `V: TypeAlias = ...`,
`type V = ...`) is a plain `dict` in either form: mypy drops the metadata of
`Annotated` there. Annotate the helpers and variables themselves.

Outside any addon (scripts) the annotations mean what they mean to plain mypy:
`Annotated[...]` is a `dict`, a bare string is an undefined name.

**The model.** The alias is looked up among the models of the file's addon view, like
`env["..."]`:

- a name no addon defines is `odoo-model` (with a near match: `did you mean`);
- a model defined only outside the `depends` closure is `odoo-model-depends`;
- two models that differ only in `.` / `_` (`a.b_c`, `a_b.c`) are reported as
  ambiguous (`odoo-model`): there are none in Odoo 17–20.

The type is then `dict[str, Any]` (values) or `Any` (records). A guard such as
`if "x" in self.env:` does not apply: the annotation is on the signature. Silence the
line with `# type: ignore[odoo-model-depends]`.

**The keys of `odoo.values.<model>`** are the fields that *any* addon on the addons
paths gives the model, as Odoo assembles them: those of its own classes, of its
`_inherit` parents and mixins (`message_ids`, `image_1920`, `access_token`...) and of
`base`, then those of the models it delegates to (`_inherits`). Not only those of the
addon's dependencies: with those only, `res = super()._prepare_vals()` in a later addon
would have fewer keys than the later addon's own annotation, and every override would be
an error. So:

- a misspelled key is an error, with `Did you mean`;
- a field of an addon outside the `depends` closure is a key (this is what code under
  `if "f" in self._fields:` writes);
- a field that only the database has (Studio, `x_*`) is not: the `TypedDict` has one
  fixed set of keys, so `vals_ignore_keys` does not apply. Declare the field in the
  code, or `# type: ignore[typeddict-unknown-key]`;
- a class found only through a constant defined in another module
  (`from .consts import MODEL`, `_name = MODEL`) is not read for the keys: they come
  from the files whose `_name` / `_inherit` name the model as a string, or through an
  expression the file itself can evaluate (`_name: str = "x"`, a constant of the same
  module, `_inherit = _name`).

**The values** depend on the field class only (the same type for every addon):

| Field class | Takes |
|---|---|
| `Char`, `Text`, `Html`, `Selection` | `str \| bool \| None` |
| `Integer`, `Many2oneReference` | `int \| NewId \| bool \| None` |
| `Float`, `Monetary` | `float \| bool \| None` |
| `Date`, `Datetime` | `date \| str \| bool \| None` |
| `Binary`, `Image` | `bytes \| str \| bool \| None` |
| `Many2one` | `int \| NewId \| BaseModel \| tuple \| bool \| None` |
| `One2many`, `Many2many` | `list \| tuple \| BaseModel \| bool \| None` (commands, ids, records) |
| `Reference` | `str \| BaseModel \| bool \| None` |
| `Boolean`, `Json`, `Properties`, a class that converts values itself, a computed `Binary` that is not stored | `Any` |

A field of a subclass that an addon defines takes what the Odoo class it derives from
takes (`class BigInteger(fields.Integer)`: the `Integer` row).

So a selection key that does not exist and records of the wrong comodel are not errors
in these values: they are checked in the dict literals of `create()` / `write()`
([`odoo-vals`](#values-of-create-and-write-odoo-vals)).

**Where they go.** `create()` (one, or a list of them; for `super().create(...)` see
below), `write()`, `copy(default=...)`, `Command.create(...)` and any `dict` parameter
take them. Values
of the model, of its `_inherit` parents and mixins and of the models it delegates to
(`_inherits`: `product.product` takes `product.template` values) fit; values of another
model are an `odoo-vals-type` error:

```text
error: Values of Odoo model "sale.order" given to create() of Odoo model "account.move"  [odoo-vals-type]
error: Values of Odoo model "sale.order" in a command of field "line_ids" of Odoo model
"account.move", which takes "account.move.line" values  [odoo-vals-type]
```

Their keys are not checked again there (mypy checked them where the values were built).

**What mypy's `TypedDict` rules mean here:**

- `vals.update({"bad": 1})` is mypy's `Unexpected TypedDict key "bad"` (code `misc`);
- a copy, `{**vals}` or `dict(vals)`, is `dict[str, object]`, whose values read as `Any`
  (see the dicts of mixed values below): annotate the copy to keep the field types;
- a parameter annotated `odoo.values.x` takes a dict literal (checked) and other
  `odoo.values.x` values, but not a plain `dict` variable (`arg-type`): annotate the
  variable too;
- an override cannot narrow a parameter that the overridden method (Odoo 19+) annotates
  `dict[str, Any]` (`override`): annotate return values and variables instead;
- an override of a helper that Odoo annotates `-> dict` (`_catalog_prepare_new_line_vals`
  in Odoo 20, a few others in 19+) gets a plain `dict` from `super()`, which is not the
  values (`return-value`): write
  `return cast(Annotated[dict, "odoo.values.x"], super()._helper())`;
- `super().create(prepared)` with a list of values is `arg-type` on Odoo 18+: mypy
  calls no plugin hook for `super()` calls, so the parameter keeps Odoo's declared
  `list[dict[str, Any]]` (lists are invariant) when no `create` override without
  annotations sits between the class and `BaseModel`. Write
  `super().create(cast("list[dict[str, Any]]", prepared))`;
- `vals.get("name")` has the field's type; an unknown key is not an error there (as for
  any `TypedDict`), it gives `Any | None`;
- `reveal_type(vals)` prints every key.

**Records of `odoo.model.x` from different addons** are different classes, as
`env["x"]` ones are (see [Not yet supported](#not-yet-supported)).

## Annotated domains: `odoo.domain`

A domain that a helper returns or that comes as a parameter has no model mypy could
see. Annotate it with the model, in the style of `odoo.values` (this annotation is the
plugin's own: neither "Odoo IDE" nor odoo-ls has one):

| Annotation | Type |
|---|---|
| `"odoo.domain.res_partner"` | `list[Any]` (Odoo 17/18), `Domain \| list[Any]` (19+), with a marker of the model |
| `Annotated[list, "odoo.domain.res_partner"]` (any item type) | `list[Any]` with the marker: for variables that use `.append()` / `+=` |
| `Annotated[Domain, "odoo.domain.res_partner"]` (Odoo 19+) | `Domain` with the marker: for `&`, `\|`, `~` |
| `Annotated[DomainType, ...]`, `Annotated[DomainType \| None, ...]` (19+) | their members, `list[...]` as `list[Any]` |

```python
def _open_domain(self) -> "odoo.domain.sale_order":
    return [("stte", "=", "draft")]
    # error: Odoo model "sale.order" has no field "stte" in domain; ...

def _lines(self, domain: "odoo.domain.sale_order_line") -> None: ...

def action(self) -> None:
    self._lines(self._open_domain())
    # error: Domain of Odoo model "sale.order" given to parameter "domain" of "_lines",
    # which takes a domain of Odoo model "sale.order.line"  [odoo-domain-model]
```

**Where its literals are checked** (against the annotation's model, with the rules that
hold everywhere): a `return` of a function annotated to return a domain (also
`-> "odoo.domain.x | None"`, not in nested functions), the value of an annotated
variable (in a module, a function or an Odoo model class), the changes of an annotated
variable or parameter (`domain = AND([domain, [...]])`, `domain.append(...)`), and an
argument of an annotated parameter (of a method, also inherited, or a function). The
literals of an annotated variable that reach a search through it get the search rules
too.

**Where its model is checked** (`odoo-domain-model`): a search method on records, a
returned value, an annotated assignment, an argument of an annotated parameter, an
`any` sub-domain, `domain=` of a field. A domain of the model itself, of a model its
registry class inherits (`_inherit` parents and mixins) or of a model it delegates to
(`_inherits`) fits; a domain of a model that inherits the expected one does not (a
mixin has none of its fields). A method whose parameter is annotated with a model that
the receiver's model inherits (a mixin, or a model it extends with `_name` and
`_inherit`), called on the receiver, takes a domain of the receiver's model: at run time
`self` is the receiver.

The model of a domain comes from a declaration: a call of an annotated helper, a cast to
an annotated type, a name or attribute with an annotation. A variable without one (also
`d = other_domain`: mypy's inferred type is no declaration) has the model of its
assignments before the line when they all agree (each a call of an annotated helper, a
cast, an annotated name, or another such variable, of the same model); a parameter without an annotation, a
global, a conditional value, an assignment in another branch, and one later in a loop
around the line make it unknown (only its literals are checked then). A statement that
spans several lines and holds the variable it binds (`domain = [("a", "any", domain)]`
written over three lines) reads the earlier value.

The model is written with `_` for `.` (`sale_order`) or with dots; an unknown,
ambiguous or invisible model is reported as for `odoo.values`.

**Not supported:**

| What | Why | Instead |
|---|---|---|
| the bare string in a module without a regular top-level class | the rewrite runs from a class's analysis, as for `odoo.values` | `Annotated[list, "odoo.domain.x"]` (its type and the consumers' checks work; a `return` there is not checked) |
| the bare string in an expression (`cast("odoo.domain.x", d)`), a type alias | an expression becomes a type later; mypy drops `Annotated` metadata in an alias | `cast(Annotated[list, "odoo.domain.x"], d)`, annotate the helper or variable |
| `Annotated[dict \| Sequence \| ..., "odoo.domain.x"]` | a `Sequence` does not go to `search()` on Odoo 19+ | `list` or `Domain` (another base is a `valid-type` error) |
| literals in nested positions (`"list[odoo.domain.x]"`, dict values) | only expressions that are a domain are checked | annotate a variable or helper that holds one domain |
| a literal default of a parameter | rare (a mutable default) | — |
| a literal in an annotated attribute of a class that is no Odoo model (dataclass...) | the check would hide the value from those classes' plugins | the consumers check it |
| arguments of `super()._helper(domain)` | mypy calls no plugin hook for `super()` calls | assign the literal to an annotated variable first |
| domains in action dicts (`{"res_model": ..., "domain": [...]}`) | another mechanism | — |

A `search=` method annotated with `odoo.domain.*` is checked as a search method (the
search rules, an internal domain) against its annotation's model; one with another
return annotation is not checked (the check passes no type context, so a precise
annotation would reject literals).
`reveal_type(self._domain())` prints the type without the marker (mypy simplifies the
type of a call), and on Odoo 19+ a variable annotated with the bare string has no
`.append()` / `&` until narrowed, as with Odoo's `DomainType`: use one of the
`Annotated` forms there.

## Code outside any addon

Scripts, and Odoo's own `odoo/*.py` modules (which mypy always analyzes), have no
`depends` closure, so the plugin leaves them alone: `env["..."]` keeps Odoo's declared
type `BaseModel`, and model names are not checked there. This keeps such files free
(resolving them would mean scanning every installed addon). Put the code in an addon to
get real model types.

## Plain mypy errors you will see more often

With real recordset types, some plain mypy codes appear more often:

- `union-attr` / `arg-type` with `... | Literal[False]`: a field that is not required
  read as `str | Literal[False]` (Odoo's own typing). Check the value
  (`if record.name:`) first.
- `arg-type` with `int | NewId` (`record.id` of possibly new records, Odoo 17 and 18):
  pass `record.ids` / `record._origin.id`, or check `isinstance(record.id, int)`.
  `Command.link` / `update` / `delete` / `unlink` and `browse` take one as Odoo does;
  `browse` also takes `None` and `False` (`browse(context.get("active_id"))`), which
  give no records.
- `assignment` between two classes of the same model (`AccountMoveLine` of two addons):
  use another variable name, or annotate it.
- A **variable that the function binds more than once** (`products = []` then
  records, `proposal = lead.company_id` then `proposal = False`, `total = 0` then
  `total += line.balance`, one loop variable over two models, a wizard then the invoice
  it made) takes the type of each value where it is bound (`rebound_variables`, on by
  default): mypy takes a variable's type from its first value, and every later value
  of another type was an `assignment` error, then an `attr-defined` one. After each
  binding the variable has that value's type, so reads, calls and field checks see the
  records, `False` or numbers it holds; where branches that gave it values of different
  types meet (after an `if`, at the head of a loop) it is `Any`. `x = None` stays a
  placeholder that a later value replaces, as mypy takes it: after a loop that may not
  run (`x = None`, then `x = job` in a `while`) the variable is `Any`, where mypy said
  `X | None` (`rebound_variables = false` keeps that).
- **Records or `False`**: a variable that a binding may give `False`
  (`partner = line.partner_id if line else False`, `so_line = lines and lines[-1] or
  False`, `next((m for m in members if ...), False)`) keeps that `False` as
  `Literal[False]`, also when it is bound once: `if partner:` then leaves the records.
  mypy widened it to `bool`, and `if partner:` left `Literal[True]`, whose attributes
  were errors. Where branches meet, the variable is the union of their values.
- A dict or a list of mixed values (`values = {"partner": partner, "amount": 0.0,
  "lines": []}`): mypy types it by joining the values (`dict[str, object]`, a protocol
  they all satisfy such as `Container[Any]`, or a union), and every use of a value was an
  error (`"object" has no attribute "append"`). The plugin reads such a value as `Any`:
  `values["lines"]`, `.get()`, `.pop()`, `.setdefault()`, `.items()`, `.values()`,
  `rows[0]` and `for row in rows`, when the value type is `object` or a protocol of the
  standard library and the variable has no annotation, or when the variable is a local
  one whose only value is a dict display of values of different types (then
  `values[key] = x` takes any value too).
- A **record dict**: a local variable built only by dict displays with string keys
  (`context = {"default_move_type": "out_invoice"}`, also `{}`) that the function changes
  under string keys (`context.update({"default_partner_id": partner.id})`,
  `vals["name"] = name`). mypy takes its value type from the first display (or the first
  change of `{}`), so every change with a value of another type was an error. Its values
  read as `Any` and its changes take any value. A dict changed under keys that are not
  literals (`totals[line.id] = 0.0`) is a mapping and keeps its value type.
- A container without an annotation whose values are `int` takes a `float` too:
  `totals = defaultdict(lambda: 0)`, then `totals[key] += line.balance`. One whose values
  are `tuple[X, ...]` takes `list[X]`, and back: Odoo code stores `records._ids` (a
  tuple) and `records.ids` (a list) alike.
- `records[key]` with a key of type `Any` (`record[record._sequence_field]`, a private
  attribute read as `Any`) is `Any`: Odoo declares `__getitem__(int | slice) -> Self`
  and `(str) -> Any`, and mypy takes the first one for an `Any` key.
- A value that holds unrelated types joined into a union
  (`{"move_ids": moves.ids, "envs": [env0, env1], "journal_id": journal.id}` holds
  `list[int] | list[Environment] | int | NewId`) is read as `Any` too, in a container
  without an annotation (also an attribute: `self.data["envs"]`). A value's own union
  (`str | Literal[False]`, `int | NewId`, records or `None`) keeps its type.
- A container typed from an empty display (`dict[Never, Never]` of
  `next(rows, {})`) takes any key and holds `Any`, and so does one that mypy completed
  with a literal key.
- A **display of dicts, or a value with an empty display inside**
  (`expected = [{"id": partner.id, "debit": 0.0}, {"id": tax.id, "x": {}}]`,
  `vals = {"name": name, "line_ids": []}`, `dict((team, []) for team in teams)`), bound
  to a variable, is typed as `list[Any]` / `dict[Any, Any]`: mypy typed it from its items
  as `list[object]` (passed to a `list[dict]` parameter, an error) or asked for an
  annotation. `zip()`, `enumerate()`, `sorted()`, `reversed()`, `list()`, `tuple()`,
  `set()`, `iter()` and the iterators of `itertools` that give their arguments' items
  (`product`, `chain`, `zip_longest`, `islice`, `combinations`, ...) of a display or
  such a variable give its items as `Any` where they were the join: `for partner,
  company in product([no_partner, partner], [False, "New Company"])` then
  `create({"partner_name": company})` was an `odoo-vals-type` error for `object`.
- A **list that the function adds dict displays to** (`rows = []`, then
  `rows.append({"name": n, "tag_ids": [(4, tag.id)]})`, `rows.insert(0, {...})`, `rows +=
  [{...}]`) is `list[Any]`: mypy typed it from the first dict (its values as a union),
  and a later dict of another shape was an error. The dicts are still checked where the
  list is created from (`create(rows)`).
- A list bound to `[]` that the function extends with what may be no list
  (`plan.extend(groups.values())`, `lines.extend(order.order_line)`, a generator) is
  `list[Any]`: mypy completes `[]` from a list argument only, and asked for an
  annotation.
- **Records are a `Sequence` and a `Set`**: Odoo's models module registers `BaseModel`
  with them (`collections.abc.Sequence.register(BaseModel)`), which mypy does not
  follow. A parameter of a registered class takes records: `Sequence[T]` with `T` the
  records (`random.sample(products, 2)` is `list[ProductProduct]`), `Sequence[X]`
  records of a class of `X`.
- A **parameter its method tests against model names** takes the records of those
  models, whatever its annotation: Odoo 20 declares `message_post_with_source(source_ref:
  str | IrUiView)`, and the method passes `source_ref` to `_get_source_from_ref`, which
  tests `source_ref._name == "mail.template"`. The scan reads `p._name == "m"` (`!=`,
  `in (...)`) of a parameter in the methods of a model class, also through the methods of
  the class it passes the parameter to.
- A **parameter takes the containers its own class passes it**: Odoo 20 annotates
  `_track_execute(self, ..., trackings: dict[int, tuple[set[str], list[ValuesType]]])` in
  `mail.track.mixin`, and the mixin itself calls it with `{parent.id: (filtered_changes,
  ...)}` where `filtered_changes` is a list comprehension. The annotation is narrower than
  the code Odoo runs, so the parameter also takes a list there
  (`dict[int, tuple[set[str], ...]] | dict[int, tuple[list[str], ...]]`), in every
  class of the model that annotates the method, and so do the parameters it is passed on
  to (`_track_log`). An override that builds its own dict with lists and calls
  `super()._track_execute(...)` (`account.tax`) is then no error. The scan reads the
  displays and comprehensions in the calls a class makes of its own annotated methods,
  also through the names the calling method binds; a `list`, `set` or `tuple` where the
  annotation names another of `list`, `set`, `frozenset` counts. Only for classes in
  modules whose errors mypy does not show (Odoo's own, with `follow_imports = "silent"`):
  where your own class annotates `names: set[str]` and passes a list, the call is still
  reported.
- An **operator that `False` does not support** is one error. A field that is not
  required reads as `datetime | Literal[False]`, and `abs(picking.scheduled_date -
  out_date) <= timedelta(seconds=1)` is reported for the operand (`Unsupported operand
  types for - ("Literal[False]" and "datetime")`), which is what the declaration says.
  mypy then went on with the type `int.__sub__` returns: `timedelta | int` is an `object`
  for `abs`, and the comparison was a second error on the line (`Unsupported operand
  types for >= ("timedelta" and "object")`). What such an operation gives is `Any`.
- `filter(None, [prefix, record.name])` takes values that may be `False`: `None` as the
  function drops every falsy item.
- `Command.set(["account.tag_x", "own_record_1", tag.id])` (Odoo 19, 20 declare
  `Collection[int]`) takes the XML ids of data that a loader such as the chart
  template's `_load_data` replaces: a bare name, which the data itself may create, and a
  `module.name` the data files of the closure create or whose module is in the
  closure (ids that Python data of a test gives). Another string stays an error.
- A list of domain terms takes Odoo's `TRUE_LEAF` / `FALSE_LEAF` (`(1, "=", 1)` /
  `(0, "=", 1)`): a term whose first item is an `int`.
- `create()` takes any iterable of values (`create({...} for i in range(3))`): Odoo
  reads the list once.
- A container of records without an annotation (`result = {}`, then `result[line.id] =
  line`) takes records of no model too: `isinstance(value, models.BaseModel)` leaves
  `BaseModel`, which Odoo code stores with the records of the model they are.
- `cls.cr = cls.env.cr` in a test (Odoo 18 declares `TransactionCase.cr: Cursor` and
  `env.cr` is `Cursor | TestCursor`): an attribute of an Odoo class (not the
  environment) declared with a class derived from `Environment.cr`'s declared base reads
  and takes every cursor class of that module.
- A `NamedTuple` that Odoo annotates with field classes (Odoo 19's
  `Procurement(product_id: fields.Many2one, name: fields.Char, ...)`) takes and gives
  `Any` for those items: the field class stands for its value. So does the name a later
  class inherits (`StockRule.Procurement` of sale_stock).
- `Counter()`, `OrderedDict()` and `defaultdict(lambda: {...})` bound to a variable are
  typed in an `Any` context too, as the displays above.
- `records._fields["price_unit"]` is the class of that field (`Float`, with
  `get_digits()`), not the `Field` Odoo declares (Odoo 19, 20; in 17 and 18 any
  attribute of a field reads). A `Selection` field with a literal selection there is
  `Any`: Odoo declares `selection` as a list, a method name or a callable, and the field
  holds the list.

## Not yet supported

- `vals` returned by a helper that is not annotated with
  [`odoo.values.*`](#annotated-helpers-odoovalues-and-odoomodel)
  (`self._prepare_vals()`). A variable's value is its last assignment before the call
  in source order, leaving out the other arms of an `if` / `match` the call is in and
  the arms that end with `return` or `raise`; after an `if` without such an end, the
  value of its last arm stands for all of them.
- Domains returned by a helper or taken as a parameter without an
  [`odoo.domain.*` annotation](#annotated-domains-odoodomain), and string arguments of
  `mapped()` / `filtered()` known only at run time (not a literal or a `Final` /
  `Literal` str).
- Where `self` is a type variable (`Self` in the body of such an override, `-> Self`,
  `self: T`), mypy calls no signature hook: `self.create({...})` there is an `arg-type`
  error on Odoo 17 and 18 (`create` declares a list). The checks of the call still run.
- An unannotated override of a `-> Self` method (`create`, `copy`, `search`, `sudo`...)
  is typed `(self: Self, ...) -> Self` only when each `return` gives its own records by
  syntax (`self`, `super().create(...)`, `records.sudo()`, `a | b`, a variable bound to
  these). Another one (`return self.env["account.account"].union(...)`, a helper's
  result) stays unannotated: calls of it keep the receiver's type, but
  `super().create(...)` through it in a later class is `Any`.
- `self` is the class being written, so inside a class `self` sees the earlier classes
  of its model, not later ones of the same addon (`env[...]` does see them):
  `self.later_method()` is an error. What `self`'s `-> Self` methods give
  (`self.with_context(...)`, `self.filtered(...)`), iterating it (`for line in self`),
  indexing it and the set operators (`self - other`) are the addon's records of the
  model, which have them.
- Only the [guard forms](#optional-dependencies-guards) listed above are detected (a
  helper of another class or addon, or a flag assigned more than once, is not).
- `env[name]` with a name only known at run time is `Any` (option `dynamic_env`), so
  code on it is not checked.
