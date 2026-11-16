"""The mypy daemon keeps one plugin instance: its registry must follow file edits.

One daemon session, scripted: each step edits addon files, rechecks with
``dmypy run`` and compares with what a cold mypy run reports for the same files.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import PYPROJECT, write_addon
from mypy.api import run as mypy_run

THING = """
from odoo import fields, models

class Thing(models.Model):
    _name = "x.thing"

    name = fields.Char()
"""

EXTENSION = """

class ThingExtra(models.Model):
    _inherit = "x.thing"

    extra = fields.Integer()
"""

USER = """
from odoo import models

class User(models.Model):
    _name = "x.user"

    def action(self) -> None:
        reveal_type(self.env["x.thing"])
"""

GADGET = """

class Gadget(models.Model):
    _name = "x.gadget"

    def action(self) -> None:
        reveal_type(self.env["x.gadget"])
        reveal_type(self.env["x.thing"].extra)
"""


class Daemon:
    """A ``dmypy`` session on one config; ``check()`` returns the reported lines."""

    def __init__(self, tmp_path: Path, config: Path, targets: list[Path]) -> None:
        self._status = tmp_path / "dmypy.json"
        self.options = ["--config-file", str(config), "--cache-dir", str(tmp_path / "cache")]
        self.targets = list(map(str, targets))
        self.flags: list[str] = []

    @property
    def _run_args(self) -> list[str]:
        return ["run", "--timeout", "300", "--", *self.options, *self.flags, *self.targets]

    def _dmypy(self, *args: str) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, "-m", "mypy.dmypy", "--status-file", str(self._status)]
        return subprocess.run([*command, *args], capture_output=True, text=True, timeout=300)

    def check(self) -> list[str]:
        result = self._dmypy(*self._run_args)
        assert "INTERNAL ERROR" not in result.stdout + result.stderr, result.stdout
        assert result.returncode in (0, 1), result.stdout + result.stderr
        return [
            line.split("my_mod/", 1)[1] for line in result.stdout.splitlines() if "my_mod/" in line
        ]

    def stop(self) -> None:
        self._dmypy("stop")


@pytest.fixture
def daemon(tmp_path: Path, odoo_root: Path, odoo_version: int) -> Iterator[Daemon]:
    root = tmp_path / "addons"
    write_addon(root / "dep_mod", {"models.py": THING})
    write_addon(root / "my_mod", {"depends": ["dep_mod"], "user.py": USER})
    config = tmp_path / "pyproject.toml"
    config.write_text(
        PYPROJECT.format(
            odoo_root=odoo_root,
            odoo_version=odoo_version,
            mypy_path=json.dumps([str(odoo_root), str(root)]),
            addons_path=json.dumps([str(root)]),
        )
    )
    session = Daemon(tmp_path, config, [root / "dep_mod", root / "my_mod"])
    try:
        yield session
    finally:
        session.stop()


def _append(path: Path, source: str) -> None:
    with path.open("a") as handle:
        handle.write(textwrap.dedent(source))


def test_dmypy_new_model_extension_and_moved_module_follow_edits(
    daemon: Daemon, tmp_path: Path
) -> None:
    root = tmp_path / "addons"
    first = daemon.check()
    _append(root / "my_mod/user.py", GADGET)
    second = daemon.check()
    _append(root / "dep_mod/models.py", EXTENSION)
    third = daemon.check()
    (root / "dep_mod/models.py").write_text(textwrap.dedent(THING))
    (root / "dep_mod/extra.py").write_text("from odoo import fields, models\n" + EXTENSION)
    (root / "dep_mod/__init__.py").write_text("from . import models, extra\n")
    fourth = daemon.check()

    assert fourth == [line.replace("models.ThingExtra", "extra.ThingExtra") for line in third]
    assert (first, second, third) == (
        ['user.py:8: note: Revealed type is "dep_mod.models.Thing"'],
        [
            'user.py:8: note: Revealed type is "dep_mod.models.Thing"',
            'user.py:15: note: Revealed type is "my_mod.user.Gadget"',
            'user.py:16: error: "Thing" has no attribute "extra"  [attr-defined]',
            'user.py:16: note: Revealed type is "Any"',
        ],
        [
            'user.py:8: note: Revealed type is "dep_mod.models.ThingExtra"',
            'user.py:15: note: Revealed type is "my_mod.user.Gadget"',
            'user.py:16: note: Revealed type is "int"',
        ],
    )


KIND = """
from odoo import fields


