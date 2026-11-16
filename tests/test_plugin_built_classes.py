"""End-to-end tests of classes whose ``__new__`` builds another class's object.

Odoo 20's ``frozendict`` returns a ``MappingProxyType`` (:mod:`odoo_typing.hooks.built_classes`).
"""

from __future__ import annotations

import pytest
from conftest import RunMypy


def test_class_that_builds_another_class_has_its_methods(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "tools.py": """
                from collections import OrderedDict
                from types import MappingProxyType

                class ordered[K, V]:
                    def __new__(cls, mapping: dict[K, V]):
                        return OrderedDict(mapping)

                class Plain:
                    def __new__(cls):
                        return super().__new__(cls)

                class Either:
                    def __new__(cls, flag: bool):
                        if flag:
                            return OrderedDict()
                        return dict()

                class Declared:
                    def __new__(cls) -> "Declared":
                        return OrderedDict()  # type: ignore[return-value]

                class Final:
                    def __new__(cls):
                        return MappingProxyType({})

                class Holder:
                    context: ordered[str, int]

                    def read(self, either: Either, declared: Declared, final: Final) -> None:
                        reveal_type(self.context.get("a"))
                        reveal_type(self.context["a"])
                        reveal_type(dict(self.context))
                        reveal_type("a" in self.context)
                        reveal_type(ordered({"a": 1}).get("a"))
                        self.context.move_to_end("a")
                        self.context.missing
                        Plain().get
                        either.get
                        declared.get
                        final.get
                """
            }
        }
    )

    messages = result.messages("tools.py")
    assert messages[:5] == [
        '31: note: Revealed type is "int | None"',
        '32: note: Revealed type is "int"',
        '33: note: Revealed type is "dict[str, int]"',
        '34: note: Revealed type is "bool"',
        '35: note: Revealed type is "int | None"',
    ]
    assert [message.partition(" error: ")[2] for message in messages[5:]] == [
        '"ordered[str, int]" has no attribute "missing"  [attr-defined]',
        '"Plain" has no attribute "get"  [attr-defined]',
        '"Either" has no attribute "get"  [attr-defined]',
        '"Declared" has no attribute "get"  [attr-defined]',
        # A final class is no base in a module whose errors are shown.
        '"Final" has no attribute "get"  [attr-defined]',
    ]


@pytest.mark.odoo(20)
def test_context_of_odoo_20_is_a_mapping(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import models

                class Thing(models.Model):
                    _name = "t.thing"

                    def action(self) -> None:
                        reveal_type(self.env.context.get("lang"))
                        reveal_type(self.env.context["lang"])
                        reveal_type(dict(self.env.context))
                        self.with_context(**self.env.context)
                        self.env.context.missing
                """
            }
        }
    )

    messages = result.messages("models.py")
    assert messages[:3] == [
        '8: note: Revealed type is "Any | None"',
        '9: note: Revealed type is "Any"',
        '10: note: Revealed type is "dict[Any, Any]"',
    ]
    assert len(messages) == 4, messages
    assert 'has no attribute "missing"' in messages[3]


@pytest.mark.odoo(20)
def test_odoo_20_parameters_annotated_dict_take_the_context(run_mypy: RunMypy) -> None:
    """``clean_context(self.env.context)``, ``with_context(self.env.context)`` (Odoo 20)."""
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import models
                from odoo.tools import clean_context

                def own(context: dict[str, object]) -> None:
                    pass

                class Thing(models.Model):
                    _name = "t.thing"

                    def action(self) -> None:
                        self.with_context(clean_context(self.env.context))
                        self.with_context(self.env.context)
                        self.with_context(1)
                        own(self.env.context)
                """
            }
        }
    )

    # Only Odoo's own annotations take it: a function of the addon is its author's word.
    assert [message.split(";")[0] for message in result.messages("models.py")] == [
        '14: error: Argument 1 to "with_context" of "BaseModel" has incompatible type "int"',
        '15: error: Argument 1 to "own" has incompatible type "frozendict[Any, Any]"',
    ]
