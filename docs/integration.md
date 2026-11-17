# Editors, pre-commit and CI

[← README](../README.md)

## Editors (Pyright, PyCharm, VS Code)

The plugin runs only inside mypy. For an editor, `odoo-typing-stubs` writes `.pyi` stubs
of the models as one addon sees them (the "top" addon of your project):

```bash
odoo-typing-stubs --addon my_addon --output typings          # again after adding fields
odoo-typing-stubs --addon my_addon --output typings --check  # in CI: exit 1 if stale
```

What it writes:

- `typings/odoo_typing_models.pyi`: a class per model (bases: the model's classes, as
  for the plugin) that declares each field with the type reading it gives (a relational
  field: its comodel's class), and a `<model>__vals` `TypedDict`;
- `typings/odoo/api.pyi` (Odoo 17-18) or `typings/odoo/orm/environments.pyi` (19+): the
  real module's stub (mypy's `stubgen`) with a `Literal["<model>"]` overload of
  `Environment.__getitem__` per model, so `self.env["res.partner"].` completes partner
  fields.

Point the editor at the directory:

- Pyright: `"stubPath": "typings"` in `pyrightconfig.json`;
- VS Code: `python.analysis.stubPath`;
- PyCharm: mark the directory as a stub root.

> [!WARNING]
> Do not put `typings` on mypy's `mypy_path`: it would hide the real modules from the
> plugin.

The stubs are checked with mypy without the plugin (as an editor sees them); Pyright and
PyCharm themselves are not part of the test suite.

Live checking of unsaved buffers (mypy's `--shadow-file`, which some editor plugins
pass) checks the buffer, but the plugin reads the addons from disk: models and fields
typed and not saved yet are unknown until the file is saved.

An editor plugin that runs mypy itself (on save, or through `dmypy`) should run
`odoo-typing-check --daemon <file>` instead of `mypy <file>`: mypy names a file given by
its real path `my_addon.models.sale`, not `odoo.addons.my_addon.models.sale`, and prints
the paths of the overlay root. `odoo-typing-check` checks the file's addon by its
module name and prints the errors of the file at its real path
([Running mypy](configuration.md#running-mypy)).

## pre-commit

The repository provides two hooks: `odoo-typing-check` (the changed files' addons are
checked and the errors of the changed files are printed, at their real paths) and
`odoo-typing` (plain mypy with the plugin, with the `args` you give it). See
[ci/pre-commit-config.yaml](ci/pre-commit-config.yaml) for an example.

## CI

`odoo-typing-check` (every addon of `addons_path`) is the one command a CI job needs: it
generates the overlay root, and its paths are those of the repository, so annotations
land on the changed lines.

[ci/Dockerfile](ci/Dockerfile) builds an image with Odoo's sources and the plugin.
[.github/workflows/ci.yml](../.github/workflows/ci.yml) is how this project tests itself
on Odoo 17, 18, 19 and 20 with the oldest and newest supported mypy.

## Gradual adoption: a baseline

mypy has no baseline mode, so `odoo-typing-baseline` wraps it:

```bash
odoo-typing-baseline record -- -p odoo.addons.my_addon   # save today's errors
odoo-typing-baseline check -- -p odoo.addons.my_addon    # only new errors, exit 1 if any
```

- `record` saves the errors in `odoo-typing-baseline.json` (`--baseline FILE` to change
  it); run it again to drop fixed errors.
- `check` prints only the errors that are not in the baseline.
- Errors are keyed `path:code:message` without the line number, which shifts with every
  edit above it, and counted per key.
