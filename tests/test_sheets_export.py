import csv
import json

import pytest

from automation_kit import sheets_export
from automation_kit.sheets_export import export_rows, sheets_cell


@pytest.mark.parametrize("value,expected", [
    ("=HYPERLINK(\"http://x\",\"y\")", "'=HYPERLINK(\"http://x\",\"y\")"),
    ("+cmd", "'+cmd"), ("@SUM(A1)", "'@SUM(A1)"), ("-cmd", "'-cmd"),
    ("-5", "-5"), ("+3.5", "+3.5"), ("12", "12"), (12, "12"),
    ("2024-05-03", "2024-05-03"), ("plain", "plain"),
    (True, "TRUE"), (False, "FALSE"), (None, ""),
    ({"b": 1, "a": [1, 2]}, '{"a": [1, 2], "b": 1}'),
])
def test_sheets_cell(value, expected):
    assert sheets_cell(value) == expected


def test_escape_can_be_switched_off():
    assert sheets_cell("=1+1", escape_formulas=False) == "=1+1"


def test_export_rows_writes_utf8_with_quoted_newlines_and_column_order(tmp_path):
    path = tmp_path / "o.csv"
    rows = [{"name": "José", "note": "line1\nline2", "n": 1}, {"name": "=evil()", "extra": "x"}]
    count = export_rows(rows, path, columns=["n", "name", "note"])
    assert count == 2
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    parsed = list(csv.reader(path.open(encoding="utf-8", newline="")))
    assert parsed == [["n", "name", "note"], ["1", "José", "line1\nline2"], ["", "'=evil()", ""]]


def test_export_rows_default_columns_are_first_seen_order(tmp_path):
    path = tmp_path / "o.csv"
    export_rows([{"b": 1}, {"a": 2, "b": 3}], path)
    assert path.read_text(encoding="utf-8").splitlines()[0] == "b,a"


def test_cli_reads_jsonl_and_csv(tmp_path):
    src = tmp_path / "in.jsonl"
    src.write_text('{"id": 1, "ok": true}\n\n{"id": 2, "ok": false}\n', encoding="utf-8")
    out = tmp_path / "o.csv"
    assert sheets_export.main([str(src), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").splitlines() == ["id,ok", "1,TRUE", "2,FALSE"]
    csv_in = tmp_path / "in.csv"
    csv_in.write_text("a\n=1+1\n", encoding="utf-8")
    assert sheets_export.main([str(csv_in), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").splitlines() == ["a", "'=1+1"]
    assert sheets_export.main([str(csv_in), "--out", str(out), "--no-escape-formulas"]) == 0
    assert out.read_text(encoding="utf-8").splitlines() == ["a", "=1+1"]


def test_cli_reports_unreadable_input(tmp_path):
    assert sheets_export.main([str(tmp_path / "missing.csv"), "--out", str(tmp_path / "o.csv")]) == 2
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json}\n", encoding="utf-8")
    assert sheets_export.main([str(bad), "--out", str(tmp_path / "o.csv")]) == 2
