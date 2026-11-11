"""Blinded review for tasks scored by a person (config task `review`: file, sample, columns).

`bench blind-sample` takes a random sample of output rows from every run of a task, mixes and
shuffles them, hides which run (and so which platform) each came from, and writes one sheet to
private/review/<task>-blind.csv. You fill in the review columns. `bench blind-import` puts each
reviewed row back into its run's review/<file>, keyed through a private mapping file.
shortcut: blinding hides the platform, not output style (column formats can hint at the tool).
"""
import csv
import json
import random
import secrets

from . import paths, runs, summary
from .scoring import ScoreError, read_csv


def _sheet(task):
    d = paths.private() / "review"
    return d / f"{task}-blind.csv", d / f"{task}-blind-key.json"


def blind_sample(cfg, task, n=None):
    rv = cfg["tasks"][task].get("review")
    if not rv:
        raise runs.RunError(f"{task} has no review settings in the config")
    n = n or rv["sample"]
    items, header, outs = [], None, []
    for r in runs.all_runs():
        out = runs.run_dir(r["run_id"]) / "output" / "output.csv"
        if r["task_id"] != task or summary.final_status(r) == "Void" or not out.exists():
            continue
        h, rows = read_csv(out)
        header = header or h
        outs.append((r["run_id"], rows))
    # Same inputs (every run covers the same domains): review the same accounts on every platform.
    doms = [{row.get("domain") for row in rows} for _, rows in outs]
    shared = sorted(doms[0]) if doms and "domain" in (header or []) and all(d == doms[0] for d in doms) else None
    picked = set(random.Random(task).sample(shared, min(n, len(shared)))) if shared else None
    for run_id, rows in outs:
        chosen = [row for row in rows if row["domain"] in picked] if picked else \
            random.Random(run_id).sample(rows, min(n, len(rows)))
        items.extend((run_id, row) for row in chosen)
    if not items:
        raise runs.RunError(f"No {task} outputs to sample")
    random.Random(secrets.randbits(64)).shuffle(items)
    sheet, keyfile = _sheet(task)
    sheet.parent.mkdir(parents=True, exist_ok=True)
    key = {}
    with open(sheet, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["item_id", *header, *rv["columns"]])
        for i, (run_id, row) in enumerate(items, 1):
            item = f"I{i:04d}"
            key[item] = run_id
            w.writerow([item, *[row.get(c, "") for c in header], *[""] * len(rv["columns"])])
    paths.write_json(keyfile, key)
    return sheet, len(items)


def blind_import(cfg, task, reviewed_path):
    rv = cfg["tasks"][task]["review"]
    _, keyfile = _sheet(task)
    if not keyfile.exists():
        raise runs.RunError(f"Run `bench blind-sample --task {task}` first")
    key = json.loads(keyfile.read_text(encoding="utf-8"))
    header, rows = read_csv(reviewed_path)
    missing = [c for c in ["item_id", "domain", *rv["columns"]] if c not in header]
    if missing:
        raise ScoreError(f"Reviewed sheet is missing columns: {missing}")
    by_run = {}
    for row in rows:
        if row["item_id"] not in key:
            raise ScoreError(f"Unknown item {row['item_id']}")
        if all(not (row[c] or "").strip() for c in rv["columns"]):
            continue
        by_run.setdefault(key[row["item_id"]], []).append([row["domain"], *[row[c] for c in rv["columns"]]])
    for run_id, out in by_run.items():
        f = runs.run_dir(run_id) / "review" / rv["file"]
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["domain", *rv["columns"]])
            w.writerows(out)
    return {run_id: len(v) for run_id, v in by_run.items()}