class Kind(models.Model):
    _name = "x.kind"

    kind = fields.Selection([("a", "A"), ("b", "B")])

    def show(self) -> None:
        reveal_type(self.kind)
"""


def test_dmypy_plugin_settings_follow_config_edits(daemon: Daemon, tmp_path: Path) -> None:
    root = tmp_path / "addons"
    config = tmp_path / "pyproject.toml"
    _append(root / "my_mod/user.py", KIND)
    first = daemon.check()
    config.write_text(config.read_text() + "literal_selection = true\n")
    # dmypy rechecks only what changed: add a class.
    _append(root / "my_mod/user.py", KIND.replace("Kind(", "Kind2(").replace("x.kind", "x.kind2"))
    second = daemon.check()

    assert first[1:] == ['user.py:19: note: Revealed type is "str | Literal[False]"']
    assert second[-1] == (
        "user.py:30: note: Revealed type is \"Literal['a'] | Literal['b'] | Literal[False]\""
    )


VALUES = """

class Note(models.Model):
    _name = "x.note"

    def vals(self) -> "odoo.values.x_thing":
        return {"name": "a", "extra": 1}

    def thing(self) -> "odoo.model.x_thing":
        return self.env["x.thing"]
"""


def test_dmypy_annotations_follow_edits(daemon: Daemon, tmp_path: Path) -> None:
    root = tmp_path / "addons"
    daemon.check()
    _append(root / "my_mod/user.py", VALUES)  # rewritten again after the edit
    before = daemon.check()
    _append(root / "dep_mod/models.py", EXTENSION)  # a new key of odoo.values.x_thing
    after = daemon.check()

    extra = 'user.py:15: error: Extra key "extra" for TypedDict "x.thing values"'
    assert before == [
        'user.py:8: note: Revealed type is "dep_mod.models.Thing"',
        f"{extra}  [typeddict-unknown-key]",
    ]
    assert after == ['user.py:8: note: Revealed type is "dep_mod.models.ThingExtra"']


def test_dmypy_fine_grained_cache_keeps_annotations(daemon: Daemon, tmp_path: Path) -> None:
    # The daemon starts from mypy's cache (no trees); an edit of dep_mod makes it parse
    # user.py again, and the classes of its new analysis replace the cached ones.
    root = tmp_path / "addons"
    wrong = '\n    def wrong(self) -> "odoo.values.x_thing":\n        return {"nope": 1}\n'
    _append(root / "my_mod/user.py", VALUES + wrong)
    mypy_run([*daemon.options, "--cache-fine-grained", *daemon.targets])
    daemon.flags = ["--use-fine-grained-cache"]
    daemon.check()  # mypy reports nothing for modules it loaded from the cache
    _append(root / "dep_mod/models.py", EXTENSION)

    after = daemon.check()

    assert after == [
        'user.py:8: note: Revealed type is "dep_mod.models.ThingExtra"',
        'user.py:21: error: Extra key "nope" for TypedDict "x.thing values"'
        "  [typeddict-unknown-key]",
    ]


OTHER = """
from odoo import fields, models

class Thing(models.Model):
    _inherit = "x.thing"
