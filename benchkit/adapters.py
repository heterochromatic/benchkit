"""Scripted provider runs: call each vendor's API directly, one endpoint configuration at a time.

A bench repo lists its endpoints in providers/__init__.py as ADAPTERS = {endpoint_id: Adapter()}.
Each adapter has:
- name: stable endpoint ID (accepted by `bench run --endpoints`)
- platform: key in config platforms (pricing, label, usage method)
- docs_url, required_env
- enrich(row) -> Result for per-row APIs, or enrich_batch(rows, save, journal) for batch APIs,
  which calls save(input_id, result) as each row finishes and records anything it has paid to
  start (e.g. a routine run ID) in journal[input_id], so a resumed run polls instead of paying again
- balance() -> float | None (optional): credit balance, read before and after the run

Every row's result is written to cells/<input_id>.json as it arrives, so `--resume` never
re-calls (or re-pays for) a finished row. Transient failures (HTTP 429, 5xx, no response) are
retried up to MAX_ATTEMPTS times in total, waiting retry_wait_s between passes (pre-registered in
each bench's methodology). HTTP request/response envelopes go to audit/ with credential headers redacted.
"""
import csv
import json
import os
import statistics
import time
import urllib.error
import urllib.request

from . import paths, runs

REDACTED = "***REDACTED***"
MAX_ATTEMPTS = 3
SECRET_KEYS = ("authorization", "api-key", "apikey", "api_key", "api-token", "api_token", "token", "secret", "password")


class AdapterError(Exception):
    pass


class StopRun(AdapterError):
    """Raised by an adapter when carrying on is pointless (e.g. out of credits). The run stops; resume later."""


