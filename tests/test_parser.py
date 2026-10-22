from pathlib import Path
import pytest
from app.parser import parse_pdf

DATA = Path(__file__).resolve().parents[1] / "data"
V1 = DATA / "ct200_manual.pdf"
V2 = DATA / "ct200_manual_v2.pdf"

@pytest.fixture(scope="module")
def parsed_v1():
    return parse_pdf(V1)

@pytest.fixture(scope="module")
def parsed_v2():
    return parse_pdf(V2)

def parent_heading_map(result):
    by_id = {n.temp_id: n for n in result.nodes}
    return {n.heading: by_id[n.parent_temp_id].heading if n.parent_temp_id else None for n in result.nodes}

def test_duplicate_error_codes_headings_remain_distinct_under_correct_parents(parsed_v1):
    matches = [n for n in parsed_v1.nodes if n.heading.split(" ", 1)[-1] == "Error Codes"]
    assert len(matches) == 2
    assert matches[0].temp_id != matches[1].temp_id
    parents = parent_heading_map(parsed_v1)
    assert parents[matches[0].heading] == "4 Alarms and Safety Behavior"
    assert parents[matches[1].heading] == "7 Troubleshooting"
    assert matches[0].path != matches[1].path

def test_irregular_skipped_heading_level_is_preserved_and_reported(parsed_v1):
    node = next(n for n in parsed_v1.nodes if n.section_number == "2.1.1.1")
    by_id = {n.temp_id: n for n in parsed_v1.nodes}
    assert by_id[node.parent_temp_id].section_number == "2.1"
    assert any("skips an explicit parent section (2.1.1)" in w for w in parsed_v1.warnings)
    assert node.section_number == "2.1.1.1"  # do not silently rewrite source numbering

def test_out_of_order_3_4_and_3_3_are_not_silently_sorted(parsed_v1):
    headings = [n.heading for n in parsed_v1.nodes]
    assert headings.index("3.4 Auto Shutoff") < headings.index("3.3 Result Display and Classification")
    parents = parent_heading_map(parsed_v1)
    assert parents["3.4 Auto Shutoff"] == "3 Device Operation"
    assert parents["3.3 Result Display and Classification"] == "3 Device Operation"
    assert any("original order preserved" in w for w in parsed_v1.warnings)

def test_table_like_rows_are_retained_as_ordered_text(parsed_v1):
    specs = next(n for n in parsed_v1.nodes if n.section_number == "2.1")
    errors = next(n for n in parsed_v1.nodes if n.section_number == "4.2")
    assert "Parameter" in specs.body_text and "Oscillometric" in specs.body_text and "0–299 mmHg" in specs.body_text
    assert "E1" in errors.body_text and "E5" in errors.body_text and "Overpressure condition" in errors.body_text
    assert specs.metadata.get("table_like_content") is True
    assert errors.metadata.get("table_like_content") is True

def test_v2_has_added_section_and_real_requirement_changes(parsed_v1, parsed_v2):
    old_battery = next(n for n in parsed_v1.nodes if n.section_number == "2.1.1.1")
    new_battery = next(n for n in parsed_v2.nodes if n.section_number == "2.1.1.1")
    assert "300 measurement cycles" in old_battery.body_text
    assert "250 measurement cycles" in new_battery.body_text
    assert old_battery.content_hash != new_battery.content_hash
    assert any(n.section_number == "5.3" for n in parsed_v2.nodes)