"""


def test_dmypy_values_keys_follow_an_addon_outside_the_view(daemon: Daemon, tmp_path: Path) -> None:
    # other_mod is checked by the daemon but is no dependency of my_mod: its field is
    # a key of odoo.values.x_thing all the same. Without an error, Note.vals is only
    # analyzed again because it depends on the classes the keys come from.
    root = tmp_path / "addons"
    models = root / "other_mod/models.py"
    write_addon(root / "other_mod", {"depends": ["dep_mod"], "models.py": OTHER})
    models.write_text(models.read_text() + "    extra = fields.Integer()\n")  # in the class
    daemon.targets.append(str(root / "other_mod"))
    _append(root / "my_mod/user.py", VALUES)
    before = daemon.check()
    models.write_text(textwrap.dedent(OTHER))

    after = daemon.check()

    reveal = 'user.py:8: note: Revealed type is "dep_mod.models.Thing"'
    extra = 'user.py:15: error: Extra key "extra" for TypedDict "x.thing values"'
    assert before == [reveal]
    assert after == [reveal, f"{extra}  [typeddict-unknown-key]"]


# -- stage 8: checks that read fields of other modules, and of variables ----------------

FIELDS_USER = """

class Order(models.Model):
    _name = "x.order"

    base_id = fields.Many2one("x.thing", required=True, ondelete="cascade", delegate=True)
    thing_id = fields.Many2one("x.thing")

    def check(self) -> None:
        self.search([("thing_id.name", "=", "a")])
        self.env["x.thing"].search([("name", "=", "a")])
        self.env["x.thing"].create({"name": "a"})
        self.search([("name", "=", "a")])
"""


def _fields_daemon(daemon: Daemon, tmp_path: Path) -> Path:
    root = tmp_path / "addons"
    user = root / "my_mod/user.py"
    user.write_text(
        user.read_text().replace("from odoo import models", "from odoo import fields, models")
    )
    _append(user, FIELDS_USER)
    return root / "dep_mod/models.py"


def _rename(models: Path, old: str, new: str) -> None:
    models.write_text(models.read_text().replace(f"    {old} = ", f"    {new} = "))


def test_dmypy_field_checks_follow_another_module(daemon: Daemon, tmp_path: Path) -> None:
    models = _fields_daemon(daemon, tmp_path)
    first = daemon.check()
    _rename(models, "name", "label")
    renamed = daemon.check()
    _rename(models, "label", "name")
    back = daemon.check()
    _rename(models, "name", "label")
    again = daemon.check()

    errors = [line for line in renamed if "error" in line]
    assert [line for line in first if "error" in line] == []
    assert [line.split(": error: ")[1].split(";")[0].split("  [")[0] for line in errors] == [
        'Odoo model "x.thing" has no field "name" in domain',
        'Odoo model "x.thing" has no field "name" in domain',
        'Odoo model "x.thing" has no field "name"',
        'Odoo model "x.order" has no field "name" in domain',
    ]
    assert [line for line in back if "error" in line] == []
    assert [line for line in again if "error" in line] == errors


def test_dmypy_field_checks_follow_another_module_from_the_cache(
    daemon: Daemon, tmp_path: Path
) -> None:
    models = _fields_daemon(daemon, tmp_path)
    mypy_run([*daemon.options, "--cache-fine-grained", *daemon.targets])
    daemon.flags = ["--use-fine-grained-cache"]
    daemon.check()
    _rename(models, "name", "label")
    renamed = daemon.check()
    _rename(models, "label", "name")
    back = daemon.check()

    assert len([line for line in renamed if "error" in line]) == 4
    assert [line for line in back if "error" in line] == []


def _cached_data(cache: Path, module_path: str) -> bytes:
    (found,) = cache.rglob(f"{module_path}.data.*")
    return found.read_bytes()


def test_fields_digest_only_in_fine_grained_runs(daemon: Daemon, tmp_path: Path) -> None:
    # Only the daemon's triggers read the digest; a full run checks again what an edit
    # of fields changes through the addons' fingerprints, so it does not pay for it.
    plain, fine = tmp_path / "plain", tmp_path / "fine"
    files = ["--no-sqlite-cache"]  # a file per module, not mypy 2.4's database
    mypy_run([*daemon.options, *files, "--cache-dir", str(plain), *daemon.targets])
    mypy_run(
        [*daemon.options, *files, "--cache-dir", str(fine), "--cache-fine-grained", *daemon.targets]
    )

    assert b"__odoo_fields" not in _cached_data(plain, "dep_mod/models")
    assert b"__odoo_fields" in _cached_data(fine, "dep_mod/models")


STORED = """

