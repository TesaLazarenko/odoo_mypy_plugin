# What the plugin leaves as an error, and why

Errors that mypy with the plugin reports on Odoo's own code and that no rule removes,
with the reason: what Odoo does, and why a rule would be wrong or cannot be derived from
Odoo's code. The examples are of Odoo 18, the 36 addons of the benchmark's `big` profile.
An entry whose reason stops holding is work to do, not a limit.

The file is filled as the errors are gone through. An error that is in none of its
entries is not a limit: it is not looked at yet (see "Not gone through yet" at the end).

## Errors that are right

### A field that Odoo itself leaves empty is read as if it had a value

`leave.date_from.date()` (`hr_holidays/models/hr_employee_base.py:221`),
`", ".join(partners.mapped("name"))` (`survey/wizard/survey_invite.py:142`),
`self.env[message.model]` (`mail/models/mail_message.py:956`), `line.name[:64]`
(`purchase_stock/models/account_invoice.py:71`): the largest group of the errors that
only the plugin reports. This entry holds for the reads that no guard written by Odoo
covers, as on the lines named here; the reads of the group that sit under such a guard
are not gone through yet (listed at the end) and may need a rule.

- The field is not `required`, no `_parent_store` or log-access rule fills it, and its
  compute method does not give every record a value. `hr.leave.date_from` is assigned
  `False` by its own compute method (`hr_holidays/models/hr_leave.py:292`);
  `res.partner.name` is required only by a Python constraint for some partner types.
- At run time the read gives `False`, and `False.date()`, `", ".join([False])`,
  `env[False]` raise. Whether a record with the empty value reaches the line depends on
  the data, which no static rule sees.
- A rule that read such fields without `False` would hide the same error in the code of
  an addon author, which is what the plugin is for. The rules that do remove `False`
  each rest on something Odoo guarantees: `required`, a domain or `filtered("field")`
  that selected the records, a compute method that assigns a string on every path.

### An empty `Reference` is read as a record

`self.resource_ref.message_subscribe(...)` (`portal/wizard/portal_share.py:116`),
`project.collaborator_ids` (`project/wizard/project_share_wizard.py:82`), 19 errors
`Item "None" of "Any | None"`; `menu.action.id` of `ir.ui.menu`
(`mrp/tests/test_order.py:5592`), one more.

A `Reference` that holds nothing reads `None`, and `None.message_subscribe` raises. It
holds nothing also when it was given a value: `Reference.convert_to_cache` keeps
`"model,id"` only if `env[model].browse(id).exists()` and the model is one of the
selection, else `None`. The wizards compute the field from `res_model` and `res_id`
(`'%s,%s' % (wizard.res_model, wizard.res_id or 0)`, `portal_share.py:38`): both are
required, yet the reference is empty when the record was deleted, when `res_id` is `0`,
and when the model is not installed (the method assigns `None` then, line 40). Whether a
wizard is opened for a record that exists is what the data says, not the code:
`ir.ui.menu.action` is empty for every menu that only groups others.

### An override is not callable like the method it overrides

17 errors `Signature of "..." incompatible with supertype`, on 15 lines (one line of
`website/models/ir_actions_server.py` overrides three classes). mypy alone does not
report them: the class of `sale` and the class of `product` are one model only for the
plugin, which gives them Odoo's registry order. Each is a call that the overridden
method takes and the override does not:

- an argument or its default dropped (9 errors): `def _filter_to_unlink(self)` of
  `sale` and `stock` (`sale/models/product_product.py:99`, `stock/models/product.py:673`)
  over `def _filter_to_unlink(self, check_access=True)` of `product`, so
  `products._filter_to_unlink(check_access=False)` raises `TypeError` once `sale` is
  installed; `view_header_get(self, view_id, view_type)` (`account`, `stock`, `product`)
  over `view_id=None, view_type='form'` of `base`; `purchase/models/product.py:131`;
  `_get_eval_context(self, action)` of `website` over `action=None`;
- `**kwargs` dropped (2): `_get_product_catalog_record_lines(self, product_ids,
  child_field=False)` (`account/models/account_move.py:2674`,
  `purchase/models/purchase_order.py:1040`) over `(..., child_field=False, **kwargs)` of
  the mixin, which passes its `**kwargs` on (`product/models/product_catalog_mixin.py:94`):
  an Odoo bug;
