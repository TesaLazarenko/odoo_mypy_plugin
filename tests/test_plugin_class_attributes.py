"""End-to-end tests of the attributes a class method defines (``cls.x = ...``).

Odoo's tests create their records in ``setUpClass`` and read them in every test; mypy
alone reports each such line as an unknown attribute
(:mod:`odoo_typing.hooks.class_attributes`).
"""

from __future__ import annotations

from conftest import RunMypy

COMMON = """
from odoo.tests.common import TransactionCase

class Common(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({"name": "A"})
        cls.count = 1
        cls.rows = []
        cls.rows.append(cls.count)
        cls.first, cls.second = "a", 2.0
        if cls.count:
            cls.inside = cls.partner.name
        cls._more()

    @classmethod
    def _more(cls):
        cls.thing = cls.env["t.thing"].create({"name": cls.partner.name})

    def setUp(self):
        super().setUp()
        self.for_one_test = 1
"""

THING = """
from odoo import fields, models

class Thing(models.Model):
    _name = "t.thing"
    _description = "Thing"

    name = fields.Char(required=True)
"""


def _addon(test: str) -> dict[str, dict[str, object]]:
    return {
        "my_mod": {
            "__init__.py": "from . import models\n",
            "models.py": THING,
            "tests/__init__.py": "from . import common, test_thing\n",
            "tests/common.py": COMMON,
            "tests/test_thing.py": test,
        }
    }


def test_attributes_of_set_up_class_have_the_types_of_their_values(run_mypy: RunMypy) -> None:
    result = run_mypy(
        _addon(
            """
            from .common import Common

            class TestThing(Common):
                @classmethod
                def setUpClass(cls):
                    super().setUpClass()
                    cls.copy = cls.thing.copy()
                    reveal_type(cls.partner.name)

                def test_read(self):
                    reveal_type(self.thing)
                    reveal_type(self.copy)
                    reveal_type(self.count)
                    reveal_type(self.rows)
                    reveal_type(self.first)
                    reveal_type(self.second)
                    reveal_type(self.inside)
                    reveal_type(type(self).thing)
                    reveal_type(TestThing.count)
            """
        )
    )

    assert result.messages("tests/common.py") == []
    assert result.messages("tests/test_thing.py") == [
        '9: note: Revealed type is "str | Literal[False]"',
        '12: note: Revealed type is "my_mod.models.Thing"',
        '13: note: Revealed type is "my_mod.models.Thing"',
        '14: note: Revealed type is "int"',
        '15: note: Revealed type is "list[int]"',
        '16: note: Revealed type is "str"',
        '17: note: Revealed type is "float"',
        '18: note: Revealed type is "str | Literal[False]"',
        '19: note: Revealed type is "my_mod.models.Thing"',
        '20: note: Revealed type is "int"',
    ]


def test_records_of_set_up_class_are_checked_like_any_record(run_mypy: RunMypy) -> None:
    result = run_mypy(
        _addon(
            """
            from .common import Common

            class TestThing(Common):
                def test_write(self):
                    self.thing.write({"nme": "x"})
                    self.thing.search([("nme", "=", "x")])
                    self.thing.missing
                    self.unknown
            """
        )
    )

    messages = result.messages("tests/test_thing.py")
    assert len(messages) == 4, messages
    assert 'has no field "nme"' in messages[0]
    assert messages[0].endswith("[odoo-vals-key]")
    assert 'has no field "nme"' in messages[1]
    assert '8: error: "Thing" has no attribute "missing"' in messages[2]
    assert '9: error: "TestThing" has no attribute "unknown"' in messages[3]


def test_a_later_value_of_another_type_is_still_an_error(run_mypy: RunMypy) -> None:
    result = run_mypy(
        _addon(
            """
            from .common import Common

            class TestThing(Common):
                @classmethod
                def setUpClass(cls):
                    super().setUpClass()
                    cls.count = "two"
            """
        )
    )

    assert result.messages("tests/test_thing.py") == [
        '8: error: Incompatible types in assignment (expression has type "str", variable has'
        ' type "int")  [assignment]'
    ]


def test_attribute_of_an_instance_method_stays_instance_only(run_mypy: RunMypy) -> None:
    result = run_mypy(
        _addon(
            """
            from .common import Common

            class TestThing(Common):
                def test_read(self):
                    reveal_type(self.for_one_test)
                    type(self).for_one_test
            """
        )
    )

    assert result.messages("tests/test_thing.py") == [
        '6: note: Revealed type is "int"',
        '7: error: Cannot access instance-only attribute "for_one_test" on class object  [misc]',
    ]


def test_class_method_of_any_class_defines_attributes(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "tools.py": """
                class Registry:
                    names: list[str] = []

                    @classmethod
                    def load(klass, path: str) -> None:
                        klass.path = path
                        klass.names = [path]

                    @staticmethod
                    def other(cls) -> None:
                        cls.unrelated = 1

                    def read(self) -> None:
                        reveal_type(self.path)
                        reveal_type(Registry.path)
                        reveal_type(self.names)
                        Registry.unrelated
                """
            }
        }
    )

    assert result.messages("tools.py") == [
        '15: note: Revealed type is "str"',
        '16: note: Revealed type is "str"',
        '17: note: Revealed type is "list[str]"',
        '18: error: "type[Registry]" has no attribute "unrelated"  [attr-defined]',
    ]