class Stored(models.Model):
    _inherit = "x.thing"

    total = fields.Float(compute="_compute_total", store=True)

    def _compute_total(self) -> None:
        pass
"""
SEARCH_TOTAL = """

class Counter(models.Model):
    _name = "x.counter"

    def check(self) -> None:
        self.env["x.thing"].search([("total", "=", 1)])
"""


def test_dmypy_field_arguments_of_another_module_are_followed(
    daemon: Daemon, tmp_path: Path
) -> None:
    root = tmp_path / "addons"
    models = root / "dep_mod/models.py"
    _append(models, STORED)
    _append(root / "my_mod/user.py", SEARCH_TOTAL)
    stored = daemon.check()
    models.write_text(models.read_text().replace("store=True", "store=False"))
    computed = daemon.check()
    models.write_text(models.read_text().replace("store=False", "store=True"))
    again = daemon.check()

    assert [line for line in stored if "error" in line] == []
    assert [line.split(": error: ")[1].split(" (")[0] for line in computed if "error" in line] == [
        'Field "total" of Odoo model "x.thing" cannot be searched'
    ]
    assert [line for line in again if "error" in line] == []


VARIABLES = """

class Built(models.Model):
    _name = "x.built"

    def check(self) -> None:
        domain = [("nme", "=", 1)]
        self.env["x.thing"].search(domain)
        vals = {"name": "a"}
        vals.update(nope="b")
        self.env["x.thing"].create(vals)
"""


def test_dmypy_checks_of_variables_survive_edits_of_their_file(
    daemon: Daemon, tmp_path: Path
) -> None:
    user = tmp_path / "addons/my_mod/user.py"
    _append(user, VARIABLES)
    first = daemon.check()
    _append(user, "# a comment\n")
    commented = daemon.check()
    source = user.read_text()
    user.write_text(source.replace('vals.update(nope="b")', 'vals.update(nope="c")'))
    assert user.read_text() != source  # the same function, edited
    edited = daemon.check()

    errors = [line for line in first if "error" in line]
    assert len(errors) == 2
    assert [line for line in commented if "error" in line] == errors
    assert [line for line in edited if "error" in line] == errors


HELPER = """

class Helper(models.Model):
    _name = "x.helper"

    def _domain(self) -> "odoo.domain.x_thing":
        return [("name", "=", 1)]
"""


def test_dmypy_returns_of_annotated_helpers_follow_edits(daemon: Daemon, tmp_path: Path) -> None:
    user = tmp_path / "addons/my_mod/user.py"
    _append(user, HELPER)
    first = daemon.check()
    user.write_text(user.read_text().replace('[("name", "=", 1)]', '[("nme", "=", 1)]'))
    edited = daemon.check()
    user.write_text(user.read_text().replace('[("nme", "=", 1)]', '[("name", "=", 1)]'))
    fixed = daemon.check()

    assert [line for line in first if "error" in line] == []
    assert [line.split("; did")[0] for line in edited if "error" in line] == [
        'user.py:15: error: Odoo model "x.thing" has no field "nme" in domain'
    ]
    assert [line for line in fixed if "error" in line] == []


# -- hooks chosen from declared signatures, in files the registry does not read ---------

UNSCANNED_HELPER = """
from odoo import models


class Helper:
    def find(self, domain: list) -> None:
        pass
"""
UNSCANNED_CALLER = """
from .common import Helper


class TestX:
    def test(self) -> None:
        Helper().find([("nme", "=", 1)])
