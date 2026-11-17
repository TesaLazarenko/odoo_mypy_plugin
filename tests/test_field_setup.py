"""The attributes the setup of Odoo's field classes rewrites (``odoo_typing.field_setup``)."""

from __future__ import annotations

from pathlib import Path

from odoo_typing.field_setup import FieldSetup

SOURCE = """
class Field:
    def _get_attrs(self, model_class, name):
        attrs = dict(self._args__)
        attrs['model_name'] = model_class._name
        if 'depends' in attrs:
            attrs['_depends'] = tuple(attrs.pop('depends'))
        del attrs['oldname']
        attrs.setdefault('copy', True)
        return attrs

    def _setup_attrs__(self, model_class, name):
        attrs = self._get_attrs(model_class, name)
        self.__dict__.update(attrs)
        if not self.string:
            self.string = name.title()
        other.groups = None

    def setup(self, model):
        self.help = None
        attrs = {}
        attrs['size'] = None


class Selection(Field):
    def _setup_attrs(self, model_class, name):
        self.ondelete: dict = {}
        self.selection += []
"""


def test_the_names_the_setup_methods_assign_delete_or_pop(tmp_path: Path) -> None:
    path = tmp_path / "fields.py"
    path.write_text(SOURCE)

    rewritten = FieldSetup().rewritten(str(path))

    assert rewritten == {
        "model_name",
        "_depends",
        "depends",
        "oldname",
        "string",
        "ondelete",
        "selection",
    }


def test_a_file_that_cannot_be_read_rewrites_nothing(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("def _get_attrs(:\n")

    setup = FieldSetup()

    assert setup.rewritten(str(broken)) == frozenset()
    assert setup.rewritten(str(tmp_path / "missing.py")) == frozenset()


def test_odoo_keeps_the_arguments_this_plugin_reads_from_field_objects(odoo_path: Path) -> None:
    """``size``, ``groups`` and ``help`` stay as given; ``ondelete`` and ``depends`` do not."""
    files = [odoo_path / "fields.py", *sorted((odoo_path / "orm").glob("fields*.py"))]
    setup = FieldSetup()

    rewritten = frozenset().union(*(setup.rewritten(str(file)) for file in files if file.exists()))

    assert {"ondelete", "depends", "string", "compute_sudo"} <= rewritten
    assert not {"size", "groups", "help", "comodel_name"} & rewritten