- named arguments taken only by keyword (4): `message_post_with_source(self, source_ref,
  **kwargs)` (`mass_mailing/models/mail_thread.py:39`, and `message_mail_with_source` at
  line 31) over `(self, source_ref, render_values=None, message_type='notification',
  ..., **kwargs)` of `mail`;
  `sale/models/sale_order.py:2176`, `website_sale/models/website_snippet_filter.py:19`.
  `thread.message_post_with_source(ref, values)` raises. Odoo's own calls pass them by
  keyword; a rule that accepted such an override would accept the positional call of an
  addon author as well;
- a parameter renamed (1): `_remove_provider(self, code, **kwargs)`
  (`account_payment/models/payment_provider.py:138`) over `(self, provider_code,
  **kwargs)`: `_remove_provider(provider_code="x")` raises;
- a positional-only marker dropped (1): `_to_store(self, store, **kwargs)`
  (`im_livechat/models/mail_message.py:24`) over `(self, store, /, *, fields=None,
  **kwargs)` of `rating`. The base takes `store=` as one of its `**kwargs`, the override
  binds it to its parameter: `_to_store(s, store=...)` raises "multiple values". No code
  writes that call, and the error is still what the two signatures say; to accept it the
  plugin would have to decide which calls nobody writes.

The fix is in the override, not in the checker.

### Odoo's own bugs

The plugin reports them and they stay: `(1, "=", 0)` as a domain leaf
(`hr/models/hr_department.py:50`), `state in 'validate'` and `not in ('cancel')`
(substring tests: `hr_holidays/models/hr_leave.py:1124`,
`stock/wizard/stock_picking_return.py:62`), selection values that the field does not
have (`'done'` of `sale.order.state`, `'asset_bank'` of `account.account.account_type`),
`configs = super().write(vals)` then `configs._check_...()` on the `True` that `write`
returns (`event/models/res_config_settings.py:75`), a field of another addon read
without depending on it (`account/models/account_move.py:6702`), `products = False` that
reaches `len(products)` when the context has neither product key
(`stock/models/product.py:1052-1057`).

## What cannot be derived from Odoo's code

### `account.root`: ids that are strings

`root.id[:-1]` (`account/models/account_root.py:38`). The model is a view over account
codes (`_auto = False`, `_table_query = '0'`) and its `browse` takes the codes as ids. It
declares no `id` field: `_fields["id"]` is the `fields.Id` every model has, and nothing
in the class says the ids are strings. A rule could only name this model, and a rule
that names a model is not derived from Odoo's code.

### `records.grouped(key).get(value)` may be `None`

`pickings_by_format.get(print_format).move_ids`
(`stock/models/stock_picking.py:2161`, `mrp/models/mrp_production.py:2879`): eight
errors.
`grouped` returns a `dict` and `dict.get` returns `None` for a missing key. The code is
right because the key comes from `mapped()` of the same field on the same records, two
statements earlier: proving it needs the values of two expressions to be related, which
a type cannot say. Odoo 19 and 20 declare `grouped` themselves and report the same.
`pickings_by_format[print_format]` is the form that type-checks.

### A value whose type depends on another value

The code is right because two values go together, which a type cannot say:

- `attachments` holds `None` for the images not uploaded yet, a loop over every index
  fills them, and the next loop reads them (`mass_mailing/models/mailing.py:1476`): that
  no `None` is left follows from the loop covering every index.