def test_attributes_are_read_on_the_class_from_a_warm_cache(run_mypy: RunMypy) -> None:
    test = """
    from .common import Common

    class TestThing(Common):
        @classmethod
        def setUpClass(cls):
            super().setUpClass()
            reveal_type(cls.thing)
            reveal_type(Common.count)
    """
    expected = [
        '8: note: Revealed type is "my_mod.models.Thing"',
        '9: note: Revealed type is "int"',
    ]
    cold = run_mypy(_addon(test), fresh_cache=True)
    assert cold.messages("tests/test_thing.py") == expected

    # Only the test module is checked again: its base class comes from the cache.
    warm = run_mypy(_addon(test + "\n"), fresh_cache=True)

    assert warm.messages("tests/test_thing.py") == expected
    assert warm.messages("tests/common.py") == []


def test_records_kept_by_another_addons_test_class_read_as_the_reading_addon_sees_them(
    run_mypy: RunMypy,
) -> None:
    result = run_mypy(
        {
            "dep_mod": {
                "__init__.py": "from . import models\n",
                "models.py": THING,
                "tests/__init__.py": "from . import common\n",
                "tests/common.py": """
                from odoo.tests.common import TransactionCase

                class Common(TransactionCase):
                    @classmethod
                    def setUpClass(cls):
                        super().setUpClass()
                        cls.thing = cls.env["t.thing"].create({"name": "A"})
                        cls.found = cls.env["t.thing"].search([], limit=1) or None

                    def setUp(self):
                        super().setUp()
                        self.other = self.thing.copy()
                """,
            },
            "my_mod": {
                "depends": ["dep_mod"],
                "__init__.py": "from . import models\n",
                "models.py": """
                from odoo import fields, models

                class Thing(models.Model):
                    _inherit = "t.thing"

                    extra = fields.Integer()
                """,
                "tests/__init__.py": "from . import test_thing\n",
                "tests/test_thing.py": """
                from dep_mod.tests.common import Common

                class TestThing(Common):
                    @classmethod
                    def setUpClass(cls):
                        super().setUpClass()
                        reveal_type(cls.thing.extra)

                    def test_read(self):
                        reveal_type(self.thing.extra)
                        reveal_type(self.other.extra)
                        self.thing.extre
                        self.thing = self.env["t.thing"].browse(1)
                        self.thing = self.other
                        if self.found:  # a union of records and None
                            reveal_type(self.found.extra)
                """,
            },
        }
    )

    assert result.messages("dep_mod/tests/common.py") == []
    messages = result.messages("my_mod/tests/test_thing.py")
    assert messages[:3] == [
        '8: note: Revealed type is "int"',
        '11: note: Revealed type is "int"',
        '12: note: Revealed type is "int"',
    ]
    assert len(messages) == 5, messages
    assert 'has no attribute "extre"' in messages[3]
    assert messages[4] == '17: note: Revealed type is "int"'


def test_attribute_hook_named_after_another_method_is_that_method(run_mypy: RunMypy) -> None:
    """``__getattr__ = __getitem__`` (Odoo 20's ``TableSQL``): mypy alone finds no hook."""
    result = run_mypy(
        {
            "my_mod": {
                "tools.py": """
                class Base:
                    pass

                class Table(Base):
                    def __getitem__(self, name: str) -> int:
                        return len(name)

                    __getattr__ = __getitem__

                class Plain(Base):
                    pass

                def read(table: Table, plain: Plain) -> None:
                    reveal_type(table.anything)
                    reveal_type(table["anything"])
                    plain.anything
                """
            }
        }
    )

    assert result.messages("tools.py") == [
        '15: note: Revealed type is "int"',
        '16: note: Revealed type is "int"',
        '17: error: "Plain" has no attribute "anything"  [attr-defined]',
    ]


def test_attribute_hook_declared_to_return_its_own_base_gives_any(run_mypy: RunMypy) -> None:
    """Odoo 20's ``table.order_id.partner_id``: each step is declared ``-> SQL``, the base."""
    result = run_mypy(
        {
            "my_mod": {
                "tools.py": """
                class Sql:
                    code: str = ""

                class Table(Sql):
                    alias: str = "t"

                    def __getitem__(self, name: str) -> Sql:
                        return Field(self, name)

                    __getattr__ = __getitem__

                class Field(Sql):
                    def __init__(self, table: Table, name: str) -> None:
                        self.table = table

                    def __getattr__(self, name: str) -> Sql:
                        return Field(self.table, name)

                class Own:
                    def __getattr__(self, name: str) -> "Own":
                        return self

                def read(table: Table, own: Own) -> None:
                    reveal_type(table.order_id)
                    reveal_type(table.order_id.partner_id.name)
                    reveal_type(table.alias)
                    reveal_type(table["order_id"])
                    reveal_type(own.anything.more)
                """
            }
        }
    )

    assert result.messages("tools.py") == [
        '25: note: Revealed type is "Any"',
        '26: note: Revealed type is "Any"',
        '27: note: Revealed type is "str"',
        '28: note: Revealed type is "my_mod.tools.Sql"',
        '29: note: Revealed type is "my_mod.tools.Own"',
    ]
