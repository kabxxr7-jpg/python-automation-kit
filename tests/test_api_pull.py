import csv
import json
import os
import subprocess
import sys
from email.utils import formatdate
from pathlib import Path

import pytest

from automation_kit import api_pull
from automation_kit.api_pull import ApiError, ConfigError, PullConfig, pull

ROOT = Path(__file__).resolve().parents[1]
TOTAL = 12


def item(n):
    return {"id": n, "name": f"item {n}", "tags": ["a", "b"] if n % 2 else [], "meta": {"n": n}}


def cursor_api(page_size=5, total=TOTAL):
    """Cursor style: {"data": [...], "meta": {"next": "c5"}}; the cursor is the offset."""
    def respond(req):
        start = int(req.query.get("cursor", "c0")[1:])
        rows = [item(n) for n in range(start + 1, min(start + page_size, total) + 1)]
        nxt = f"c{start + page_size}" if start + page_size < total else None
        return 200, {}, {"data": rows, "meta": {"next": nxt}}
    return respond


def page_api(page_size=5, total=TOTAL, with_total_pages=False):
    def respond(req):
        page = int(req.query["page"])
        start = (page - 1) * page_size
        rows = [item(n) for n in range(start + 1, min(start + page_size, total) + 1)]
        body = {"results": rows}
        if with_total_pages:
            body["total_pages"] = -(-total // page_size)
        return 200, {}, body
    return respond


def cursor_cfg(server, tmp_path, **kw):
    base = dict(url=server.url, out=tmp_path / "out.jsonl", style="cursor", items_path="data",
                next_cursor_path="meta.next", rps=0, backoff_base=0.5)
    base.update(kw)
    return PullConfig(**base)


def lines(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------- pagination


def test_cursor_pagination_collects_every_item_in_order(make_server, tmp_path, fake):
    server = make_server(cursor_api())
    summary = pull(cursor_cfg(server, tmp_path), runtime=fake[1])
    assert [r["id"] for r in lines(tmp_path / "out.jsonl")] == list(range(1, TOTAL + 1))
    assert summary["pages"] == 3 and summary["items"] == TOTAL and summary["requests"] == 3
    assert [r.query.get("cursor") for r in server.requests] == [None, "c5", "c10"]


def test_extra_params_and_page_size_are_sent(make_server, tmp_path, fake):
    server = make_server(cursor_api())
    pull(cursor_cfg(server, tmp_path, params={"status": "open"}, page_size_param="limit", page_size=5), runtime=fake[1])
    assert all(r.query["status"] == "open" and r.query["limit"] == "5" for r in server.requests)


def test_page_number_pagination_stops_on_short_page(make_server, tmp_path, fake):
    server = make_server(page_api())
    cfg = PullConfig(url=server.url, out=tmp_path / "out.jsonl", style="page", items_path="results",
                     page_size_param="per_page", page_size=5, rps=0)
    summary = pull(cfg, runtime=fake[1])
    assert summary["items"] == TOTAL and summary["pages"] == 3
    assert [r.query["page"] for r in server.requests] == ["1", "2", "3"]


def test_page_number_pagination_with_total_pages_and_exact_multiple(make_server, tmp_path, fake):
    server = make_server(page_api(total=10, with_total_pages=True))
    cfg = PullConfig(url=server.url, out=tmp_path / "out.jsonl", style="page", items_path="results",
                     total_pages_path="total_pages", rps=0)
    summary = pull(cfg, runtime=fake[1])
    assert summary["items"] == 10 and len(server.requests) == 2  # stops at total_pages, no empty extra request


def test_page_number_pagination_stops_on_empty_page(make_server, tmp_path, fake):
    server = make_server(page_api(total=10))  # no page_size, no total: only an empty page ends it
    cfg = PullConfig(url=server.url, out=tmp_path / "out.jsonl", style="page", items_path="results", rps=0)
    summary = pull(cfg, runtime=fake[1])
    assert summary["items"] == 10 and len(server.requests) == 3  # pages 1, 2 and the empty page 3


def test_response_that_is_a_bare_list(make_server, tmp_path, fake):
    def respond(req):
        return 200, {}, [item(1), item(2)] if req.query.get("page") == "1" else []
    server = make_server(respond)
    cfg = PullConfig(url=server.url, out=tmp_path / "o.jsonl", style="page", rps=0)
    assert pull(cfg, runtime=fake[1])["items"] == 2


def test_cursor_that_does_not_advance_is_an_error(make_server, tmp_path, fake):
    server = make_server(lambda req: (200, {}, {"data": [item(1)], "meta": {"next": "same"}}))
    with pytest.raises(ApiError, match="same cursor"):
        pull(cursor_cfg(server, tmp_path, params={"cursor": "same"}), runtime=fake[1])


def test_items_that_are_not_a_list_are_an_error(make_server, tmp_path, fake):
    server = make_server(lambda req: (200, {}, {"data": {"oops": 1}, "meta": {}}))
    with pytest.raises(ApiError, match="not a JSON list"):
        pull(cursor_cfg(server, tmp_path), runtime=fake[1])


def test_invalid_json_is_not_retried(make_server, tmp_path, fake):
    server = make_server(lambda req: (200, {}, "<html>oops</html>"))
    with pytest.raises(ApiError, match="not valid JSON"):
        pull(cursor_cfg(server, tmp_path), runtime=fake[1])
    assert len(server.requests) == 1


# ------------------------------------------------------------------ retries / backoff


def flaky(failures, status=500, headers=None, inner=None):
    """Fail the first `failures` requests with `status`, then behave like the real API."""
    inner = inner or cursor_api()
    def respond(req):
        if req.index < failures:
            return status, headers or {}, {"error": "try later"}
        return inner(req)
    return respond


def test_429_with_retry_after_seconds_is_honoured(make_server, tmp_path, fake):
    clock, runtime = fake
    server = make_server(flaky(1, 429, {"Retry-After": "2"}))
    summary = pull(cursor_cfg(server, tmp_path), runtime=runtime)
    assert clock.sleeps == [2.0]  # waited exactly what the server asked, not the 0.5s backoff
    assert summary["items"] == TOTAL and summary["retries"] == 1 and summary["requests"] == 4


def test_retry_after_http_date(make_server, tmp_path, fake):
    clock, runtime = fake
    when = formatdate(clock.now + 7, usegmt=True)
    server = make_server(flaky(1, 503, {"Retry-After": when}))
    pull(cursor_cfg(server, tmp_path), runtime=runtime)
    assert clock.sleeps == [7.0]


def test_retry_after_longer_than_the_cap_stops_instead_of_waiting(make_server, tmp_path, fake):
    clock, runtime = fake
    server = make_server(flaky(5, 429, {"Retry-After": "3600"}))
    with pytest.raises(ApiError, match="max_retry_after"):
        pull(cursor_cfg(server, tmp_path), runtime=runtime)
    assert clock.sleeps == [] and len(server.requests) == 1


def test_500_then_200_uses_exponential_backoff(make_server, tmp_path, fake):
    clock, runtime = fake
    server = make_server(flaky(3, 500))
    summary = pull(cursor_cfg(server, tmp_path, backoff_base=0.5), runtime=runtime)
    assert clock.sleeps == [0.5, 1.0, 2.0]
    assert summary["items"] == TOTAL


def test_backoff_is_capped(make_server, tmp_path, fake):
    clock, runtime = fake
    server = make_server(flaky(4, 502))
    pull(cursor_cfg(server, tmp_path, backoff_base=1, backoff_max=3), runtime=runtime)
    assert clock.sleeps == [1, 2, 3, 3]


@pytest.mark.parametrize("status", [502, 503, 504])
def test_other_5xx_are_retried(make_server, tmp_path, fake, status):
    server = make_server(flaky(1, status))
    assert pull(cursor_cfg(server, tmp_path), runtime=fake[1])["retries"] == 1


def test_retries_are_limited(make_server, tmp_path, fake):
    clock, runtime = fake
    server = make_server(flaky(99, 500))
    with pytest.raises(ApiError, match="giving up after 4 attempts"):
        pull(cursor_cfg(server, tmp_path, max_retries=3), runtime=runtime)
    assert len(server.requests) == 4 and clock.sleeps == [0.5, 1.0, 2.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_non_retryable_4xx_fails_immediately(make_server, tmp_path, fake, status):
    clock, runtime = fake
    server = make_server(lambda req: (status, {}, {"error": "no"}))
    with pytest.raises(ApiError) as info:
        pull(cursor_cfg(server, tmp_path), runtime=runtime)
    assert info.value.status == status and len(server.requests) == 1 and clock.sleeps == []


def test_redirects_are_not_followed(make_server, tmp_path, fake):
    server = make_server(lambda req: (302, {"Location": "http://127.0.0.1:1/elsewhere"}, {}))
    with pytest.raises(ApiError) as info:
        pull(cursor_cfg(server, tmp_path), runtime=fake[1])
    assert info.value.status == 302


def test_connection_errors_are_retried_then_reported(tmp_path, fake):
    clock, runtime = fake
    cfg = PullConfig(url="http://127.0.0.1:9/items", out=tmp_path / "o.jsonl", next_cursor_path="x",
                     rps=0, max_retries=2, timeout=2)
    with pytest.raises(ApiError, match="giving up after 3 attempts"):
        pull(cfg, runtime=runtime)
    assert clock.sleeps == [0.5, 1.0]


# ----------------------------------------------------------------------- rate limit


def test_rate_ceiling_spaces_requests(make_server, tmp_path, fake):
    clock, runtime = fake
    server = make_server(cursor_api())
    pull(cursor_cfg(server, tmp_path, rps=2), runtime=runtime)
    assert clock.sleeps == [0.5, 0.5]  # three requests at 2/s: nothing before the first


def test_rate_ceiling_also_applies_to_retries(make_server, tmp_path, fake):
    clock, runtime = fake
    started = []
    inner = flaky(1, 500)

    def respond(req):
        started.append(clock.now)  # fake-clock time at which this request arrived
        return inner(req)

    server = make_server(respond)
    pull(cursor_cfg(server, tmp_path, rps=2, backoff_base=0.1), runtime=runtime)
    gaps = [round(b - a, 6) for a, b in zip(started, started[1:])]
    assert len(started) == 4  # the failed attempt, its retry, then pages 2 and 3
    assert gaps == [0.5, 0.5, 0.5]  # a 0.1s backoff is topped up so even the retry respects 2 requests/second


def test_unlimited_rate_never_sleeps(make_server, tmp_path, fake):
    clock, runtime = fake
    server = make_server(cursor_api())
    pull(cursor_cfg(server, tmp_path, rps=0), runtime=runtime)
    assert clock.sleeps == []


# ------------------------------------------------------------------------ CSV output


def test_csv_output_infers_fields_flattens_nested_and_counts_extras(make_server, tmp_path, fake):
    def respond(req):
        if req.query.get("page") == "1":
            return 200, {}, {"results": [{"id": 1, "name": "a", "tags": ["x", "y"], "ok": True, "gone": None}]}
        if req.query.get("page") == "2":
            return 200, {}, {"results": [{"id": 2, "name": "b", "new_key": "ignored"}]}
        return 200, {}, {"results": []}
    server = make_server(respond)
    cfg = PullConfig(url=server.url, out=tmp_path / "o.csv", format="csv", style="page", items_path="results", rps=0)
    summary = pull(cfg, runtime=fake[1])
    rows = list(csv.reader((tmp_path / "o.csv").open(encoding="utf-8")))
    assert rows[0] == ["id", "name", "tags", "ok", "gone"]
    assert rows[1] == ["1", "a", '["x", "y"]', "true", ""]
    assert rows[2] == ["2", "b", "", "", ""]
    assert summary["extra_keys_ignored"] == 1


def test_csv_output_with_explicit_fields(make_server, tmp_path, fake):
    server = make_server(cursor_api())
    pull(cursor_cfg(server, tmp_path, out=tmp_path / "o.csv", format="csv", fields=["name", "id"]), runtime=fake[1])
    rows = list(csv.reader((tmp_path / "o.csv").open(encoding="utf-8")))
    assert rows[0] == ["name", "id"] and rows[1] == ["item 1", "1"] and len(rows) == TOTAL + 1


# ----------------------------------------------------------------------- checkpoint


def crash_on_checkpoint(monkeypatch, nth):
    """Make the nth checkpoint save fail, simulating a crash right after a page was written."""
    real = api_pull._save_checkpoint
    calls = {"n": 0}

    def flaky_save(path, state):
        calls["n"] += 1
        if calls["n"] == nth:
            raise RuntimeError("simulated crash")
        real(path, state)

    monkeypatch.setattr(api_pull, "_save_checkpoint", flaky_save)


def test_resume_after_crash_between_output_and_checkpoint(make_server, tmp_path, fake, monkeypatch):
    server = make_server(cursor_api())
    cp = tmp_path / "cp.json"
    crash_on_checkpoint(monkeypatch, nth=3)  # page 3 is written, its checkpoint never lands
    with pytest.raises(RuntimeError, match="simulated crash"):
        pull(cursor_cfg(server, tmp_path, checkpoint=cp), runtime=fake[1])
    assert len(lines(tmp_path / "out.jsonl")) == TOTAL  # the output already has page 3...
    assert json.loads(cp.read_text())["pages_done"] == 2  # ...but the checkpoint says only 2 pages are done
    monkeypatch.undo()

    before = len(server.requests)
    summary = pull(cursor_cfg(server, tmp_path, checkpoint=cp), runtime=fake[1])
    ids = [r["id"] for r in lines(tmp_path / "out.jsonl")]
    assert ids == list(range(1, TOTAL + 1))  # every item exactly once, no gap, no duplicate
    assert [r.query["cursor"] for r in server.requests[before:]] == ["c10"]  # only the unfinished page was refetched
    assert summary["resumed"] is True and summary["items"] == TOTAL
    assert json.loads(cp.read_text())["done"] is True


def test_resume_after_api_failure_continues_from_the_failed_page(make_server, tmp_path, fake):
    healthy = {"on": False}
    inner = cursor_api()

    def respond(req):
        if req.query.get("cursor") == "c5" and not healthy["on"]:
            return 500, {}, {}
        return inner(req)

    server = make_server(respond)
    cp = tmp_path / "cp.json"
    with pytest.raises(ApiError):
        pull(cursor_cfg(server, tmp_path, checkpoint=cp, max_retries=1), runtime=fake[1])
    assert len(lines(tmp_path / "out.jsonl")) == 5
    healthy["on"] = True
    before = len(server.requests)
    summary = pull(cursor_cfg(server, tmp_path, checkpoint=cp), runtime=fake[1])
    assert [r["id"] for r in lines(tmp_path / "out.jsonl")] == list(range(1, TOTAL + 1))
    assert [r.query["cursor"] for r in server.requests[before:]] == ["c5", "c10"]
    assert summary["pages"] == 3


def test_resume_with_page_style_and_csv_keeps_a_single_header(make_server, tmp_path, fake, monkeypatch):
    server = make_server(page_api())
    cp = tmp_path / "cp.json"
    cfg = PullConfig(url=server.url, out=tmp_path / "o.csv", format="csv", style="page", items_path="results",
                     page_size_param="per_page", page_size=5, rps=0, checkpoint=cp)
    crash_on_checkpoint(monkeypatch, nth=2)
    with pytest.raises(RuntimeError):
        pull(cfg, runtime=fake[1])
    monkeypatch.undo()
    pull(cfg, runtime=fake[1])
    rows = list(csv.reader((tmp_path / "o.csv").open(encoding="utf-8")))
    assert rows[0] == ["id", "name", "tags", "meta"]
    assert [r[0] for r in rows[1:]] == [str(n) for n in range(1, TOTAL + 1)]
    assert sum(1 for r in rows if r[0] == "id") == 1


def test_finished_checkpoint_makes_no_requests(make_server, tmp_path, fake):
    server = make_server(cursor_api())
    cp = tmp_path / "cp.json"
    pull(cursor_cfg(server, tmp_path, checkpoint=cp), runtime=fake[1])
    before = len(server.requests)
    again = pull(cursor_cfg(server, tmp_path, checkpoint=cp), runtime=fake[1])
    assert len(server.requests) == before and again["already_complete"] is True
    assert len(lines(tmp_path / "out.jsonl")) == TOTAL


def test_fresh_starts_over(make_server, tmp_path, fake):
    server = make_server(cursor_api())
    cp = tmp_path / "cp.json"
    pull(cursor_cfg(server, tmp_path, checkpoint=cp), runtime=fake[1])
    before = len(server.requests)
    summary = pull(cursor_cfg(server, tmp_path, checkpoint=cp), runtime=fake[1], fresh=True)
    assert len(server.requests) == before + 3 and summary["resumed"] is False
    assert len(lines(tmp_path / "out.jsonl")) == TOTAL  # overwritten, not appended


def test_checkpoint_of_a_different_job_is_refused(make_server, tmp_path, fake):
    server = make_server(cursor_api())
    cp = tmp_path / "cp.json"
    pull(cursor_cfg(server, tmp_path, checkpoint=cp), runtime=fake[1])
    with pytest.raises(ConfigError, match="different pull"):
        pull(cursor_cfg(server, tmp_path, checkpoint=cp, params={"status": "closed"}), runtime=fake[1])


def test_truncated_output_is_detected(make_server, tmp_path, fake, monkeypatch):
    server = make_server(cursor_api())
    cp = tmp_path / "cp.json"
    crash_on_checkpoint(monkeypatch, nth=3)
    with pytest.raises(RuntimeError):
        pull(cursor_cfg(server, tmp_path, checkpoint=cp), runtime=fake[1])
    monkeypatch.undo()
    (tmp_path / "out.jsonl").write_bytes(b"")  # someone emptied the output file
    with pytest.raises(ConfigError, match="shorter than the checkpoint"):
        pull(cursor_cfg(server, tmp_path, checkpoint=cp), runtime=fake[1])


def test_corrupt_checkpoint_is_reported(make_server, tmp_path, fake):
    server = make_server(cursor_api())
    cp = tmp_path / "cp.json"
    cp.write_text("{broken")
    with pytest.raises(ConfigError, match="unreadable"):
        pull(cursor_cfg(server, tmp_path, checkpoint=cp), runtime=fake[1])


def test_max_pages_stops_and_a_larger_limit_continues(make_server, tmp_path, fake):
    server = make_server(cursor_api())
    cp = tmp_path / "cp.json"
    with pytest.raises(ApiError, match="max_pages"):
        pull(cursor_cfg(server, tmp_path, checkpoint=cp, max_pages=2), runtime=fake[1])
    assert len(lines(tmp_path / "out.jsonl")) == 10
    pull(cursor_cfg(server, tmp_path, checkpoint=cp, max_pages=10), runtime=fake[1])
    assert [r["id"] for r in lines(tmp_path / "out.jsonl")] == list(range(1, TOTAL + 1))


def test_checkpoint_never_contains_headers_or_secrets(make_server, tmp_path, fake):
    server = make_server(cursor_api())
    cp = tmp_path / "cp.json"
    pull(cursor_cfg(server, tmp_path, checkpoint=cp, headers={"Authorization": "Bearer TOP-SECRET"}), runtime=fake[1])
    assert "TOP-SECRET" not in cp.read_text()


# ------------------------------------------------------------------------------ CLI


def test_cli_end_to_end_with_header_from_env(make_server, tmp_path):
    server = make_server(cursor_api())
    env = {**os.environ, "NO_PROXY": "127.0.0.1", "MY_TOKEN": "Bearer s3cret-value"}
    out = tmp_path / "cli.jsonl"
    proc = subprocess.run(
        [sys.executable, "-m", "automation_kit.api_pull", server.url, "--out", str(out), "--items-path", "data",
         "--next-cursor-path", "meta.next", "--header-env", "Authorization=MY_TOKEN", "--header", "X-Api-Version: 2",
         "--rps", "0", "--checkpoint", str(tmp_path / "cp.json")],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["items"] == TOTAL
    assert [r["id"] for r in lines(out)] == list(range(1, TOTAL + 1))
    assert server.requests[0].headers["authorization"] == "Bearer s3cret-value"
    assert server.requests[0].headers["x-api-version"] == "2"
    assert "s3cret-value" not in proc.stdout + proc.stderr  # the secret is never printed


def test_cli_exit_codes(make_server, tmp_path):
    server = make_server(lambda req: (404, {}, {}))
    env = {**os.environ, "NO_PROXY": "127.0.0.1"}
    base = [sys.executable, "-m", "automation_kit.api_pull", server.url, "--out", str(tmp_path / "o.jsonl"),
            "--next-cursor-path", "next", "--rps", "0"]
    assert subprocess.run(base, cwd=ROOT, env=env, capture_output=True).returncode == 1  # API error
    no_cursor = [sys.executable, "-m", "automation_kit.api_pull", server.url, "--out", str(tmp_path / "o.jsonl")]
    assert subprocess.run(no_cursor, cwd=ROOT, env=env, capture_output=True).returncode == 2  # config error
    missing_env = base + ["--header-env", "Authorization=DOES_NOT_EXIST_123"]
    assert subprocess.run(missing_env, cwd=ROOT, env=env, capture_output=True).returncode == 2