- `qtype = self._type_match.get(question.question_type, (False, False))`, then
  `qtype[1][0]` under `if question.question_type == "matrix":`
  (`survey/tests/common.py:144`): `qtype[1]` is a tuple only for that one key, and the
  default `(False, False)` is never used for it. (The other half of the type, the join
  of the dict's values, is work to do: below.)
- `action.type == "ir.actions.act_window" and action.res_model`
  (`mail/models/ir_ui_menu.py:59`): `menu.action` is a `Reference` to one of the action
  models, and only `ir.actions.act_window` has `res_model`. `type` is a `Char` field
  whose default each action model sets: a value of the data, not a type the union could
  be narrowed by. `isinstance(action, type(self.env["ir.actions.act_window"]))` or
  `action._name == "ir.actions.act_window"` would say it to a reader, and the second
  is to do (narrowing by `_name ==`, listed below).

### `None` for a parameter Odoo annotates `str`

`session.set_opening_control(0, None)` in the tests of `point_of_sale`, `pos_hr` and
`pos_online_payment` (12 calls); Odoo's own code passes `""`
(`pos_self_order/models/pos_config.py:343`):
Odoo declares `def set_opening_control(self, cashbox_value: int, notes: str)`
(`point_of_sale/models/pos_session.py:1779`). The plugin corrects what Odoo leaves
untyped or types wrongly at run time; an annotation Odoo wrote is what the method
promises, and the calls do not keep to it.

### `value or False` leaves `Literal[True]`

`move_directions = self.env.context.get('move_directions') or False`, then `if
move_directions: move_directions.get(...)` (`stock_account/models/stock_move.py:718-723`,
`purchase_stock/models/stock_move.py:226`). The plugin declares such a variable `Any |
Literal[False]` (`rebound_variables`), and with a typed left operand that is enough:
`partner = self.partner_id or False` is `Partner | Literal[False]`. Here the left operand
is `Any` (a context value), and mypy types the right operand of `or` in the context of
the left one alone when that one holds `Any` (`ExpressionChecker._combined_context`,
"Any should be contagious"): the declared type is dropped, the `False` becomes `bool`,
and `if move_directions:` leaves `Literal[True]`.

mypy calls no plugin hook for a boolean operator, and the type context is its own, so a
rule could only rewrite the source before mypy checks it (`False` into a cast to
`Literal[False]`). That changes the code mypy checks, not the types Odoo declares, and
where the left operand is typed the cast is redundant, which mypy reports under
`warn_redundant_casts`. `max(..., default=False)` is a call, and there the plugin keeps
the literal.

## Not gone through yet

Groups of errors that only the plugin reports and that have no entry above and no rule
yet. They are work to do:

- a field read under `isinstance(self.env[self._name],
  self.env.registry["mail.activity.mixin"])` (`mail/models/mail_thread.py:4783`);
- tuples and lists that grow by a value of another shape (`domain += (...)`:
  `mrp/models/stock_rule.py:148`, `stock/models/stock_move.py:1370`);
- the errors of the empty-field group that sit under a guard Odoo wrote and that a rule
  could follow: the group is not broken down line by line yet;
- a variable bound from a conditional expression, narrowed by its condition:
  `to_confirm = self.filtered(...) if confirming else None`, then `if confirming:
  to_confirm._update_mail_schedulers()` (`event/models/event_registration.py:236` on
  Odoo 18, `:290` on Odoo 19). The rule for a condition kept in a variable (`docs/checks.md`) covers the
  other direction (`value = x if cond else False`, then `if value:`); this one is the
  same proof read from the condition;
- a class attribute bound once from a dict display of values of different types
  (`_type_match` of `survey/tests/common.py`, read at lines 62, 67, 78, 80): the rule
  for such a display covers local variables only;
- narrowing by `records._name == "model"` (`mail/models/mail_render_mixin.py:357`,
  `website/models/theme_models.py:79`): mypy calls no hook where a branch begins, so a
  rule needs the source scan of the branches, as for the reads above;
- attributes a mixin's methods read that every model using the mixin declares
  (`self._track_duration_field` of `mail.tracking.duration.mixin`, 8 errors;
  `self.message_post_with_source` of `mail.activity.mixin`): mypy reports them without
  the plugin too. A rule needs the models that inherit the mixin on the whole addons
  path, and the mixin's module checked again when one of them changes;
- `self.f` read directly under a test kept in a variable (`field = self._fields.get("f")`,
  `if field:`, `self.f`): the records taken from `self` there have the field
  (`self.sudo().f`), `self` itself does not, because mypy calls no hook where the
  variable is tested. No such read in the 36 addons; to do when one is found;
- the views of records asked with `"f" in records` (not kept in a variable) bound to a
  variable inside the block and read after it: `first = self.browse()`, `if "f" in
  self._fields: first = self.sudo()`, then `first.f` is not reported. mypy keeps the
  type with the field when it joins the branches (the field does not change the class,
  and the union keeps the first item), as it does for its own `hasattr`. The views
  given the field by a test kept in a variable (`field = self._fields.get("f")`) are
  checked against this; those of the `in` question are not yet;
- reads of a field where a test says it is missing that are not reported. mypy gives
  the records the field in both branches of `if "f" in records:` (it narrows the operand
  of `in` to the recorded type either way and calls no hook where a branch begins), so
  the plugin finds the reads in the source, and reports only the plain ones. Left:
  - inside one expression (`"f" in records or records.f`, `records.f if "f" not in
    records else None`): the parts may share a line with the test; to do with columns;
  - through another name or a view (`r = records`, then `r.f`; `records.sudo().f`): the
    scan matches the records as written;
  - after an `elif` branch that returns, and after a branch whose last statement is an
    `if` that returns on every path: the scan follows one `if` and its literal last
    statement;
  - where the statements bind the name again (also by a walrus in a comprehension, by
    `nonlocal` or `global` in a nested function, by assigning to `self.partner` when
    the records are `self.partner`) or ask again (`hasattr`, `isinstance`, another
    `in`): nothing is reported there at all, since the name may hold other records.
