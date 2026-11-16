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
- A field is a call of a field class, however the file names the class: `fields.Char`,
  `from odoo.fields import Char`, `from odoo import fields as f`, an alias, and the
  subclasses that addons define (`class BigInteger(fields.Integer)`), also those of
  another addon, of a library and of Odoo's own modules (`odoo.orm.fields_numeric`),
  through any number of modules that only pass the class on. A function that returns a
  field (`def money(**kw): return fields.Float(**kw)`) and
  `functools.partial(fields.Float, ...)` make fields too. A subclass is a field of the
  Odoo class it derives from, with that class's positional arguments
  (`Partner("res.partner")`); one that defines a `convert_to_*` method has values of
  its own, which are not checked, and one that defines `__init__` (or a factory) takes
  positional arguments of its own. A name of `odoo.fields` that is no field class
  (`Domain(...)` in a class body) is no field.
- `env.ref("module.name")` is the record's model, from the XML ids of data files (also
  the ids `_update_xmlids` gives, whose model the data does not say). The record of an
  id whose model is not known (also an id made at run time) is `Any`, not Odoo 19's
  `BaseModel`, of which every field was an unknown attribute.
- `odoo.http.request` has the attributes addons set at run time (`request.website`,
  `.lang`, `.is_frontend`, `.website_routing`...), typed `Any`.
- `mapped("a.b")`, `grouped("a")` and `read([...])` / `search_read` return real types.
- Unannotated overrides of `-> Self` methods that return their own records keep the
  model type through `super()`.
- An attribute a class method defines (`cls.partner = ...` in `setUpClass`) is an
  attribute of the class and its subclasses, with the type of its value: plain mypy
  reports every such line and every later read (`"TestSale" has no attribute
  "partner"`), which was over 90 % of the errors left on Odoo's own addons, and the
  records of the tests were `Any`. The records a test class of another addon keeps read as the
  model the reading addon sees.
- `env.context` is a mapping on Odoo 20: a class without bases whose `__new__` builds
  another class's object (`frozendict` returns a `MappingProxyType`) has that class as
  its base.
- `from odoo import Command, _, SUPERUSER_ID` on Odoo 19 and 20, where `odoo` is a
  namespace package and a module of it binds the names (`odoo.Command = Command`): the
  package has them, with their types, also after mypy parses the package again for a
  stale import cycle (a changed option), which it does without asking plugins.
- `env.website` (Odoo 20, declared `BaseModel`) is the `website` records: an attribute
  of `Environment` declared as records of no model is the model it is named after.
- A controller's `self.env` is an `Environment` (Odoo 20 declares `Environment | None`
  on its `Controller`; what an addon's own class declares is left alone).
- A class that names its attribute hook after another method
  (`__getattr__ = __getitem__`, Odoo 20's `TableSQL`) has that method as the hook; what
  a `__getattr__` declared to return a base class of its own class gives is `Any`
  (`table.order_id.partner_id`: each step is declared `-> SQL`, which has no attributes).
- Records read through a model the file never names (`partner.user_ids`) are `Any` when
  the only classes mypy has loaded for them are fewer than the reading addon sees: the
  methods and fields its other dependencies add were unknown attributes.
- The test of an `if` names the addon whose models the lines under it use
  (`if modules.search([("name", "=", "website"), ("state", "=", "installed")]):`).
- A field takes what reading it gives: on Odoo 20 a binary field reads as a
  `BinaryValue`, and `tag.image = product.image_1920` was an `odoo-vals-type` error.
- A list of relational commands takes the plain tuples as well
  (`commands = [Command.update(...)]`, then `commands.append((2, line.id))`): Odoo 19
  and 20 type `Command` as an `int` enumeration.
- `browse(...)` takes what Odoo takes besides the declared `int | Iterable[IdType]`:
  `None` and `False` (its docstring: "int or iterable(int) or None"), so
  `browse(context.get("active_id"))` is no error, and one id of any kind a list of ids
  may hold (`record.id` is `int | NewId` on Odoo 18).
- `mapped(function)` is the list of the function's values, or its records (Odoo 19 and
  20 declare a union of both, which no `sum(...)` or `join(...)` accepts); `mapped` of
  a path known only at run time is `Any`, as on Odoo 17 and 18.
- `_inherits = {"res.partner": "partner_id"}` in a model class is no error on Odoo 19,
  which declares the attribute as the `frozendict` it builds from it.
- A field delegated through `_inherits` is required when the parent's field is, as in
  Odoo; `create_date` and `write_date` read without `| Literal[False]`
  (`required_values`).
