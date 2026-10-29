"""sheets_export: write rows as a CSV that is safe to import into Google Sheets or Excel.

Safe here means: UTF-8, ISO dates left untouched, booleans as TRUE/FALSE, nested values as JSON
text, and cells that would be read as a formula (starting with = + - @) get a leading
apostrophe so a hostile or accidental "=HYPERLINK(...)" is stored as text.

NOT verified against Google Sheets itself: the tests only check what this file writes. How
Sheets displays the apostrophe on import is unconfirmed; see the README.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
NUMBER_RE = re.compile(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)")


def sheets_cell(value: Any, escape_formulas: bool = True) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    if escape_formulas and text.startswith(FORMULA_START) and not NUMBER_RE.fullmatch(text):
        return "'" + text
    return text


def export_rows(
    rows: Iterable[dict[str, Any]],
    path: str | Path,
    columns: list[str] | None = None,
    escape_formulas: bool = True,
) -> int:
    """Write rows to `path`. Returns the number of data rows written."""
    rows = list(rows)
    if columns is None:
        columns = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
    count = 0
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([sheets_cell(row.get(c), escape_formulas) for c in columns])
            count += 1
    return count


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sheets_export", description="Make a Sheets-import-friendly CSV.")
    p.add_argument("input", help=".csv or .jsonl")
    p.add_argument("--out", required=True)
    p.add_argument("--columns", help="comma separated column order (default: all, first-seen order)")
    p.add_argument("--no-escape-formulas", action="store_true")
    args = p.parse_args(argv)
    try:
        rows = _read_rows(Path(args.input))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    columns = [c.strip() for c in args.columns.split(",")] if args.columns else None
    written = export_rows(rows, args.out, columns, escape_formulas=not args.no_escape_formulas)
    print(json.dumps({"rows": written, "out": args.out}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
