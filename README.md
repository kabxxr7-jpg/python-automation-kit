# python-automation-kit

> **Status:** a proof project (138 tests, run locally against a local test server). Not used in production or by any client. See Limits below for what was not verified.

Three small command-line tools for the common "clean this file / pull that API / get it into a sheet" job, with a test suite. Written to be read and adapted, not installed as a product.

| Tool | Does | Run |
|---|---|---|
| `csv_clean` | Cleans a CSV or XLSX against a YAML/JSON schema; writes a clean file and an error report | `python -m automation_kit.csv_clean` |
| `api_pull` | Pulls a paginated JSON API (cursor or page number) into JSONL/CSV with retries, rate ceiling and a resumable checkpoint | `python -m automation_kit.api_pull` |
| `sheets_export` | Writes a CSV that is safe to import into Google Sheets or Excel | `python -m automation_kit.sheets_export` |

Only `openpyxl` and `PyYAML` are needed at run time (`api_pull` uses the standard library's `urllib`).

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## csv_clean

```bash
python -m automation_kit.csv_clean examples/customers.csv \
  --schema examples/schema.yaml --out clean.csv --errors errors.csv
```

`examples/customers.csv` is a small made-up file (8 records). Result: 3 clean rows, 4 rejected rows, 1 duplicate dropped, exit code 1. The error report looks like this:

```
row,column,value,reason,level
4,email,alan@example,does not match the required pattern,error
5,signup_date,2024-02-30,"not a valid date (tried %Y-%m-%d, %d/%m/%Y)",error
6,full_name,,required value is missing,error
7,customer_id,,duplicate of row 2 (customer_id=101),duplicate
9,plan,gold,'gold' is not one of the allowed values,error
```

**Rows** are numbered the way a spreadsheet shows them: the header is row 1, the first data row is row 2. A CSV record with a line break inside quotes counts as one row.

**Exit codes:** `0` nothing rejected · `1` at least one row rejected (`--strict` also fails on dropped duplicates) · `2` the file or schema is unusable.

**Schema** (see `examples/schema.yaml`). Per column: `name`, `aliases`, `type` (`string`, `integer`, `number`, `boolean`, `date`), `required`, `trim`, `case` (`lower`/`upper`/`title`), `allowed`, `pattern` (must match the whole value), `max_length`, `min`/`max`, `date_formats` (tried in order, first match wins), `date_output`, `default`, `decimal`/`thousands`/`decimals` for numbers. Top level: `dedupe` (`key`, `keep: first|last`, `action: drop|flag`), `extra_columns` (`keep|drop|error`), `empty_values` (extra strings to treat as empty, e.g. `n/a`).

Behaviours worth knowing:
- A row with any error is rejected whole; every error in it is listed. Rejected rows do not take part in duplicate detection.
- Rows with an empty duplicate key are never treated as duplicates.
- Dates are checked as real calendar dates. `03/05/2024` is ambiguous: with `["%d/%m/%Y"]` first it is 3 May. Put the format you trust first.
- Numbers are handled as exact decimals and rounded half-up (`2.665` with `decimals: 2` gives `2.67`).
- Encodings: UTF-8 (with or without BOM) and UTF-16 with BOM are detected. If the file is not valid UTF-8 it is decoded line by line (UTF-8, then Windows-1252, then Latin-1), which handles files that mix encodings. A line that is a valid UTF-8 sequence but was meant as Windows-1252 cannot be told apart. The encoding used is printed in the summary. `--encoding` forces one.
- Output files are UTF-8 with `\n` line endings. XLSX output writes every cell as text.
- XLSX: first sheet unless `--sheet`; row 1 is the header; columns with no header are ignored; legacy `.xls` is refused.

## api_pull

```bash
# cursor style: {"data": [...], "meta": {"next": "<cursor>"}}
export API_AUTH="Bearer <token>"
python -m automation_kit.api_pull https://api.example.com/v1/items \
  --out items.jsonl --items-path data --next-cursor-path meta.next --cursor-param cursor \
  --header-env Authorization=API_AUTH --checkpoint items.checkpoint.json --rps 5

# page-number style
python -m automation_kit.api_pull https://api.example.com/v1/items --style page \
  --out items.csv --items-path results --page-param page --page-size-param per_page --page-size 100
```

- **Retries:** 429, 500, 502, 503, 504 and connection errors, up to `--max-retries` (5). Wait is `backoff-base × 2^attempt`, capped at `--backoff-max`. A `Retry-After` header (seconds or HTTP date) is used instead of the backoff; if it asks for more than `--max-retry-after` the run stops rather than waiting. Other 4xx fail at once. Redirects are never followed (so an `Authorization` header cannot be sent to another host).
- **Rate ceiling:** `--rps` caps request starts per second and applies to retries too.
- **Checkpoint:** after every page the output is flushed and a checkpoint is written (atomically) with the next cursor/page and the output size. On rerun the output is cut back to that size, so a page that was half-written at the crash is fetched again and nothing is duplicated or skipped. A finished checkpoint makes a rerun do nothing; `--fresh` starts over. A checkpoint from a different job (other URL, params or output) is refused. Without `--checkpoint` every run starts from scratch and overwrites the output.
- **Secrets:** use `--header-env NAME=ENV_VAR` so a token stays out of the command line, shell history and process list. Headers are not written to the checkpoint and are not printed.
- **Output:** JSONL keeps each item as is. CSV uses `--fields` or the keys of the first item; nested values become JSON text; keys that appear later but not in the header are dropped and counted in `extra_keys_ignored`.
- **Exit codes:** `0` finished · `1` API failure after retries · `2` bad configuration.

## sheets_export

```bash
python -m automation_kit.sheets_export clean.csv --out for_sheets.csv
```

Booleans become `TRUE`/`FALSE`, nested values become JSON text, and any cell starting with `=`, `+`, `-` or `@` (other than plain numbers) gets a leading apostrophe so it is not read as a formula (`--no-escape-formulas` turns that off).

**Not tested against Google.** The tests only check what the file contains. How Sheets shows the apostrophe after import is unconfirmed. This Apps Script snippet is also untested:

```javascript
// UNTESTED. Replace the URL; Apps Script needs permission to fetch it.
function importCsv() {
  const csv = UrlFetchApp.fetch('https://example.com/export.csv').getContentText();
  const rows = Utilities.parseCsv(csv);
  const sheet = SpreadsheetApp.getActiveSheet();
  sheet.clear();
  sheet.getRange(1, 1, rows.length, rows[0].length).setValues(rows);
}
```

## Tests

`pytest` runs 138 tests in about a second, with no network access beyond a local server on `127.0.0.1`:

- **csv_clean:** every value rule, bad dates, missing required values, duplicates (first/last/flag, empty keys, rejected rows), UTF-8 BOM, Windows-1252, mixed encodings, UTF-16, XLSX row numbers, exit codes, bad schemas.
- **api_pull:** both pagination styles; 429 then 200; 500 then 200; `Retry-After` in seconds and as a date; backoff and its cap; limited retries; no retry on 4xx; no redirects; connection errors; the rate ceiling (including on retries) using a fake clock; crash between output and checkpoint, API failure then resume, finished checkpoint, fresh start, mismatched checkpoint; a CLI run that checks the token is sent but never printed.
- **sheets_export:** formula escaping, booleans, nesting, column order.

While building, the code was deliberately broken in 14 ways (for example: not truncating on resume, ignoring `Retry-After`, following redirects, no backoff, no rate ceiling, wrong rounding mode, loose date parsing). Two slipped past the first version of the tests; cases were added and all 14 are now caught.

## Limits (read before relying on it)

- **Never run against a real third-party API or a real Google Sheet.** The HTTP behaviour is tested against a local stand-in server that I scripted, which only proves the code does what I expected, not that any particular provider behaves that way. APIs with other pagination styles (Link headers, GraphQL, POST bodies, OAuth flows) are not covered.
- **No production use.** Single process, no locking: two runs against the same output and checkpoint will corrupt each other. Everything is read into memory for `csv_clean` (fine for files that fit in RAM, not for multi-GB files).
- Rate limiting is a simple minimum gap between requests, not a token bucket, and does not read rate-limit headers other than `Retry-After`.
- Tested on Python 3.11.14 (macOS) only.
- Sample data is made up.
