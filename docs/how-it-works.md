# How it works

[← README](../README.md)

- [The registry](#the-registry)
- [mypy hooks](#mypy-hooks)
- [Annotations: `odoo.values` and `odoo.model`](#annotations-odoovalues-and-odoomodel)
- [Incremental mode](#incremental-mode)
- [Shared scan cache](#shared-scan-cache)
- [The daemon (dmypy)](#the-daemon-dmypy)

## The registry

The registry ([registry/](../src/odoo_typing/registry/__init__.py),
[scanner/](../src/odoo_typing/scanner/__init__.py)) is a static AST scan that never
imports Odoo or touches a database. It mirrors Odoo 18's loading rules:

- **Addons** are directories with a `__manifest__.py` (or the deprecated
  `__openerp__.py`), named after their entry in the addons path (a symlink may point to
  any directory). Files are matched to addons by their resolved directory, so a path
  through the overlay root, a symlink or the real directory finds the same addon. Module
  names of an `addons_path` addon are recognized in both spellings (`my_addon.x`,
  `odoo.addons.my_addon.x`); the plugin itself only ever names the configured one.
- **Load order** is Odoo's module graph: `base`, then by dependency depth, then by name.
- **Within an addon**, classes are ordered as Python creates them: the scan replays the
  `__init__.py` imports statement by statement, so an imported module's classes come at
  the import, and imports or classes inside `try:` / `if` blocks count (not
  `if TYPE_CHECKING:` ones).
- **Model names** follow `MetaModel`: `_name = _description = "x"`, `_name = CONST`,
  `_inherit = _name` and f-strings of module constants are understood. Several
  `_inherit` entries without `_name` define a new model named after the class. A `_name`
  that cannot be computed statically (for example in a loop) is skipped.
- **Fields** are the calls of field classes in a class body. The class is found the way
  Python finds it, not by how the call is spelled: the scan follows what each name of
  the file is bound to (`from odoo import fields`, `from odoo.fields import Char`,
  `from .fields import BigInteger`, `Alias = BigInteger`) and what each class derives
  from. Odoo's classes are the ones `odoo.fields` has; any other class is a field class
  if one of its bases is, and its fields are fields of that Odoo class (`BigInteger` of
  `Integer`). A function whose every `return` calls a field class, and
  `functools.partial(<class>, ...)`, count as such a class. The scan of an addon cannot
  look outside the addon, so it names a class of another addon, of a library or of
  Odoo's own modules by its dotted name
  (`odoo.addons.queue_job.fields.JobSerialized`), keeps the call's positional arguments,
  and records which of its own classes and functions may be field classes (and the
  names it imports from other addons, which it may only pass on). The registry links
  them when it reads the scans: it follows the dotted name through the scans of the
  addons, and reads the source of a module outside them
  ([registry/sources.py](../src/odoo_typing/registry/sources.py): Odoo's own under the
  configured `odoo` package, a library on the interpreter's search path, never
  imported; what each module says is kept in the scan store with its file's stamp).
  There, Odoo's field classes are the names of `odoo.fields` whose classes
  derive from `Field`, wherever they are defined (Odoo 19 defines them in
  `odoo.orm.fields_*`), so `Domain` or `Command` is none. A subclass that defines a
  `convert_to_*` method has values of its own (they are not checked), one that defines
  `__init__` takes positional arguments of its own (they are not read). A call of
  anything else is no field.
- **Every model inherits `base` last**, like `_build_model` does, so methods that addons
  add with `_inherit = "base"` exist on `self` and on extensions of other models.
- **A model's class chain** is built like Odoo's registry class (`_build_model`'s
  `LastOrderedSet` of bases, linearized with C3): the model's earlier classes, then the
  registry classes of its `_inherit` parents, `base` last. A parent's registry class
  holds *all* its classes that the addon sees, also later ones: Odoo extends it in
  place, so a method that `sms` adds to `mail.thread` is on `project.task` in every
  addon that depends on both `project` and `sms`.

Everything is lazy: manifests are read on the first question (a fully cached mypy run
reads the stored index instead, see [Incremental mode](#incremental-mode)), and an addon
is scanned only when a file whose `depends` closure contains it needs a model.

Sources are parsed as bytes, so a UTF-8 BOM or a `# -*- coding: ... -*-` cookie works.
The compiler's warnings (an invalid escape sequence such as `"\d"`) are not printed, as
mypy does not print them, and `-W error` does not make them errors; the same holds for
manifests and string domains. A file that cannot be parsed is reported once on stderr,
since its models are then missing:

```text
odoo-typing: warning: cannot parse addons/my_addon/models/x.py: invalid syntax (line 3); the Odoo models it defines are ignored
```

The reason is the syntax error, `Permission denied` for a file that cannot be read (and
for `<dir>/__init__.py` when a package directory cannot be searched), or
`too complex to parse (RecursionError)` (or `MemoryError`) for code nested too deeply
for Python's parser. A file, manifest or directory that cannot be read is kept with a
stamp that no file has (see [Incremental mode](#incremental-mode)), so it is read again
once it can be.

## mypy hooks

- **`get_additional_deps`** makes mypy load (and analyze first) the modules of every
  class a file's models are made of: the class chains of its own classes, the models
  named in `env["..."]` and comodel literals, and the comodels of the relational fields
  its classes inherit (so `self.partner_id.<field added by a dependency>` resolves).

  A file that imports test modules of other addons also gets the classes of the models
  those modules name (and the ones they import in turn), as its own addon sees them: a
  test reads the records its base classes keep (`self.partner` of another addon's
  common test class) without ever naming their model. A class of a module that mypy
  has loaded but not analyzed yet is never merged into a view class.

  For the `odoo` package of Odoo 19+, which has no `__init__.py`, the same hook adds
  the imports of the names a module of the package binds on it (`odoo.Command =
  Command` in `odoo/init.py`) to the package's empty module, so `from odoo import
  Command` is the class. mypy parses a module again without this hook when another
  module of its import cycle is out of date, so every hook lookup also binds the names
  the package's current tree lacks (one dict lookup once it is done), and the package's
  cache entry carries the bound names as its config data.

- **Attributes of any class.** mypy asks for the MRO hook of every class, before it
  analyzes the class body. For each class method the plugin marks the first parameter
  as the method's `self`, so mypy itself defines the attributes of
  `cls.name = value` (types, `cls.rows = []` / `.append`, later assignments). mypy
  keeps such definitions for instances only: the names assigned through the first
  parameter are kept in the class's metadata (so in mypy's cache), and
  `get_class_attribute_hook`, which mypy asks right before it looks an attribute up on
  a class, makes the definition readable there. An attribute hook then re-resolves a
  record kept on a class that is no model (a test case) from the reading addon, like a
  relational field. A class without bases whose `__new__` returns another class's
  object (Odoo 20's `frozendict`; read from the source, since mypy drops the function
  bodies of modules whose errors it does not show) gets that class as its base, and a
  class with `__getattr__ = <method>` that method as its hook. The signature hooks of
  the functions and methods Odoo declares with a `dict[str, ...]` parameter add the
  class `Environment.context` is declared with, where it is no `dict` (`context_class`),
  to that parameter in addon files: Odoo 20 passes `self.env.context` to
  `clean_context(context: dict[str, Any])` itself.

- **`get_customize_class_mro_hook`** gives each model class the MRO of its registry
  class, computed by the registry. It is never copied from a base class, so it does not
  depend on the order in which mypy analyzes the classes of an import cycle.
  - When a class of the chain is not analyzed yet, mypy is asked to analyze the class
    again (classes outside the run, or still missing on mypy's final pass, are left out,
    and unknown attributes then give `Any`). A class that mypy never analyzes although
    its module is done (an unreachable `if sys.version_info < ...` branch) does not
    exist for mypy and is simply left out.
  - Written bases keep their type arguments (`class N(Mix[int])`). Python's own MRO of
    the written bases is respected; when it contradicts the `_inherit` order (Odoo
    cannot load such a class either), all bases are kept and a note says so.
  - It also reports, with code `odoo-model`, what Odoo would refuse to load: an
    `_inherit` entry that no addon in the `depends` closure defines (naming the addon
    that does, if any, with code `odoo-model-depends`: the fix is usually a missing
    `depends`), one that is only defined after the class, and `_inherit` cycles (whose
    edges are then ignored). Finding the defining addon outside the closure is one text
    search over all addons (about 0.3 s, only in a run that reports an unknown model
    name).

- **`_inherits`** and **`Many2one(..., delegate=True)`**: once a model class's body is
  analyzed, the fields (not the methods) of the delegated models' registry classes, as
  the class's addon sees them, are added to it unless the class already has the name,
  like Odoo's `_add_inherited_fields`. `_inherits` of mixins count (`mail.alias.mixin`).
  The field variables are shared with the parent's classes, so their types are inferred
  once. A model whose delegated parent is extended outside the tip's view gets a merged
  `env[...]` class.

- **Decorators and `-> Self` methods**: `@api.private` and `@api.returns(...)` have no
  annotations, which made `sudo`, `with_context`, `filtered`, `search`, `copy`... `Any`.
  A function hook keeps the decorated signature (also for `api.model`, `api.readonly`,
  `api.model_create_multi`). An override of `create`, `copy`, `search`, `browse`,
  `sudo`, `with_*`, `filtered`, `sorted`, `exists` or `ensure_one` written without
  annotations returns the receiver's model; `create` also accepts one dict, as
  `model_create_multi` does at runtime.

- **Overrides that return their `super()` call**: `product.product` overrides `_search`
  without annotations and returns `super()._search(domain, ...)`, which made every
  `_search` of products `Any` where Odoo declares `Query`. A method without annotations
  every `return` of which gives `super().<the same method>(...)`, or a local bound once
  to it, and that ends in a `return` or `raise` on every path, passes on what the
  definition that `super()` reaches returns: the next class of the MRO that has the
  method, annotated there, or such an override again (a chain), undecorated or with
  decorators that keep the signature (`api.model`). The scan reads those methods from
  the source (mypy keeps no bodies of the modules it does not report on). The type is
  given where the method is called, by a method hook that finds `Any` for the call of an
  unannotated method of a model class and follows the receiver's MRO; the methods
  themselves stay as written. So an annotated override of a later class is compared
  with what its bases declare themselves (no second `override` error for each class in
  between), classes of one module or import cycle work (mypy analyzes signatures after
  the class bodies), and the daemon has no rewritten signature to lose. A declared
  `None`, `Any`, an overload, a property or a type variable (`-> Self`, see above) gives
  no type; `super().m(...)` itself keeps mypy's type (no hook for `super()` calls).

- **`env.user` / `env.company` / `env.companies`**: records of `res.users` /
  `res.company` as the file's addon sees them. `env[NAME]` accepts a `Final` string
  constant (a plain variable stays `BaseModel`).

- **`get_base_class_hook`**: private class attributes of model classes (`_inherit`,
  `_order`, `_rec_name`, `_sequence_index`...) get the type a model base class declares
  with an annotation (`BaseModel._inherit: str | list[str] | ...`), `Any` otherwise. So
  `_inherit = "x"` does not clash with `_inherit = ["x", "y"]` in a base class, nor
  `_sequence_index = "journal_id"` with `sequence.mixin`'s `False`, nor a property with
  a mixin's string. `_name` is a `str`; private fields (`_x = fields.Char()`) keep their
  type.

- **`Environment.__getitem__`**: `env["x"]` is the last class of `x` when its MRO is the
  whole registry class as the addon sees it. Otherwise (unrelated addons extend `x`, or
  its mixins are extended outside that class's view) it is a merged class named
  `<subclass of "x" seen from "my_addon">` (shown as `"x"` in messages), stored in the
  checked module like mypy's own ad hoc intersections, so the incremental cache keeps
  it. A module reuses the merged class of a module it imports from, so records passed
  between them (`RECS = env["x"]` in one, `from .a import RECS` in the other) keep one
  type.

- **`Many2one` / `One2many` / `Many2many(...)`** declare the field with the class that
  defines the comodel (the same class in every addon, so redeclaring a field elsewhere
  is compatible). **`__get__`** and an attribute hook resolve the comodel again when the
  field is read, from the reading addon (or the record's own class, if the reading
  module does not load all the comodel's classes), so the result has every extension
  the reader sees. `Model.field` (read on the class) is the field itself.

- **`__contains__`** of records: `"f" in records` asks whether a field is there. mypy
  narrows `hasattr(records, "f")` by giving the type an extra attribute and has no hook
  for narrowing, so the method hook of `__contains__` does what mypy's `hasattr` does
  (`TypeChecker.hasattr_type_maps`) and puts the narrowed type of `records` into
  mypy's binder, and into the type recorded for the operand, which mypy narrows the
  right side of `in` from. The class is not given `__getattr__`: mypy would then take
  records for any protocol. The model asked is remembered for the function being
  checked: `env["model"]` and the records kept on a test case take the attribute after
  the question.

- **`__getitem__` and the other reads of `dict` and `list`** (also `defaultdict` and
  `OrderedDict`; mypy names a method's hook after the receiver's class). mypy calls no
  hook for a dict or list display (`visit_dict_expr` checks a call of `<dict>` without a
  name), so the join it makes of mixed values cannot be changed where it is made. The
  hooks of the reads replace the joined type argument by `Any` in the result
  (`dict_items[str, object]` gives `dict_items[str, Any]`); `Var.is_inferred` of the
  variable the container is read from tells an inferred type from a declared one, and
  the function's bindings (`LocalValues`) tell a local dict display, whose value types
  mypy has recorded. For such a local dict the signature hooks of `__setitem__`,
  `update` and `setdefault` take any value; so do they for a dict the function builds
  by string-keyed displays and changes under string keys (`LocalValues.changes`), and
  they widen `int` values to `float` in a container without an annotation. A union of
  unrelated values counts as a join, and a container of `Never` (`{}`) or of a literal
  takes any key through the signature hooks of the reads. The function hooks of `zip`,
  `enumerate` and the like (also the iterators of `itertools`, `MIXED_CALLS`) map the
  join of a display argument to `Any`; the signature hooks of an item's `__setitem__`
  take any value where the function writes the items of a local `defaultdict` under
  two or more string keys (`LocalValues.item_keys`: `x[k]["a"] = v`, also `+=`); and
  the signature hook of `filter` takes `False` in the `None` overload.

- **Rebound variables** (`odoo_typing.rebound`). mypy types a local variable from its
  first value, after the hooks of that value have run, and no hook runs at a plain
  name or a literal (`last = False`, then `last = line`). The first lookup of a hook
  while mypy checks a module of an addon (`checker_state.type_checker`; for a model,
  its first field) declares the local variables without an annotation that a function
  binds more than once `Any`, and makes their first binding a plain assignment
  (`NameExpr.is_inferred_def`): `ConditionalTypeBinder.assign_type` then narrows an
  inferred `Any` variable to the type of each value, and joins differing values to
  `Any`. A variable that may be given `False` is declared `Any | Literal[False]`: the
  declared type is the context of each value, in which mypy types `False` as a literal
  it keeps (it widens `False` to `bool` otherwise), and branches meet as a union. A
  variable bound to a display of dicts, or to a value with an empty display inside, is
  declared `Any` too, so the display is typed in that context (`list[Any]`). Only
  functions mypy has not reached yet change; in the function being checked, only the
  variables whose first binding it has not checked (`Var.type` is still `None`).

- **Fields a search sets** (`odoo_typing.searched`). The `__get__` signature hook of a
  field read from a local bound once to `search(...)` / `search_fetch(...)` with a
  literal domain (`x |= v` counts as a binding; or the variable of a `for` over such a
  local), and the `read_result` of `search_read`, read the domain's top-level
  conjunction (a prefix parse: `&` keeps both sides, `|` and `!` keep nothing) for
  terms that set a field of the model. For a `for` variable bound more than once, the
  loop is the innermost `for` binding it whose body holds the read (the statements
  around the read are found by their source spans), when no other binding of it in
  that body is before the read or inside a loop of the body around the read.

- **Computed text fields** (`odoo_typing.computed`, `scanner.assigned`). The scan of a
  class keeps, per method and attribute name, the kinds of the values assigned
  (`x.name = v`, `setattr(x, "name", v)`; `x["name"] = v` and `x.update({"name": v})` /
  `x.write({...})` with a dict display where `x` starts at the method's first parameter
  or at a name bound to an expression that does, since most such writes are a dict's):
  a string by its syntax, a true operand of `or` (any type but `None` / `False`), the
  dotted name of a called name of the module (`json.dumps`), or unknown (then only the
  attribute's name, per class). A call of a name the method binds itself
  (`url.geturl()`, `self._label()`) is unknown. An unknown value written to records the
  method's records reach through attributes (`menu.page_id.write(...)`) is kept with the
  attributes instead; the fields of the model's view tell whether they lead to the
  model's records (a field that is not relational, a comodel not known, the model or a
  comodel that delegates to it). The scan also follows, statement by statement, which
  attributes a method assigns to each of its records on every path
  (`scanner.assigned.filled_attributes`, only for methods that assign a known value or
  call `super()` of themselves): `self.x = v`, and `for r in self:` whose every path
  through the body assigns `r.x` (both arms of an `if`; `continue` ends a record's path;
  `break` and `return` in the loop leave records without a value; inner loops, `with`,
  `try` bodies and `match` add nothing), plus a mark for `super().<method>()` on every
  path. `value_required` asks for a `Char` / `Text` / `Html` field that is computed at
  each read (not stored) whether the classes of the model's view assign only such
  values (a true operand of `or` only in a `Char` / `Text`), at least once in the compute
  method; for a field with an `inverse` or `readonly=False`, where `Field.__get__` fills
  in `False` instead of raising, also whether the last override of the compute method
  fills the field on every path (or calls `super()` on every path and the method it
  overrides does). The called names are looked up in mypy's symbol table of the module
  that calls them (then in `builtins`) and must be declared to return `str` (a class
  derived from `str` counts). Reads inside the compute method and the inverse method keep
  the declared type. The assigned kinds, the writes through attributes, the filled
  attributes and the method names are part of the addon's scan digest and of
  `__odoo_fields`, so an edit of a compute method checks the readers again.

- **Attributes of field objects** (`odoo_typing.field_setup`, `hooks.field_objects`). The
  scan keeps, for each field definition, the names of the arguments it passes, with the
  type of a true `str` / `int` / `float` literal (`FieldInfo.given`: `size=int`, the bare
  name for another value, `*` for `**kwargs`, positional arguments the class does not
  name, a class with an `__init__` of its own and a factory); the definitions of a field
  merge by name, and unnamed arguments hide the earlier ones. The attribute hook of the
  field classes (asked last, after the hooks of records) looks at an attribute declared
  `None` or `... | None` whose receiver is `records._fields["name"]` or a local bound to
  it once: where the field passes the attribute's name as such a literal, the attribute
  has the literal's type. `Field.__init__` keeps its arguments and the setup gives the
  object one attribute for each; the arguments the setup rewrites are left as declared:
  the keys that `_get_attrs` and `_setup_attrs` / `_setup_attrs__` of the field class and
  of its bases assign, delete or pop and the attributes they assign on `self`, read from
  the source files of these classes, once per file.

- **Guards of Odoo functions** (`odoo_typing.falsy_guards`, `hooks.falsy_parameters`).
  mypy drops the bodies of the modules whose errors it does not show, so the signature
  and return-type hooks of a function or method that Odoo defines outside the addons
  read the statements it starts with from its source file (indexed once per file by its
  `class` / `def` lines, following `from_string = to_date`; only the function's own text
  is parsed): `if not p: return ...` gives `p` `Literal[False] | None`, `if type(p) is
  bool: return p` gives it `bool`, and a `return None` that only `if not p:` makes is
  left out of the result for a true argument. The whole body is walked once more per
  parameter (`guarded_parameters`): a parameter every read of which is a truth test, or
  runs where a test of it passed (tracked through `if` / `while` / `assert`, `and` /
  `or`, conditional expressions and early exits), takes `Literal[False] | None` too.

- **Reads proven true** (`odoo_typing.proven`). The hooks of `get` of the class
  `Environment.context` is declared with (named after the receiver's class,
  `odoo.tools.misc.frozendict.get`) and of `env.ref` ask what the innermost function
  being checked proves about the call: the facts along the path from its body to the
  call (the tests of the arms it is in, `if not x: return` / `raise` before it,
  `assert`, the left operands of `and` / `or`), through `not`, `and` / `or`, `is
  (not) None`, `len(x) < n` and a local name's last value; a fact is about an
  expression of the same structure, and holds while no name it reads is bound again.

- **Aliases of conditions and `assertTrue`** (`odoo_typing.conditions`). mypy narrows
  the names a test reads, not the names its value was built from. With the rebound
  variables, each function's tests are rewritten before mypy checks it: an `if` /
  `elif` / `while` / `assert` operand that is a local whose last binding before the test
  is a value whose truth implies the truth of names or member expressions (`x and ...`,
  `... if x else False`, `(x and ...) or []`, `bool(x)`) becomes `alias and x` (`OpExpr`
  with fresh, bound `NameExpr` / `MemberExpr` nodes), so the binder narrows `x`; only
  where no binding of the alias or of those names may run between the alias and the
  test. The statements are numbered in source order, each with the blocks it is in; a
  binding counts when it is between the two in that order, in a loop around the test
  (unless the loop holds the alias too and the alias is an earlier statement of a
  block the test is in), in a loop around the alias only, or, for an earlier binding of
  the alias, when the alias is no such statement. A `self.assertTrue(value)`
  statement becomes a block of itself and `assert value`, in its place in the list (the
  function may be the one mypy is walking).

- **Tests about installed modules** (`odoo_typing.module_tests`): where the env hooks
  type records, the function mypy checks (`checker.scope.top_level_function()`) is
  read once for its `if` tests that hold `"ir.module.module"` or `"installed"` and
  the modules they name (also through a name the function binds once, or an
  attribute the methods of its class bind once), and for its statements
  `self.ensure_installed("x")`; a line those tests decide gives records of a model that a
  named addon outside the dependencies extends as `Any`, and the models, XML ids and
  fields of the named addon's closure are not reported (`Models.running_with`). mypy's
  own tree of the function is used, so no file is parsed again; the class's methods are
  read only for a test that reads an attribute of `self`.

- **Auto-installed addons** (`AddonIndex.auto_installed`): the manifests' `auto_install`
  (`True`: the addon's `depends`; a list: those addons) gives the addons Odoo installs
  with a closure, to a fixed point, with their own dependencies. What they define is
  no `odoo-model-depends` / `odoo-xmlid-depends` / `odoo-vals-key` error in the
  closure's addons; their classes are not added to the addon's view.

- **Records as the addon sees them** for what `self` gives: the method hooks of the
  `-> Self` methods, `__iter__`, `__getitem__` and the set operators of a model class
  map a class of the model that the reading addon's view extends to that view
  (`Models.view_type`); a type variable (`Self` of an override) is left alone.

- **Annotations narrower than the calls of their own class**
  (`odoo_typing.hooks.passed`). The scan of a model class records where a call of one of
  its own annotated methods passes a `list`, `set` or `tuple` display (or
  comprehension, also through a name the method binds) and the annotation names another
  container at that place, and which parameters a method passes on as it got them
  (`Contribution.passed`). `Registry.passed` joins what the classes of a class's own
  registry class say, to a fixed point over the parameters passed on; it does not depend
  on the reading addon, so the result can be kept with the class's module. mypy calls no
  hook for `super().m(...)`, so the annotation itself gets the alternative, in the hook
  that runs after the class body: mypy analyzes the signatures of methods after the
  class bodies of the module, and the hook rewrites the annotation that is not analyzed
  yet (`dict[int, tuple[set[str], X]]` becomes a union with `dict[int, tuple[list[str],
  X]]`; a name counts if it resolves to the builtin where it is written). Only in a
  module whose errors mypy does not show.

- **Operators of `False`** (`odoo_typing.hooks.comparisons.false_operand`). mypy names
  the hook of a literal's method after the class that defines the method, so `False - x`
  asks for `builtins.int.__sub__`. When the receiver is the literal `False` and the
  operand fits none of the method's signatures, the hook gives `Any`: mypy has reported
  the operand, and would otherwise go on with the return type of the method that failed.
  `None` has no operator methods, so for `None + delta` mypy checks the right operand's
  reversed method (`timedelta.__radd__`); the hooks of the reversed methods
  (`REVERSED_OPERATORS`, `empty_operand`) give `Any` the same way when the operand is
  `None` or `False`.

- **Dependency edges weigh as imports inside functions** (`DEPENDENCY_PRIORITY`,
  mypy's `PRI_LOW`): within an import cycle mypy orders modules by their imports'
  weight, dropping the lightest edges first. A plugin edge from a helper module to the
  models it reads (`account/tools/after_commit.py` reads `env["account.move"]`) closes a
  cycle; weighing as a top-level import it could put a model before the package it
  imports a name from, and mypy then binds a submodule of that name instead.

- **An exception in the plugin.** mypy calls `get_additional_deps` and
  `report_config_data` outside the context in which it reports the crash of a hook: an
  exception there ends mypy with an INTERNAL ERROR that points at mypy's issue tracker,
  and without `--show-traceback` no message at all. So these two, and the check of the
  addons for changes that comes first (it reads addons from disk), print one line and
  raise the exception again; mypy's exit status (2) and traceback stay the same:

  ```text
  odoo-typing: internal error while finding the models odoo.addons.my_addon.models.x relies on: KeyError: 'x' (please report it to odoo-typing)
  ```

  The other two texts are `computing the cache data of <module>` and
  `checking the addons for changes`. The hooks mypy asks for per name or expression
  are not wrapped: mypy reports their crashes with the file and line it was checking.

## Annotations: `odoo.values` and `odoo.model`

How the [annotated helpers](checks.md#annotated-helpers-odoovalues-and-odoomodel) get
their types:

- **Which files.** The scan of each file records the `odoo.values.*` /
  `odoo.model.*` names in it (a text search over the bytes it reads anyway). Their
  models join the file's dependency edges, like the models of `env["..."]`.
- **Bare strings.** mypy cannot look `"odoo.values.res_partner"` up: it would need a
  name `odoo` in the module and a module `odoo.values`. So the plugin rewrites those
  annotations to `__build_class__["values", "res_partner"]` (a name of `builtins`,
  visible in every module and never an annotation of its own, whose type-analysis hook
  the plugin owns). It does so when mypy's semantic analysis reaches the module's
  first regular top-level class (the customize-MRO hook, called before the class's
  body; mypy does not call it for a class-syntax `TypedDict` / `NamedTuple` or a class
  whose bases it rejects, and the plugin skips classes defined in functions). mypy
  analyzes a module from source on several paths that ask the plugin nothing else
  first: a cached module whose dependency changed, the other modules of an import
  cycle, `--num-workers`, a daemon update. An annotation analyzed before the first class
  waits for mypy's next pass, as a name mypy cannot find yet does. A module without
  such a class is never rewritten (`Annotated[...]` works there).
  `Annotated[dict, "..."]` needs no rewriting: the plugin's hook for `typing.Annotated`
  reads its metadata, and hands every other `Annotated` back to mypy unchanged.
- **`odoo.values.<model>`** is a named `TypedDict` (`total=False`) created in the
  module that uses it, the way merged view classes are (so the incremental cache keeps
  it). Its bases are `typing._TypedDict` and `dict[str, Any]`, so the values go where
  Odoo 19+ annotates a `dict`. Its name carries a digest of its keys: the daemon merges
  a module analyzed again into its earlier symbols by name, and would otherwise keep the
  earlier keys.
- **The keys** are the fields any addon gives the model, its `_inherit` parents and
  `base` (recursively), and the models it delegates to. A text index lists the files
  whose `_name` / `_inherit` name each model (string literals; a file that assigns
  anything else is read by the scanner, which evaluates its constants); only those
  files are read, each on its own, and kept for their stamps. The index is kept too, with the stamps of every
  source file and directory of the addons. The first time, building it reads every file
  (about 0.4 s on Odoo's addons) and the files of `res.partner` and its parents take
  0.6 s more; later runs stat the files and directories (a new, removed or renamed file
  changes its directory's stamp) and read only what changed. The same keys in every
  module and addon make the `TypedDict`s of one model compatible, which `super()`
  needs.
- **`odoo.model.<model>`** is made during semantic analysis by the code that types
  `env["..."]`: the model's last class, or the merged class of the module. When a class
  it needs is not analyzed yet (an import cycle), mypy is asked to analyze the
  annotation again on its next pass.
- **Incremental mode.** The config data of a file that uses `odoo.values.*` carries a
  digest of the keys of every model its aliases may name (kept with the stamps of the
  files they come from): a field added in any addon makes mypy check every such file
  again, so a cached file and a checked one never see different keys. Checking the
  index costs a run where nothing changed about 0.08 s on Odoo's addons (stats of
  6,000 files), only when the checked files use `odoo.values.*`. A file whose
  `odoo.model.*` names no model of its closure, like one that names models outside it,
  also depends on the list of addons.
- **Daemon.** An annotation is analyzed again when a class of its model in the view,
  a class its keys come from (in any addon the daemon checks) or the namespace of their
  modules changes. The registry starts over when a file of the index changes, so the
  keys follow the disk. A key that an addon the daemon does not check adds shows only
  in the annotations the daemon analyzes after that (the others keep the earlier keys,
  so `super()` between them can be an error) until `dmypy restart`.

## Domains: `odoo.domain` and the checked expressions

How [annotated domains](checks.md#annotated-domains-odoodomain) are typed and checked:

- **The type.** `odoo.domain.<model>` is the types a domain has (`list[Any]`, and
  `Domain` on Odoo 19+, like Odoo's `DomainType` but with `list[Any]`, so that a
  variable holding a list passes) in a union with a marker: a class without members,
  `<domain of res_partner>`, created in the module that uses the annotation (as the
  `TypedDict` of `odoo.values`), a subclass of `list[Any]` (and of `Domain`) whose
  metadata names the model. Literals and `Domain(...)` fit the union; the marker stays
  in the declared types (a function's return type, a variable's, a parameter's) and in
  the cache. mypy simplifies the type of a call, which drops the marker, so the model
  is always read from the declaration: the callee's declared return type, the variable
  of a name, the type of a `cast`. Its name has no digest: nothing in it can change.
- **Where literals are checked.** mypy has no hook on a `return` or an assignment. The
  pass that rewrites bare strings (see above) also wraps the expressions that are
  domains: `return E` of a function annotated with a domain, the value of an annotated
  assignment, and `return E` of a field's `search=` method (of a concrete model, in the
  class's addon view) become `__build_class__(E, "<kind>", "<alias>")`. Python itself
  calls `builtins.__build_class__` for every class statement; no code calls it, so the
  plugin owns its hooks: a signature hook makes the call `def [T] (T, str, str) -> T`
  (mypy types the statement as before; the expression keeps its lines and columns), a
  function hook checks `E`. The wrapped places only have types whose members are
  `list[Any]`, `Domain`, the marker, `None` or `Any`, for which the identity is
  transparent (it passes no type context). The name is bound to the builtin by hand:
  the statements of a module above its first class are analyzed before the rewrite.
  Annotated class attributes are wrapped only in Odoo model classes (a dataclass's
  plugin reads the value of a field). Whether an annotated variable is a whole domain
  where it is assigned (no later `+=`, `append`, `extend`, `insert` in its scope, nor a
  rebinding that uses it as a piece of a list: `x = x + [...]`, `x = [*x, ...]`; one that
  uses it as a whole domain, `x = AND([x, ...])`, keeps it whole) is decided by the same
  pass, by name: some statements of the module are analyzed already, others
  not, so the function walk of the checks (which keys variables by their symbol) could
  not tell one variable apart there.
- **The parts of a domain.** A consumer (a search method, the wrapper, an annotated
  parameter, an `any` term, `domain=`) walks the expression into literal lists and
  typed parts (`odoo_typing.domain_parts`): the literals are checked term by term, a
  typed part only for its model. A method's hook is named after the receiver's class
  (`addon.SaleOrder._helper`), so the declaration of an annotated parameter is found
  in that class's MRO, once per name.
- **Odoo 19+.** The parameters Odoo annotates `DomainType` get a signature with
  `list[Any]` for the list member in addon files, found the same way.

## Incremental mode

mypy's cache only knows a module's own source and imports, but the plugin's result for a
file also depends on its addon's `depends` closure. So the plugin gives mypy, for every
module in an addon (`report_config_data`), a digest of that closure: each addon's name,
path, manifest `depends` and what its scan found (model classes, `_name` / `_inherit`,
registration order, comodels, model names used in `env[...]`), plus the plugin's version.
Odoo's own modules (`odoo`, `odoo.*`, not in an addon) get `odoo_version=<N>`, whichever
layout mypy finds them through (the overlay root, a symlink, the package directory):
the profile of `odoo_version` types some of their names (`odoo.http.request`, and on
Odoo 19+ the relational field classes), so correcting the version checks them again.
Other modules outside addons get no data.

When a dependency gains an `_inherit` extension, a manifest changes `depends`, an
extension module is deleted or moved, or `addons_path` changes, mypy checks the affected
modules again instead of replaying stale errors. Editing a method body changes no
digest: mypy's own dependency tracking handles it.

mypy also keeps a snapshot of its plugins with its cache: the plugin module's hash and
its `__version__`, which is a digest of the plugin's whole source. A new plugin version
(an upgrade, or any edit of its source) therefore makes the next run a full cold run,
the standard library and Odoo's own modules included (`plugins differ` in `mypy -v`).

The digests are kept in `<cache_dir>/odoo_typing/<cache_tag>/`
(`.mypy_cache/odoo_typing/cpython-312/`: one directory per interpreter, whose grammar
the scans use), together with `stat` stamps (mtime and size) of the manifests, addons
directories and scanned files they come from. Records of earlier versions, directly in
`<cache_dir>/odoo_typing/`, are never read again and can be deleted.

- Each stamp is taken right before its file is read or its directory listed, never
  after: a file saved during a run then has a newer stamp than the recorded one, and is
  read again on the next run.
- Each addon's scan includes its manifest, whose `data` lists name the files of its XML
  ids: any manifest edit, even of the version, scans that addon again.
- A file, manifest or directory that cannot be read gets a stamp that no file has. What
  was computed without it is kept while it still cannot be read; once it can be (a
  `chmod` or `chown` changes neither its mtime nor its size), the next run reads it.
  This holds for the scans, the data files, the class-file index and the addon index.

A run in which nothing changed reads these small files and calls `stat` on the files
they list, but parses nothing.

**Limits:**

- A structural change (a new class, `_inherit`, comodel or `env["..."]` literal) in an
  addon makes mypy recheck every module of the addons that depend on it, not only the
  ones that use the changed model.
- Whether an unknown name is reported as `odoo-model` or as `odoo-model-depends`
  depends on the addons *outside* the closure. The cache entry of a file of an
  `addons_path` addon that names such models depends on the list of addons and on the
  closure (not for Odoo's own addons, whose files often name optional models): an addon
  added or removed anywhere, or a `depends` edit, checks the file again. It can still
  lag when an existing outside addon starts or stops defining the model without a
  directory change (only its file was edited), until the file is checked again.
- Switching `overlay_root` on or off changes every addon's module names and invalidates
  the whole cache.

## Shared scan cache

The plugin also keeps each addon's scan (classes, fields, XML ids), and what the files
no scan reaches (tests) refer to, valid while the files they read are unchanged: after
an edit, only the edited addon is parsed again, not its whole dependency closure.

A scan depends only on its addon (its files, module prefix and place in the load order),
so it is kept in `scan_cache_dir`, shared by every project and mypy cache on the
machine:

- `$XDG_CACHE_HOME/odoo-typing/scans` (`~/.cache/odoo-typing/scans`) by default;
- a path relative to the config file;
- or `""` to keep the scans in mypy's cache only.

The scans sit in `<scan_cache_dir>/<cache_tag>/` (`~/.cache/odoo-typing/scans/cpython-312/`),
one directory per interpreter: a scan parses with the running Python's grammar, and a
file that one Python version cannot parse may be fine for another. So each interpreter
scans Odoo's addons once. Scans that earlier versions wrote directly in
`scan_cache_dir` are never read again and can be deleted.

A new mypy cache, another worktree or another project on the same Odoo then skips the
scans of Odoo's addons (on the `small` benchmark profile, a cold run without the scans
takes 20–26 % less time). A directory that cannot be written falls back to mypy's cache.

## The daemon (dmypy)

The daemon keeps one plugin instance. Before the files of each update are processed,
the plugin compares the stamps of everything its registry has read (addons roots,
manifests, scanned and data files, and mypy's config file) with the disk, and starts
over with a fresh registry, and the settings read again, if any changed (an invalid
config is reported and the previous settings kept).

The daemon only rechecks the code that changed (and its dependents), so the new
registry or settings show in what is checked again. After editing only a manifest or
the `[tool.odoo_typing]` / `[odoo_typing]` settings, run `dmypy restart` to see their
effect everywhere.

A file that cannot be read does not make every update start over: the registry starts
over on the first update after it can be read (a `chmod` or `chown`).

The hooks that depend on a declared signature (the method, function and signature hooks
for `odoo.domain.*` and `DomainType` parameters) are chosen again each time mypy loads a
set of modules, that is on each update: the declaration may be in a file the registry
does not read (an addon's `tests/`, a helper package), which changes without a new
registry. The class and attribute hooks depend only on the registry and the profile,
and are kept.

**Deleting or renaming a module that defines model classes.** mypy records the
dependencies the plugin adds to a module (the modules that define the models it uses)
when it parses that module, and computes them again only when it parses that module
again. A plugin can neither ask mypy to compute another module's dependencies again nor
learn that a file was deleted. So after such a module is deleted, dmypy may report a
spurious error, with no line number, in each file that used its models, until that file
is edited or the daemon restarts:

```text
error: Cannot find implementation or library stub for module named "odoo.addons.my_base.models.extra"  [import-not-found]
```

When the deletion is the only change of an update, dmypy does not refresh the registry
at all: the deleted classes (and, for example, the fields of a deleted extension) still
count until the next update that changes a file. After deleting or renaming a module
that defines model classes, run `dmypy restart`, or edit the files that use those
models. A normal mypy run, with or without a cache, is not affected.

**Checks that read fields of other modules.** A check of a domain, of `vals` or of a
field path reads the fields of models that other modules define. The daemon reruns a
target (a function, or a module's top level) only for its dependencies, so each check
makes the target depend on the classes of the view of every model it read (the model,
the models it delegates to, the comodels its `related=` paths go through): `<C>` fires
when a class goes away, `<C[wildcard]>` when one of its members changes. A field's type
does not change with its arguments (`store=`, `search=`, `related=`...), so every module
with model classes also gets a hidden attribute `__odoo_fields` whose type is a
`Literal` digest of its classes' field definitions (and of what the registry reads of
them: `_name`, `_inherit`, `_inherits`...): changing an argument fires
`<module.__odoo_fields>`. (A module attribute: mypy would make every class that inherits
a class member depend on it.) These triggers lead to one trigger per checked module and
model view, on which the module's targets depend; mypy follows a trigger to the triggers
that depend on it, so a target adds one dependency per model, not one per class. The
dependencies and the digest exist only in fine-grained mode (the daemon,
`--cache-fine-grained`; a full run checks again what such an edit changes through the
addons' fingerprints), and no dependencies are written for modules whose errors mypy does
not show (`follow_imports = "silent"`, such as Odoo's own addons). On `sale` (Odoo 18) the fine-grained cache grows by 2 % and the
daemon's peak memory by 2 %.

Their limits: mypy reads these dependencies when it processes a whole module (the first
build, the fine-grained cache, a file parsed again), not when it rechecks one target
for a trigger. A model that a function starts to read without an edit of its file (a
helper in another file got another annotation), and a new class of a model in a
module the view did not have, show after an edit of the function's file or
`dmypy restart`. An error that a change elsewhere removes always goes away: mypy
rechecks the targets with errors on each update. A fine-grained cache written with
`--num-workers` carries no dependencies at all (mypy's own neither). Whether a method is
a field's `search=` method is decided when its module is analyzed: a `search=` added in
another file wraps the method after an edit of its module or `dmypy restart`.

An error about a selection key is reported from the hook of the comparison's method
(`str.__eq__`, `tuple.__contains__`...). mypy checks an operator on a union operand
(`str | Literal[False]`) item by item and reads any error reported meanwhile as "this
item does not support the operator", so the plugin sets mypy's error watchers aside for
that one report (a private attribute of mypy; without it the error is reported the usual
way).

The class attributes that name fields (`_order`, `_rec_name`...) are checked during
semantic analysis (mypy has no hook on a class attribute when it checks types), so they
write no dependencies: a field that another module drops is reported there after an edit
of the class's module or `dmypy restart`.

The variables of a function (`domain = [...]; domain.append(...)`) are kept for the
function being checked; the daemon merges a changed function into the old function
object with a new body, so they are kept for the body too.