def require_env(*names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise AdapterError(f"missing credentials: {', '.join(missing)}")
    return tuple(os.environ[n] for n in names)


def redact_headers(headers):
    return {k: (REDACTED if any(s in k.lower() for s in SECRET_KEYS) else v) for k, v in (headers or {}).items()}


def http_json(method, url, headers=None, body=None, timeout=60):
    """One HTTP call. Returns (status, parsed_json_or_text, latency_ms, audit_envelope)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        status, raw = None, str(e.reason)
    except (TimeoutError, ConnectionError, OSError) as e:
        status, raw = None, f"{type(e).__name__}: {e}"
    latency = round((time.monotonic() - t0) * 1000, 1)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        parsed = raw
    audit = {"request": {"method": method, "url": url, "headers": redact_headers(headers), "body": body},
             "response": {"status": status, "body": parsed}, "latency_ms": latency}
    return status, parsed, latency, audit


def result(fields=None, ok=True, status=None, error=None, latency_ms=None, audit=None):
    return {"fields": fields or {}, "ok": ok, "http_status": status, "error": error,
            "latency_ms": latency_ms, "audit": audit}


class Adapter:
    name = ""
    platform = ""
    docs_url = ""
    required_env = ()
    pause_s = 0.0
    retry_wait_s = 30.0

    def validate(self):
        require_env(*self.required_env)

    def enrich(self, row):
        raise NotImplementedError

    def balance(self):
        return None


def registry():
    """The bench repo's endpoint roster: providers/__init__.py ADAPTERS, plus private/providers/
    (adapters for anonymized vendors) when present."""
    try:
        reg = dict(paths.load_module(paths.repo() / "providers" / "__init__.py", "bench_providers").ADAPTERS)
        priv = paths.private() / "providers" / "__init__.py"
        if priv.exists():
            reg.update(paths.load_module(priv, "bench_private_providers").ADAPTERS)
        return reg
    except paths.PathError as e:
        raise AdapterError(str(e)) from None


def transient(cell):
    """A failure worth retrying: rate limit, server error or no response, under the attempt cap."""
    st = cell.get("http_status")
    return (not cell.get("ok") and (st is None or st == 429 or st >= 500)
            and cell.get("attempts", 1) < MAX_ATTEMPTS and not cell.get("final"))


class Journal(dict):
    """Per-run record of paid-for work in flight (journal.json), written on every change."""

    def __init__(self, path):
        super().__init__(json.loads(path.read_text(encoding="utf-8")) if path.exists() else {})
        self.path = path

    def __setitem__(self, k, v):
        super().__setitem__(k, v)
        paths.write_json(self.path, dict(self))


def _rows(run, cfg):
    t = cfg["tasks"][run["task_id"]]
    with open(runs.run_dir(run["run_id"]) / "input" / t["input"], newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _cells(d):
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (d / "cells").glob("*.json")}


def _save_cell(d, input_id, res):
    prev = d / "cells" / f"{input_id}.json"
    attempts = json.loads(prev.read_text(encoding="utf-8")).get("attempts", 1) + 1 if prev.exists() else 1
    audit = res.pop("audit", None)
    if audit is not None:
        paths.write_json(d / "audit" / f"{input_id}.json", audit)
        res["audit_path"] = f"audit/{input_id}.json"
    paths.write_json(d / "cells" / f"{input_id}.json", {"input_id": input_id, **res, "attempts": attempts})


def run_endpoint(cfg, run_id, limit=0, log=print):
    """Execute (or resume) a scripted run. Writes cells/, audit/ and output/output.csv."""
    run = runs.load(run_id)
    adapter = registry().get(run.get("endpoint"))
    if adapter is None:
        raise AdapterError(f"{run_id} has no known endpoint ({run.get('endpoint')})")
    adapter.validate()
    t = cfg["tasks"][run["task_id"]]
    d = runs.run_dir(run_id)
    rows = _rows(run, cfg)
    if limit:
        rows = rows[:limit]
    usage_balance = run["usage_method"] == "balance"
    if not run["started_at"]:
        if usage_balance:
            run["balance_before_auto"] = adapter.balance()
            runs.save(run)
        runs.start(run_id)
    journal = Journal(d / "journal.json")
    for attempt in range(MAX_ATTEMPTS):
        cells = _cells(d)
        pending = [r for r in rows if r["input_id"] not in cells or transient(cells[r["input_id"]])]
        log(f"{run_id} {adapter.name}: pass {attempt + 1}, {len(rows) - len(pending)} done, {len(pending)} to call")
        if not pending:
            break
        if attempt and adapter.retry_wait_s:
            time.sleep(adapter.retry_wait_s)
        if hasattr(adapter, "enrich_batch"):
            adapter.enrich_batch(pending, lambda i, res: _save_cell(d, i, res), journal)
            continue
        for i, row in enumerate(pending, 1):
            try:
                res = adapter.enrich(row)
            except StopRun:
                raise
            except AdapterError as e:
                res = result(ok=False, error=str(e))
            _save_cell(d, row["input_id"], res)
            if i % 50 == 0:
                log(f"  {i}/{len(pending)}")
            if adapter.pause_s:
                time.sleep(adapter.pause_s)

    cells = _cells(d)
    run = runs.load(run_id)
    if all(r["input_id"] in cells for r in rows):
        with open(d / "output" / "output.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=t["output_columns"], extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({"input_id": r["input_id"], **cells[r["input_id"]].get("fields", {})})
        if not run["stopped_at"]:
            run = runs.stop(run_id)
        if usage_balance and not run["usage_units"]:
            before, after = run.get("balance_before_auto"), adapter.balance()
            if before is not None and after is not None:
                runs.set_usage(run_id, before=before, after=after)
        run = runs.load(run_id)
        ok = [c for c in cells.values() if c.get("ok")]
        lat = [c["latency_ms"] for c in ok if c.get("latency_ms") is not None]
        run["script"] = {"endpoint": adapter.name, "cells": len(cells), "successful_requests": len(ok),
                         "retried": sum(c.get("attempts", 1) > 1 for c in cells.values()),
                         "median_latency_ms": statistics.median(lat) if lat else None}
        run["manual"].setdefault("processing_min", 0.0)
        if limit:
            run["status"], run["status_source"] = "Void", "smoke-test"
            run["manual"]["failure_category"] = "setup"
            run["manual"]["notes"] = f"smoke test on the first {limit} rows"
        runs.save(run)
        log(f"{run_id}: output/output.csv written ({len(rows)} rows, {len(ok)} successful requests)")
    return runs.load(run_id)
