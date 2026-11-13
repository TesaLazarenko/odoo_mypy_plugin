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
  `has no attribute "name"`.
- **`fields.Many2one("res.partner")`** is `Many2one[Partner]`, not `Need type annotation`.
- **`self.partner_id`, `self.line_ids`** are the comodel's records, with the extensions
  the reading addon sees, not `Any`. So `self.x_score + self.name` is an error
  (`Unsupported operand types for + ("int" and "str")`) instead of silence.
- **`self.sudo()`, `.with_context(...)`, `.filtered(...)`, `.search([])`, `.copy()`**
  keep the receiver's model, also through unannotated overrides of `create`, `copy`,
  `search`...
- **`self.env.user`, `self.env.company`** are `res.users` / `res.company` records as the
  addon sees them.
- **Methods added to `base`** (`self._phone_format(...)` from `phone_validation`) exist
  on every model of an addon that depends on the adding addon.
- **`_inherits` and `delegate=True`**: `line.product_id.uom_id` (through `_inherits` of
  `product.template`) is the `uom.uom` record. Fields of the parents are delegated,
  methods are not.
- **Log-access fields**: `self.create_date` is `datetime | Literal[False]` and
  `self.create_uid` a `res.users` record, unless `_log_access` / `_auto` is `False`.
- **`from odoo.addons.my_base.models.x import Y`** (a custom addon) is the real class
  with the [overlay root](configuration.md#the-overlay-root), not `import-not-found`.
- **`mapped("partner_id.name")`** is `list[str | Literal[False]]`, `mapped("line_ids")`
  the comodel's records, and `grouped("partner_id")` a `dict` from the field's values to
  records.
- **`read(["name", "partner_id"])`** and `search_read(domain, ["name"])` with a literal
  list return a list of `TypedDict`s with `id` and those fields, typed as `read()` gives
  them (`partner_id` is `tuple[int, str] | Literal[False]`, x2many fields `list[int]`).
- **`env[records._name]`** has the type of `records` (`self` in a mixin is the model
  that uses it at run time).
- **Controllers**: `odoo.http.request` is a `Request` and `request.env` an
  `Environment`, so `request.env["res.partner"]` is checked like `self.env`. (Odoo 18-19
  build `request` with werkzeug, which mypy does not follow; Odoo 20 declares `env` as
  possibly `None`.)

**Odoo 17** declares no types at all: the plugin gives its fields, `self.env`,
`self.id`, record iteration, `create()` and the `api` decorators the types Odoo 18
declares.

## Error codes

Every check has its own error code; a sub-code is also disabled with its parent.

| Code | What it reports |
|---|---|
| `odoo-model` | a model name that no addon on the addons path defines; `_inherit` defined later; `_inherit` cycles |
| ↳ `odoo-model-depends` | a model that only addons outside the `depends` closure define |
| `odoo-field-ref` | names in field definitions, `@api.depends` / `constrains` / `onchange`, `mapped` / `filtered` / `sorted` / `grouped`, `read()` |
| `odoo-vals` | the `vals` of `create` / `write` / `copy(default=)`, x2many commands, field assignments |
| ↳ `odoo-vals-key` | a key that is not a field of the model |
| ↳ `odoo-vals-type` | a value the field does not take |
| `odoo-domain` | field paths, operators and selection values of literal domains |
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
- `x if "sale.order" in self.env else y`.

The test may also be a flag assigned once in the function (`has_sale = "sale.order" in
self.env`, then `if has_sale:`) or a method of the same class whose body is
`return <test>` (`if self._has_sale():`).

A **field** that another addon may add is guarded the same way by
`"x_flag" in self._fields`, `self._fields.get("x_flag")` or `hasattr(record, "x_flag")`.
There the name is not reported as an unknown key of `vals`, a domain field, or a field of
`mapped()` / `filtered()` / `sorted()` / `grouped()` / `read()` (whatever the records
are).

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
- The field names of `read([...])` and `search_read(domain, [...])`.

Abstract models are not checked: their fields refer to the models that use them.

## Values of `create` and `write`: `odoo-vals`

`odoo-vals` covers:

- the `vals` of `create()`, `write()` and `copy(default=...)`;
- the vals inside x2many commands (`Command.create({...})`, `Command.update(id, {...})`,
  `(0, 0, {...})`), checked against the comodel;
- values assigned to fields (`record.partner_id = "x"`);
- the `default_<field>` keys of `records.with_context(...)` followed by `.create(...)`
  (keywords, a dict, or a variable holding such records). A key of a field the model
  lacks is reported here only when it is a near miss of one (a typo), since Odoo ignores
  it and code passes defaults for fields of optional addons.

It has two sub-codes:

- `odoo-vals-key`: a key that is not a field of the model as the addon sees it (fields
  of later addons and of `_inherits` parents included);
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
  `Json`, `Properties` and custom field classes are not checked.

## Domains: `odoo-domain`

`odoo-domain` covers the `domain` of `search()`, `search_count()`, `search_read()`,
`search_fetch()`, `filtered_domain()`, `read_group()`, `_read_group()` and `_search()`,
and the `domain=` of relational fields (checked against the comodel).

```text
error: Odoo model "res.partner" has no field "x_scroe" in domain; did you mean
"x_score"?  [odoo-domain]
error: Value "dun" is not in the selection of field "state" ...  [odoo-domain]
```

**Domain forms understood:** a list, `[...] + [...]`, `expression.AND([...])` / `OR`,
and on Odoo 19+ `Domain([...])`, `Domain(path, operator, value)`, `Domain.AND` / `OR`,
`&`, `|`, `~`. Also a variable built before the call in the same function
(`domain = [...]`, `domain += [...]`, `domain.append(term)`, `domain &= Domain(...)`),
read in source order as for `vals`.

**Each term with a literal path is checked:**

- its fields, through the comodels (a date field may end with a date part such as
  `date.year_number`);
- its operator: the operators of the configured Odoo version (`<>` is gone in 20, `any!`
  is new in 19); logical operators other than `&`, `|`, `!` are errors;
- `any` / `not any` sub-domains, against the comodel;
- a string value of a `Selection` field with literal keys.

A string domain (`domain="[('nme', '=', 'x')]"` of a field) is evaluated by the client,
so only its field paths and operators are checked, and `parent.x` is skipped. Terms
built from variables, `(1, "=", 1)` and domains of abstract models are not checked.

## XML ids: `odoo-xmlid`

`odoo-xmlid` covers `env.ref("module.name")`. The XML id must either:

- be created by a data file (`data` / `demo` in a manifest: `<record>`, `<template>`,
  `<menuitem>`, `<act_window>`, `<report>`, the `id` column of `<model>.csv`) of an
  addon in the `depends` closure, or
- be one Odoo makes itself (`module.model_res_partner`,
  `module.field_res_partner__name`, `base.module_sale`).

Its sub-code `odoo-xmlid-depends` is an id that only an addon outside the closure
creates.

- The result is the record's model as the addon sees it: `env.ref("base.main_company")`
  is a `res.company` record.
- With `raise_if_not_found=False` the result is `| None`, and the check is off (the code
  expects the id may be missing). So is a line under
  `if env.ref("sale.x", raise_if_not_found=False):` for every id of `sale`.
- An id whose module is not an addon of the closure is reported only when an addon
  outside it creates it: ids made by Python code (`local_es.x` of a chart template,
  hooks) are not in any data file.

## Fields that Odoo leaves untyped

- `Binary` and `Image` read as `bytes | Literal[False]` (Odoo 18-19 declare them without
  a value type; Odoo 20's own type is kept), except computed fields that are not stored:
  Odoo uses those to hold any value for a widget (a dict), so they stay `Any`.
- `Reference` reads as a record of the models of its literal `selection` (`BaseModel`
  when the selection is a method), or `False`. Odoo types it as the `str` of
  `Selection`, but the value is a record.
- With `literal_selection = true`, a `Selection` field with literal keys reads as
  `Literal["draft"] | ... | Literal[False]` (see [configuration](configuration.md#options)).

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
| `Boolean`, `Json`, `Properties`, a custom class, a computed `Binary` that is not stored | `Any` |

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
- a copy, `{**vals}` or `dict(vals)`, is `dict[str, object]`: annotate the copy to keep
  the field types;
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

## Code outside any addon

Scripts, and Odoo's own `odoo/*.py` modules (which mypy always analyzes), have no
`depends` closure, so the plugin leaves them alone: `env["..."]` keeps Odoo's declared
type `BaseModel`, and model names are not checked there. This keeps such files free
(resolving them would mean scanning every installed addon). Put the code in an addon to
get real model types.

## Plain mypy errors you will see more often

With real recordset types, some plain mypy codes appear more often:

- `union-attr` / `arg-type` with `... | Literal[False]`: a field read as
  `str | Literal[False]` (Odoo's own typing). Check the value (`if record.name:`) first.
- `arg-type` with `int | NewId` (`record.id` of possibly new records): pass
  `record.ids` / `record._origin.id`, or check `isinstance(record.id, int)`.
- `assignment` between two classes of the same model (`AccountMoveLine` of two addons):
  use another variable name, or annotate it.

## Not yet supported

- `vals` returned by a helper that is not annotated with
  [`odoo.values.*`](#annotated-helpers-odoovalues-and-odoomodel)
  (`self._prepare_vals()`), or assigned in another branch than the last assignment
  before the call (source order stands for the flow).
- Domains returned by a helper (`self._get_domain()`) or taken as a parameter, and
  string arguments of `mapped()` / `filtered()` known only at run time (not a literal or
  a `Final` / `Literal` str).
- An unannotated override of a `-> Self` method (`create`, `copy`, `search`, `sudo`...)
  is typed `(self: Self, ...) -> Self` only when each `return` gives its own records by
  syntax (`self`, `super().create(...)`, `records.sudo()`, `a | b`, a variable bound to
  these). Another one (`return self.env["account.account"].union(...)`, a helper's
  result) stays unannotated: calls of it keep the receiver's type, but
  `super().create(...)` through it in a later class is `Any`.
- `self` is the class being written, so inside a class `self` sees the earlier classes
  of its model, not later ones of the same addon (`env[...]` does see them). The classes
  of one model are distinct types: a variable that first holds `env["x"]` (or a
  relational field of model `x`, as in `for line in self.move_id.line_ids:` then
  `for line in self:` in an earlier `account.move.line` class) cannot later be assigned
  `self` of an earlier class of `x` (an `assignment` error).
- Only the [guard forms](#optional-dependencies-guards) listed above are detected (a
  helper of another class or addon, or a flag assigned more than once, is not).
- `env[name]` with a name only known at run time is `Any` (option `dynamic_env`), so
  code on it is not checked.
