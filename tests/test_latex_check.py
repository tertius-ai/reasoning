"""Tests for the KaTeX renderability checker."""

import pytest

from reasoning_ledger import latex_check as lc

pytestmark = pytest.mark.skipif(
    not lc.available(), reason="Node + KaTeX checker unavailable"
)


class TestExtraction:
    def test_delimiters_and_modes(self):
        text = r"inline \(a\) and $b$; display \[c\] and $$d$$"
        spans = lc.extract_spans(text)
        assert [(s.text, s.display) for s in spans] == [
            ("a", False),
            ("b", False),
            ("c", True),
            ("d", True),
        ]

    def test_code_is_ignored(self):
        text = r"real \(x\) but `not $y$` and ```\(z\)``` fenced"
        spans = [s.text for s in lc.extract_spans(text)]
        assert spans == ["x"]

    def test_escaped_dollar_not_a_delimiter(self):
        # a literal \$ (a price) does not open a math span
        assert lc.extract_spans(r"it costs \$5 today") == []


class TestChecking:
    def test_clean_math_passes(self):
        assert lc.check_text(r"\(x^2 + y^2 = z^2\) and \[\frac{a}{b}\]") == []

    def test_undefined_macro_fails(self):
        fails = lc.check_text(r"the ring \(\RR\)")
        assert len(fails) == 1
        assert "Undefined control sequence" in fails[0].error
        assert fails[0].span == r"\RR"

    def test_paper_operator_macros_pass(self):
        # The chat defines \CH and similar operator names; the checker agrees.
        assert lc.check_text(r"\(\CH^3_L(Y)\) and \[\Hom(\CH^3(Y,1), \colim_n A_n)\]") == []

    def test_own_newcommand_for_a_table_macro_passes(self):
        assert lc.check_text(r"\(\newcommand{\CH}{A} \CH^3\) then \(x\)") == []

    def test_unsupported_environment_fails(self):
        fails = lc.check_text(r"\[\boxed{\begin{minipage}{2in}x\end{minipage}}\]")
        assert fails and "minipage" in fails[0].error
        assert fails[0].display is True

    def test_check_fields_reports_first_error_with_label(self):
        err = lc.check_fields(
            {"statement": r"clean \(x\)", "notes": r"broken \(\notamacro\)"}
        )
        assert err is not None
        assert err.startswith("notes contains LaTeX")
        assert "Undefined control sequence" in err

    def test_check_fields_all_clean(self):
        assert (
            lc.check_fields({"statement": r"\(x\)", "notes": "plain prose"}) is None
        )

    def test_empty_and_prose_only(self):
        assert lc.check_text("") == []
        assert lc.check_text("no math here at all") == []
