"""api_pull: pull a paginated JSON API into JSONL or CSV.

* Cursor and page-number pagination.
* Retries with exponential backoff on 429/500/502/503/504 and on connection errors;
  a Retry-After header (seconds or HTTP date) is honoured.
* A request-rate ceiling that also applies to retries.
* A checkpoint file so a crash resumes instead of restarting. The checkpoint records how many
  bytes of output belong to completed pages; on resume the output is cut back to that size, so
  a page that was half-written when the process died is fetched again and nothing is duplicated.

Exit codes: 0 = finished, 1 = the API failed (retries exhausted or a non-retryable status),
2 = bad configuration.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
CHECKPOINT_VERSION = 1


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ConfigError(Exception):
    pass


@dataclass
class Runtime:
    """Clock and sleep, injectable so tests do not have to wait."""

    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time


@dataclass
class PullConfig:
    url: str
    out: Path
    style: str = "cursor"  # "cursor" or "page"
    format: str = "jsonl"  # "jsonl" or "csv"
    params: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    items_path: str | None = None
    fields: list[str] | None = None
    # cursor style
    cursor_param: str = "cursor"
    next_cursor_path: str | None = None
    # page style
    page_param: str = "page"
    start_page: int = 1
    total_pages_path: str | None = None
    # both
    page_size_param: str | None = None
    page_size: int | None = None
    # reliability
    rps: float = 5.0
    max_retries: int = 5
    backoff_base: float = 0.5
    backoff_max: float = 30.0
    max_retry_after: float = 300.0
    timeout: float = 30.0
    max_pages: int = 10_000
    checkpoint: Path | None = None

    def validate(self) -> None:
        if self.style not in {"cursor", "page"}:
            raise ConfigError("style must be 'cursor' or 'page'")
        if self.format not in {"jsonl", "csv"}:
            raise ConfigError("format must be 'jsonl' or 'csv'")
        if self.style == "cursor" and not self.next_cursor_path:
            raise ConfigError("cursor style needs next_cursor_path (where the next cursor is in the response)")
        if not urllib.parse.urlparse(self.url).scheme in {"http", "https"}:
            raise ConfigError("url must start with http:// or https://")
        if self.max_retries < 0 or self.rps < 0 or self.backoff_base < 0:
            raise ConfigError("max_retries, rps and backoff_base must not be negative")

    def fingerprint(self) -> str:
        """Identifies the job a checkpoint belongs to. Headers (secrets) are deliberately excluded."""
        identity = {
            "url": self.url, "params": self.params, "style": self.style, "format": self.format,
            "items_path": self.items_path, "fields": self.fields, "cursor_param": self.cursor_param,
            "next_cursor_path": self.next_cursor_path, "page_param": self.page_param,
            "start_page": self.start_page, "total_pages_path": self.total_pages_path,
            "page_size_param": self.page_size_param, "page_size": self.page_size,
            "out": str(self.out.resolve()),
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------- helpers


def get_path(obj: Any, path: str | None) -> Any:
    """Follow a dotted path through nested dicts. Returns None if any step is missing."""
    if not path:
        return obj
    for part in path.split("."):
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            return None
    return obj


def build_url(url: str, params: dict[str, Any]) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query += [(k, str(v)) for k, v in params.items()]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: they could carry the Authorization header to another host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Pacer:
    """Keeps at least 1/rps seconds between the start of consecutive requests."""

    def __init__(self, rps: float, runtime: Runtime) -> None:
        self.interval = 1.0 / rps if rps > 0 else 0.0
        self.runtime = runtime
        self._last: float | None = None

    def wait(self) -> None:
        if self.interval:
            now = self.runtime.monotonic()
            if self._last is not None:
                delay = self._last + self.interval - now
                if delay > 0:
                    self.runtime.sleep(delay)
                    now = self.runtime.monotonic()
            self._last = now
        else:
            self._last = self.runtime.monotonic()


class Client:
    def __init__(self, config: PullConfig, runtime: Runtime) -> None:
        self.config = config
        self.runtime = runtime
        self.pacer = Pacer(config.rps, runtime)
        self.opener = urllib.request.build_opener(_NoRedirect)
        self.requests = 0
        self.retries = 0

    def _http_get(self, url: str) -> tuple[int | None, dict[str, str], bytes, str]:
        headers = {"Accept": "application/json", "User-Agent": "automation-kit/0.1", **self.config.headers}
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self.opener.open(request, timeout=self.config.timeout) as response:
                return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read(), ""
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            return exc.code, {k.lower(): v for k, v in exc.headers.items()}, body, ""
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            return None, {}, b"", type(exc).__name__

    def _retry_after_seconds(self, headers: dict[str, str]) -> float | None:
        value = headers.get("retry-after")
        if value is None:
            return None
        value = value.strip()
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError):
            return None
        return max(0.0, when - self.runtime.wall())

    def get_json(self, params: dict[str, Any]) -> Any:
        url = build_url(self.config.url, params)
        attempt = 0
        while True:
            self.pacer.wait()
            self.requests += 1
            status, headers, body, error = self._http_get(url)
            if status is not None and 200 <= status < 300:
                try:
                    return json.loads(body)
                except ValueError as exc:
                    raise ApiError("response was not valid JSON", status) from exc
            if status is not None and status not in RETRYABLE_STATUS:
                raise ApiError(f"HTTP {status}", status)
            if attempt >= self.config.max_retries:
                what = f"HTTP {status}" if status is not None else error
                raise ApiError(f"giving up after {attempt + 1} attempts ({what})", status)
            retry_after = self._retry_after_seconds(headers)
            if retry_after is not None:
                if retry_after > self.config.max_retry_after:
                    raise ApiError(
                        f"server asked to wait {retry_after:.0f}s (more than max_retry_after "
                        f"{self.config.max_retry_after:.0f}s); not retrying",
                        status,
                    )
                wait = retry_after
            else:
                wait = min(self.config.backoff_max, self.config.backoff_base * (2**attempt))
            attempt += 1
            self.retries += 1
            self.runtime.sleep(wait)


# ------------------------------------------------------------------------ checkpoint


def _save_checkpoint(path: Path, state: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _load_checkpoint(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ConfigError(f"checkpoint {path.name} is unreadable; delete it or use --fresh") from exc
    if state.get("version") != CHECKPOINT_VERSION:
        raise ConfigError(f"checkpoint {path.name} has an unknown version; use --fresh")
    return state


# ------------------------------------------------------------------------------ pull


def _encode_page(items: list[dict[str, Any]], config: PullConfig, fields: list[str] | None) -> tuple[bytes, int]:
    """Encode one page. Returns (bytes, number of items carrying keys outside `fields`)."""
    if config.format == "jsonl":
        return b"".join(json.dumps(i, ensure_ascii=False).encode("utf-8") + b"\n" for i in items), 0
    assert fields is not None
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    extra = 0
    for item in items:
        if set(item) - set(fields):
            extra += 1
        writer.writerow([_csv_value(item.get(f)) for f in fields])
    return buffer.getvalue().encode("utf-8"), extra


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def pull(
    config: PullConfig,
    runtime: Runtime | None = None,
    fresh: bool = False,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    config.validate()
    runtime = runtime or Runtime()
    log = log or (lambda _msg: None)
    fingerprint = config.fingerprint()

    state: dict[str, Any] | None = None
    if config.checkpoint is not None:
        if fresh and config.checkpoint.exists():
            config.checkpoint.unlink()
        state = _load_checkpoint(config.checkpoint)
        if state is not None and state.get("fingerprint") != fingerprint:
            raise ConfigError("checkpoint belongs to a different pull (url, params or output changed); use --fresh")
        if state is not None and state.get("done"):
            return {
                "pages": state["pages_done"], "items": state["items_written"], "requests": 0,
                "retries": 0, "resumed": True, "already_complete": True, "extra_keys_ignored": 0,
            }

    resumed = state is not None
    out = config.out
    if resumed:
        assert state is not None
        size = out.stat().st_size if out.exists() else 0
        if size < state["output_bytes"]:
            raise ConfigError("output file is shorter than the checkpoint says; use --fresh to start over")
        with out.open("r+b") as handle:
            handle.truncate(state["output_bytes"])
        offset = state["output_bytes"]
        pages_done = state["pages_done"]
        items_total = state["items_written"]
        fields = state["fields"]
        cursor_or_page = state["next"]
    else:
        offset = pages_done = items_total = 0
        fields = list(config.fields) if config.fields else None
        cursor_or_page = None if config.style == "cursor" else config.start_page

    client = Client(config, runtime)
    extra_keys_ignored = 0
    with out.open("ab" if resumed else "wb") as handle:
        while True:
            if pages_done >= config.max_pages:
                raise ApiError(f"max_pages ({config.max_pages}) reached; raise it and rerun to continue")
            params: dict[str, Any] = dict(config.params)
            if config.page_size_param and config.page_size:
                params[config.page_size_param] = config.page_size
            if config.style == "cursor":
                if cursor_or_page is not None:
                    params[config.cursor_param] = cursor_or_page
            else:
                params[config.page_param] = cursor_or_page
            data = client.get_json(params)

            items = get_path(data, config.items_path)
            if not isinstance(items, list):
                where = config.items_path or "the response"
                raise ApiError(f"{where} is not a JSON list")
            if any(not isinstance(i, dict) for i in items):
                raise ApiError("every item must be a JSON object")
            if config.format == "csv" and fields is None and items:
                fields = list(items[0].keys())

            chunk = b""
            if items:
                if config.format == "csv" and offset == 0:
                    header = io.StringIO()
                    csv.writer(header, lineterminator="\n").writerow(fields)
                    chunk += header.getvalue().encode("utf-8")
                body, extra = _encode_page(items, config, fields)
                chunk += body
                extra_keys_ignored += extra
            if chunk:
                handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
                offset = handle.tell()
            pages_done += 1
            items_total += len(items)
            log(f"page {pages_done}: {len(items)} items")

            finished = False
            next_value: Any = None
            if config.style == "cursor":
                next_value = get_path(data, config.next_cursor_path)
                if next_value in (None, ""):
                    finished = True
                elif next_value == cursor_or_page:
                    raise ApiError("the API returned the same cursor again; stopping to avoid a loop")
            else:
                total_pages = get_path(data, config.total_pages_path)
                if not items:
                    finished = True
                elif isinstance(total_pages, int) and cursor_or_page >= total_pages:
                    finished = True
                elif config.page_size and len(items) < config.page_size:
                    finished = True
                else:
                    next_value = cursor_or_page + 1

            if config.checkpoint is not None:
                _save_checkpoint(config.checkpoint, {
                    "version": CHECKPOINT_VERSION, "fingerprint": fingerprint,
                    "next": None if finished else next_value, "pages_done": pages_done,
                    "items_written": items_total, "output_bytes": offset, "fields": fields,
                    "done": finished,
                })
            if finished:
                break
            cursor_or_page = next_value

    return {
        "pages": pages_done, "items": items_total, "requests": client.requests,
        "retries": client.retries, "resumed": resumed, "already_complete": False,
        "extra_keys_ignored": extra_keys_ignored,
    }


# ----------------------------------------------------------------------------- CLI


def _parse_pairs(values: list[str], sep: str, what: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if sep not in value:
            raise ConfigError(f"{what} must look like NAME{sep}VALUE")
        key, val = value.split(sep, 1)
        result[key.strip()] = val.strip()
    return result


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="api_pull", description="Pull a paginated JSON API into JSONL/CSV.")
    p.add_argument("url")
    p.add_argument("--out", required=True, help="output file (.jsonl or .csv)")
    p.add_argument("--style", choices=["cursor", "page"], default="cursor")
    p.add_argument("--format", choices=["jsonl", "csv"], help="default: from the --out suffix")
    p.add_argument("--items-path", help="dotted path to the list of items (default: the response itself)")
    p.add_argument("--fields", help="CSV columns, comma separated (default: keys of the first item)")
    p.add_argument("--param", action="append", default=[], metavar="KEY=VALUE", help="extra query parameter")
    p.add_argument("--header", action="append", default=[], metavar="'Name: value'")
    p.add_argument("--header-env", action="append", default=[], metavar="Name=ENV_VAR",
                   help="send header Name with the value of an environment variable (keeps secrets off the command line)")
    p.add_argument("--cursor-param", default="cursor")
    p.add_argument("--next-cursor-path", help="dotted path of the next cursor (cursor style)")
    p.add_argument("--page-param", default="page")
    p.add_argument("--start-page", type=int, default=1)
    p.add_argument("--total-pages-path")
    p.add_argument("--page-size-param")
    p.add_argument("--page-size", type=int)
    p.add_argument("--rps", type=float, default=5.0, help="max requests per second (0 = unlimited)")
    p.add_argument("--max-retries", type=int, default=5)
    p.add_argument("--backoff-base", type=float, default=0.5)
    p.add_argument("--backoff-max", type=float, default=30.0)
    p.add_argument("--max-retry-after", type=float, default=300.0)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--max-pages", type=int, default=10_000)
    p.add_argument("--checkpoint", help="checkpoint file; enables resume")
    p.add_argument("--fresh", action="store_true", help="ignore and delete an existing checkpoint")
    p.add_argument("--quiet", action="store_true")
    return p


def config_from_args(args: argparse.Namespace) -> PullConfig:
    headers = _parse_pairs(args.header, ":", "--header")
    for name, var in _parse_pairs(args.header_env, "=", "--header-env").items():
        value = os.environ.get(var)
        if value is None:
            raise ConfigError(f"environment variable {var} is not set")
        headers[name] = value
    out = Path(args.out)
    fmt = args.format or ("csv" if out.suffix.lower() == ".csv" else "jsonl")
    return PullConfig(
        url=args.url, out=out, style=args.style, format=fmt,
        params=_parse_pairs(args.param, "=", "--param"), headers=headers,
        items_path=args.items_path,
        fields=[f.strip() for f in args.fields.split(",")] if args.fields else None,
        cursor_param=args.cursor_param, next_cursor_path=args.next_cursor_path,
        page_param=args.page_param, start_page=args.start_page,
        total_pages_path=args.total_pages_path, page_size_param=args.page_size_param,
        page_size=args.page_size, rps=args.rps, max_retries=args.max_retries,
        backoff_base=args.backoff_base, backoff_max=args.backoff_max,
        max_retry_after=args.max_retry_after, timeout=args.timeout, max_pages=args.max_pages,
        checkpoint=Path(args.checkpoint) if args.checkpoint else None,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = config_from_args(args)
        log = None if args.quiet else (lambda msg: print(msg, file=sys.stderr))
        summary = pull(config, fresh=args.fresh, log=log)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except ApiError as exc:
        print(f"api error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