- A local variable that its function binds more than once has the type of each value
  where it is bound (`rebound_variables`, on by default): `products = []` then records,
  `proposal = company` then `proposal = False`, `total = 0` then `total += line.amount`,
  one loop variable over two models were `assignment` errors and then unknown
  attributes, since mypy takes a variable's type from its first value. Where branches
  that gave it values of different types meet it is `Any`. A variable that may be
  given `False` keeps it as `Literal[False]`, so `if partner:` leaves the records
  rather than `Literal[True]`.
- What `self`'s `-> Self` methods give (`with_context`, `filtered`, `sudo`...),
  iterating, indexing and the set operators of records are the addon's records of the
  model: `self` of an earlier class did not fit a dict keyed by them or `concat()`, and
  `self.with_context(...).later_method()` was an unknown attribute.
- Records of a model that an addon outside the dependencies extends are `Any` where
  that addon runs: under a test about installed modules that names it (also ended by
  `self.skipTest(...)`, also through a name bound to a search of
  `ir.module.module`), under the guard of a field no dependency has, and in the block
  of `hasattr(records, "x")` of a name their class lacks.
- `records._fields["name"]` is the class of that field on Odoo 19 and 20 (`Float` has
  `get_digits()`); a `Selection` with literal keys is `Any` there.
