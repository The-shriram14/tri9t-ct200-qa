from app.versioning import diff_summary


def test_numeric_change_excerpts_contain_full_numbers():
    """The battery section changed 300→250 cycles and 15%→10%.
    The diff excerpts must show the complete numbers, not fragments."""
    old = ("2.1.1.1 Battery Life\n"
           "The CT-200 uses 4 AA alkaline batteries and supports approximately "
           "300 measurement cycles per set under typical use. A low-battery "
           "indicator appears when remaining capacity falls below 15%.")
    new = ("2.1.1.1 Battery Life\n"
           "The CT-200 uses 4 AA alkaline batteries and supports approximately "
           "250 measurement cycles per set under typical use. A low-battery "
           "indicator appears when remaining capacity falls below 10%.")
    result = diff_summary(old, new)
    assert result["changed"] is True
    assert "300" in result["old_excerpt"]
    assert "250" in result["new_excerpt"]
    assert "15%" in result["old_excerpt"]
    assert "10%" in result["new_excerpt"]
    # Must NOT contain the character-level fragments
    assert result["old_excerpt"] != "30 … 5"
    assert result["new_excerpt"] != "25 … 0"


def test_unchanged_text_reports_no_change():
    result = diff_summary("Identical content here.", "Identical content here.")
    assert result["changed"] is False
    assert result["old_excerpt"] is None
    assert result["new_excerpt"] is None


def test_percentage_change_is_not_fragmented():
    """A change from 95% to 90% should show '95%' and '90%', not '5' and '0'."""
    old = "The accuracy is within 95% confidence."
    new = "The accuracy is within 90% confidence."
    result = diff_summary(old, new)
    assert result["changed"] is True
    assert "95%" in result["old_excerpt"]
    assert "90%" in result["new_excerpt"]


def test_word_insertion_shows_added_word():
    result = diff_summary("Hello world.", "Hello beautiful world.")
    assert result["changed"] is True
    assert "beautiful" in result["new_excerpt"]


def test_word_deletion_shows_removed_word():
    result = diff_summary("Hello beautiful world.", "Hello world.")
    assert result["changed"] is True
    assert "beautiful" in result["old_excerpt"]


def test_multiple_differing_spans_are_joined():
    old = "Alpha one beta two gamma three."
    new = "Alpha 1 beta 2 gamma 3."
    result = diff_summary(old, new)
    assert result["changed"] is True
    assert "one" in result["old_excerpt"]
    assert "two" in result["old_excerpt"]
    assert "1" in result["new_excerpt"]
    assert "2" in result["new_excerpt"]