"""


def test_dmypy_domain_parameters_of_an_unscanned_helper_follow_edits(
    daemon: Daemon, tmp_path: Path
) -> None:
    # my_mod/tests is not imported by the addon: no scan reads it, so an edit of it
    # does not start a new registry, and the hooks of its methods must follow anyway.
    tests = tmp_path / "addons/my_mod/tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("from . import test_x\n")
    (tests / "common.py").write_text(textwrap.dedent(UNSCANNED_HELPER))
    (tests / "test_x.py").write_text(textwrap.dedent(UNSCANNED_CALLER))
    plain = daemon.check()
    annotated = UNSCANNED_HELPER.replace("domain: list", 'domain: "odoo.domain.x_thing"')
    (tests / "common.py").write_text(textwrap.dedent(annotated))
    checked = daemon.check()
    (tests / "common.py").write_text(textwrap.dedent(UNSCANNED_HELPER))
    reverted = daemon.check()

    assert [line for line in plain if "error" in line] == []
    assert [line.split("; did")[0] for line in checked if "error" in line] == [
        'tests/test_x.py:7: error: Odoo model "x.thing" has no field "nme" in domain'
    ]
    assert [line for line in reverted if "error" in line] == []


SHARED_FINDER = """
from odoo.orm.types import DomainType


class Finder:
    def find(self, domain: list) -> None:
        pass
"""
SHARED_CALLER = """
from odoo import models
from odoo.orm.domains import Domain

from shared.helpers import Finder


class Caller:
    def run(self, narrow: list[tuple[str, str, str]], whole: Domain) -> None:
        Finder().find(narrow)
        Finder().find(whole)
"""


@pytest.mark.odoo(19, 20)
def test_dmypy_domain_type_parameter_outside_addons_widens_lists(
    daemon: Daemon, tmp_path: Path
) -> None:
    # A plain package next to the addons (no manifest): never an input of the registry.
    shared = tmp_path / "addons/shared"
    shared.mkdir()
    (shared / "__init__.py").write_text("")
    (shared / "helpers.py").write_text(textwrap.dedent(SHARED_FINDER))
    (tmp_path / "addons/my_mod/caller.py").write_text(textwrap.dedent(SHARED_CALLER))
    daemon.targets.append(str(shared))
    before = daemon.check()
    typed = SHARED_FINDER.replace("domain: list", "domain: DomainType")
    (shared / "helpers.py").write_text(textwrap.dedent(typed))

    after = daemon.check()

    # The Domain argument only "domain: list" rejects: caller.py was checked again.
    assert [line.split(": error: ")[0] for line in before if "error" in line] == ["caller.py:11"]
    assert 'incompatible type "Domain"' in "".join(before)
    assert [line for line in after if "error" in line] == []


# -- attributes a class method defines (hooks/class_attributes.py) ----------------------

CLASS_COMMON = """
from odoo.tests.common import TransactionCase


class Common(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.thing = cls.env["x.thing"].create({"name": "A"})
"""
CLASS_TEST = """
from .common import Common


class TestThing(Common):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        reveal_type(cls.thing)

    def test_read(self) -> None:
        reveal_type(self.thing.name)
        self.other
"""


def test_dmypy_attributes_of_class_methods_follow_edits(daemon: Daemon, tmp_path: Path) -> None:
    tests = tmp_path / "addons/my_mod/tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("from . import test_thing\n")
    (tests / "common.py").write_text(textwrap.dedent(CLASS_COMMON))
    (tests / "test_thing.py").write_text(textwrap.dedent(CLASS_TEST))
    first = daemon.check()
    with (tests / "common.py").open("a") as handle:
        handle.write("        cls.other = cls.thing.name\n")
    second = daemon.check()
    (tests / "common.py").write_text(textwrap.dedent(CLASS_COMMON) + "undefined_name\n")
    broken = daemon.check()  # mypy analyzes the module's top level once more
    (tests / "common.py").write_text(textwrap.dedent(CLASS_COMMON))
    third = daemon.check()

    typed = [
        'tests/test_thing.py:9: note: Revealed type is "dep_mod.models.Thing"',
        'tests/test_thing.py:12: note: Revealed type is "str | Literal[False]"',
    ]
    missing = 'tests/test_thing.py:13: error: "TestThing" has no attribute "other"  [attr-defined]'
    undefined = 'tests/common.py:10: error: Name "undefined_name" is not defined  [name-defined]'
    tests_only = [
        [line for line in lines if line.startswith("tests/")]
        for lines in (first, second, broken, third)
    ]
    assert tests_only == [
        [*typed, missing],
        typed,
        [*typed, missing, undefined],
        [*typed, missing],
    ]
