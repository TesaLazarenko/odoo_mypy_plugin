"""The reads of a field where the test says the records do not have it."""

from __future__ import annotations

import ast
import textwrap

from odoo_typing.denied_reads import denied_reads, if_statements

SOURCE = """
def use(self, order, other):
    if "f" in self:
        self.f
    else:
        self.f
        other.f
        self.g
    if not "f" in order.partner_id._fields:
        order.partner_id.f
        order.f
    if "f" not in other:
        return
    other.f
    if "f" in other._fields:
        raise ValueError
    other.f = 1
    print(other.f)
    if "f" in self and other:
        pass
    else:
        self.f
    x = 1 if "f" in self else self.f
    if "g" in self: pass
    elif "g" in other: pass
    else: other.g
"""


def _reads(line: int, name: str, records: str) -> list[int]:
    statements = if_statements(ast.parse(textwrap.dedent(SOURCE)))
    return [at for at, _ in denied_reads(statements, line, name, records, _ends)]


def test_reads_in_the_branch_where_the_answer_is_no() -> None:
    assert _reads(3, "f", "self") == [6]  # the else branch; not other.f, not self.g
    assert _reads(9, "f", "order.partner_id") == [10]  # ``not`` of the question
    assert _reads(15, "f", "other") == [18]  # after a branch that ends; not the assignment


def test_no_reads_where_the_answer_is_yes_or_unknown() -> None:
    assert _reads(12, "f", "other") == []  # ``not in`` with return: the rest has the field
    assert _reads(19, "f", "self") == []  # ``and``: false does not say the field is missing
    assert _reads(23, "f", "self") == []  # a conditional expression
    assert _reads(3, "f", "other") == []  # other records
    assert _reads(3, "g", "self") == []  # another field
    assert _reads(25, "g", "other") == [26]  # elif: an if statement of its own


DOUBTS = """
def use(self, rec, other):
    if "f" in rec:
        return rec.f
    rec = other.partner_id
    return rec.f

def loop(self, rec, other):
    if "f" not in rec:
        for rec in other:
            print(rec.f)

def scopes(self, rec, other):
    if "f" not in rec:
        other.filtered(lambda rec: rec.f)
        [rec.f for rec in other]
        rec.f

def asked_again(self, rec, other):
    if "f" not in rec:
        if hasattr(rec, "f"):
            rec.f

def unreachable(self, rec, other):
    if "f" not in rec:
        raise ValueError
        rec.f

def typed(self, rec, other):
    if "f" not in rec:
        if isinstance(rec, type(other)):
            rec.f

def walrus(self, rec, other):
    if "f" not in rec:
        [(rec := item) for item in other]
        rec.f

def outer(self, rec, other):
    if "f" not in rec:
        def inner():
            nonlocal rec
            rec = other
        inner()
        rec.f

def nested(self, rec, other):
    if "f" not in rec:
        if other:
            return
            rec.g
        rec.f

def captured(self, rec, other):
    if "f" not in rec:
        match other:
            case {**rec}:
                print(rec.f)

def stopped(self, rec, other):
    if "f" not in rec:
        rec.stop()
        rec.f
"""


def _ends(line: int, method: str | None) -> bool:
    """No call ends the statements after it but ``rec.stop()``, of line 62."""
    return line == 62 and method is None


def _doubtful(line: int) -> list[int]:
    statements = if_statements(ast.parse(textwrap.dedent(DOUBTS)))
    return [at for at, _ in denied_reads(statements, line, "f", "rec", _ends)]


def test_nothing_is_reported_where_the_source_leaves_a_doubt() -> None:
    assert _doubtful(3) == []  # the name holds other records after the test
    assert _doubtful(9) == []  # a loop variable of that name
    assert _doubtful(20) == []  # asked again inside
    assert _doubtful(25) == []  # after raise: unreachable
    assert _doubtful(30) == []  # asked by isinstance
    assert _doubtful(35) == []  # a walrus in a comprehension binds the name here
    assert _doubtful(40) == []  # nonlocal in a nested function
    assert _doubtful(55) == []  # a case capture of the rest of a mapping
    assert _doubtful(61) == []  # after a call that does not return
    assert _doubtful(14) == [17]  # not the lambda's nor the comprehension's rec


def test_other_scopes_are_not_read_and_a_return_ends_only_its_block() -> None:
    assert _doubtful(14) == [17]  # not the lambda's nor the comprehension's rec
    assert _doubtful(48) == [52]  # the return ends the inner block


ATTRIBUTES = """
def use(self, other):
    if "f" not in self.partner:
        self.partner = other
        self.partner.f
    if "f" not in self.partner:
        self.partner.f
"""


def test_an_assignment_to_the_records_attribute_is_a_doubt() -> None:
    statements = if_statements(ast.parse(textwrap.dedent(ATTRIBUTES)))

    assert denied_reads(statements, 3, "f", "self.partner", _ends) == []
    assert [at for at, _ in denied_reads(statements, 6, "f", "self.partner", _ends)] == [7]


SKIPPED = """
def test_has_it(self, rec):
    if "f" in rec:
        self.skipTest("rec has f")
    rec.f

def test_returns(self, rec, helper):
    if "f" in rec:
        helper.skipTest("rec has f")
    rec.f

def test_lacks_it(self, rec):
    if "f" not in rec:
        rec.g
    else:
        self.skipTest("rec has f")
    rec.f

def test_skipped_inside(self, rec, flag):
    if "f" in rec:
        if flag:
            self.skipTest("rec has f")
    rec.f

def test_wrapped(self, rec):
    if "f" in rec:
        (
            self.skipTest("rec has f")
        )
    rec.f

def test_two_on_a_line(self, rec, flag, helper):
    if "f" in rec:
        flag and self.skipTest("a"); helper.skipTest("b")
    rec.f

def test_logged(self, rec):
    if "f" in rec:
        print("rec has f")
        self.skipTest("rec has f")
    rec.f

def test_opened(self, rec):
    if "f" in rec:
        print("rec has f"); (
            self.skipTest("rec has f")
        )
    rec.f
"""


def test_a_branch_that_skips_the_test_ends_as_return_does() -> None:
    """``self.skipTest(...)`` raises ``SkipTest``: what follows runs when the answer is no.

    Only a call ``may_end`` says is declared not to return (false for ``skipTest``: a
    method of that name declared ``-> None``, or not known), whatever the other calls of
    its line; asked on the line of the call, also in parentheses.
    """
    statements = if_statements(ast.parse(textwrap.dedent(SKIPPED)))

    def may_end(line: int, method: str | None) -> bool:
        # On line 9 ``helper.skipTest`` returns; another call there is not known.
        return line in {4, 16, 22, 28, 34, 40, 46} or (line == 9 and method is None)

    def reads(line: int) -> list[int]:
        return [at for at, _ in denied_reads(statements, line, "f", "rec", may_end)]

    assert reads(3) == [5]  # the field is missing past the skipped branch
    assert reads(8) == []  # a skipTest that returns ends nothing
    assert reads(13) == [17]  # ``not in``: the else branch skips, the rest lacks the field
    assert reads(20) == []  # the branch ends with an ``if`` without ``else``, not a skip
    assert reads(26) == [30]  # the statement begins on 27, its call on 28
    assert reads(33) == []  # ``may_end`` answers for line 34, which two statements share
    assert reads(38) == [41]  # a statement before the skip, on a line of its own
    assert reads(44) == [48]  # the statement before ends on 45, the skip's call is on 46