- A module that is not on the addons path (Odoo Enterprise's `accountant`), named by a
  test about installed modules (`"accountant" not in modules._installed()`), may
  extend any model: the records are `Any` where it runs.
- A `NamedTuple` annotated with field classes (Odoo 19's `Procurement`) takes and gives
  `Any` for those items, also through a name a later class inherits
  (`StockRule.Procurement` in sale_stock).

### Checks

- Data built from displays is not typed from its first values: a variable bound to a
  display of dicts or to a value with an empty display inside (`[{...}, {...}]`,
  `{"lines": []}`, `dict((k, []) for k in keys)`) is `list[Any]` / `dict[Any, Any]`
  (mypy made it `list[object]`, or asked for an annotation); a union of unrelated values
  in a container without an annotation reads as `Any`; a container of an empty display
  (`next(rows, {})`) takes any key; `zip()` / `enumerate()` of such displays give `Any`
  items; `filter(None, ...)` takes values that may be `False`; `Counter()`,
  `OrderedDict()` and `defaultdict(lambda: {...})` need no annotation.
- `Command.set([...])` takes the XML ids of data that a loader replaces (a bare name, a
  `module.name` the closure's data creates, or of a module of the closure): Odoo 19 and
  20 declare `Collection[int]`.
- A list of domain terms takes Odoo's `TRUE_LEAF` / `FALSE_LEAF` (`(0, "=", 1)`).
- The operands of `and` after a guard are guarded (`"f" in self._fields and
  self.env["m"].search(...)`), those of `or` after a false test too.
- What a line runs with is there with its dependencies: after
  `self.ensure_installed("sale")`, under a test about installed modules (also through
  an attribute the class binds once, `cls.document_installed = ...`), and the addons
  Odoo installs by itself with the closure (`auto_install`: `account_payment` with
  `account`, `base_import` with `web`). Their models, XML ids and fields are no
  `odoo-model-depends`, `odoo-xmlid-depends` or `odoo-vals-key` errors.
- A list that the function adds dict displays to (`rows = []`, then
  `rows.append({...})`, `rows += [{...}]`), or extends with what may be no list
  (`plan.extend(groups.values())`), is `list[Any]`: mypy typed it from the first dict,
  and a later dict of another shape was an error, or it asked for an annotation.
- Records are the classes Odoo registers `BaseModel` with (`collections.abc.Sequence`,
  `Set`, read from Odoo's models module): `random.sample(products, 2)` is
  `list[ProductProduct]`, a `Sequence[Product]` parameter takes products.
- A key that a model's `create` / `write` reads from the values
  (`vals.pop("counterpart_account_id", None)`) is no `odoo-vals-key` error.
- A parameter that its method tests against model names (`source_ref._name ==
  "mail.template"`, also in a method of the class it passes the parameter to) takes
  the records of those models: `message_post_with_source(template)` on Odoo 20, which
  declares `source_ref: str | IrUiView`.
- A parameter takes the containers that the class which annotated it passes it:
  `_track_execute(trackings: dict[int, tuple[set[str], list[ValuesType]]])` of Odoo 20's
  `mail.track.mixin` is called by the mixin itself with a list where the annotation
  names a set, so an override that passes a list (`account.tax`) is no `arg-type` error,
  also through `super()` and the methods the parameter is passed on to. Only for the
  classes of modules whose errors mypy does not show; in the code being checked an
  annotation is its author's word.
- An operator that the `False` of an empty value does not support
  (`abs(picking.scheduled_date - out_date) <= delta`) is one error, about the operand:
  mypy went on with `int`, what `int.__sub__` returns, and reported the comparison too
  (`Unsupported operand types for >= ("timedelta" and "object")`).
- `create()` takes any iterable of values (`create({...} for i in range(3))`), which
  Odoo reads once.
- A container of records without an annotation takes records of no model too
  (`isinstance(value, models.BaseModel)` before `result[line.id] = value`); one of
  tuples of ids takes lists of them, and back (`records._ids`, `records.ids`).
- `records[key]` with a key of type `Any` is `Any`: mypy took Odoo's first overload
  (`int | slice -> Self`), and `record[record._sequence_field]` was records.
- `from ..tools import after_commit` in a model of an import cycle is the function, not
  the submodule of that name (Odoo 19+): the plugin's dependency edges weigh as imports
  inside functions, so they no longer order the cycle before real imports do.
- `cls.cr = cls.env.cr` in a test (Odoo 18): an attribute of an Odoo class declared as
  `Cursor` takes the cursor classes `env.cr` is (`Cursor | TestCursor`).

- Every check also runs where `self` is a type variable: in an unannotated `create` /
  `copy` override (typed `(self: Self) -> Self`), under `-> Self` and `self: T`.
- A `vals` or domain variable assigned in another arm of the `if` / `match` the call is
  in, or in an arm that returns, is not taken for its value; `match` statements are
  followed like `if`.
- Required fields and `display_name` read without `| Literal[False]`, except where the
  value types a variable whose later values mypy checks against it (it takes the type
  from the first one); option `required_values = false` reads them as Odoo declares them
  (a new record reads `False`). A variable that `rebound_variables` declares `Any`, one
  that only `+=` changes and an empty list whose later items are no `False` as written
  are not such variables: `label = self.name` then `label += " (x)"` is `str`.
  `read()` / `search_read()` give a required field's value without `False` too
  (`account["company_id"][0]`).
- `env.cr` has the methods of a cursor (`execute`, `fetchall`, `dbname`): Odoo 18 declares
  it as `BaseCursor`, which has none of them.
- A model of an addon outside the dependencies is not reported in code written for that
  addon: under the guard of a field that no model of the dependencies has
  (`if "payment_provider_id" in lines._fields:`), and in a function that passes the
  addon's name to a call (`cls.ensure_installed("sale")`). `"f" in env["model"]` guards
  a field like `"f" in records._fields`.
- A field asked for is read without an error: `if "phone_sanitized" in partner:` (or
  `in partner._fields`) gives `partner` the attribute, as mypy does for `hasattr`; a
  question about the model (`"f" in self.env["model"]`) also covers the records of that
  model the function types after it (`env[...]`, the attributes of a test case).
- A value read from a dict or a list of mixed values is `Any`, not mypy's join of the
  values (`object`, a protocol such as `Container[Any]`, a union):
  `values = {"partner": partner, "amount": 0.0, "lines": []}`, then
  `values["lines"].append(...)`, `values["amount"] += x`, `for key, value in
  values.items()`. Such a dict was `dict[str, Any]` while records were untyped. A local
  dict built by string-keyed displays and then changed under string keys
  (`context.update({"default_partner_id": partner.id})`) takes any value and reads as
  `Any`; a container without an annotation of `int` values takes a `float`. An annotated
  `dict[str, object]` and an unchanged dict of one type are read as before.
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
- A list of domain terms takes a term with another kind of value:
  `domain = [("state", "=", "draft")]` followed by
  `domain.append(("company_id", "in", companies.ids))` (also `insert`, `extend`, `+=`,
  `+`, and an operator such as `"|"`) is no `arg-type` error.
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

- The daemon keeps the addon index and the decoded scans, data and class records it read
  across the registries it starts after edits (it has no mypy cache): an update after an
  edit of one addon file takes 0.07 s instead of 0.22 s.
- Checks that only report are skipped in modules whose errors mypy hides
  (`follow_imports = "silent"`, `ignore_errors`): most field definitions of Odoo's own
  addons.
- Less memory: the XML ids of a closure are a view over one dict per addon, one shared
  empty `frozenset`, a lighter pool of field maps (-15 MB on 120 core addons).

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

- A hook that raises no longer ends the run with mypy's INTERNAL ERROR: its result is
  mypy's own there, and one line names the plugin, the file and the line
  (`ODOO_TYPING_RAISE=1` raises, as the test suite does).
- Warnings where checks would silently stop: `follow_imports = "skip"` for `odoo`, an
  addon shadowed by one of the same name earlier on the addons path, a manifest whose
  `depends` cannot be read. `scan_cache_dir = "~/..."` without a home keeps the scans in
  mypy's cache instead of crashing.
- `odoo-typing-baseline` runs mypy with one error per line (`--no-pretty`...), so a
  baseline recorded in a wide terminal matches a check in a narrow one.
- No false "analyzed twice" warning for an addon named like a module mypy loads
  (`json`); `env[b"x"]` is no model name.

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
