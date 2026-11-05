"""`bench sanity`: check the graders against the real answer keys before freezing.

For every task whose dataset exists:
- empty: an output with the right columns and no answers must accept nothing;
- oracle: the bench repo's scorers may define ORACLES = {task: fn(run, cfg)} that writes the answer
  key as the run's output (plus verifier results) and returns {metric: expected value}; scoring it
  must give exactly those values (normally 1.0 on every keyed metric).
Runs go in a throwaway folder, so nothing is added to private/runs.
"""
import csv
import json
import os
import tempfile
from pathlib import Path

from . import paths, runs, scoring
from .verify import _digits


def _empty_output(run, t):
    d = runs.run_dir(run["run_id"])
    with open(d / "output" / "output.csv", "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(t["output_columns"])
    paths.write_json(d / "verifier" / "results.json", {"email": {}, "phone": {}})


def run_sanity(cfg):
    """Returns [(task, check, ok, detail)]."""
    real = paths.datasets()
    oracles = getattr(scoring.plugin(), "ORACLES", {})
    platform = next(iter(cfg["platforms"]))
    out = []
    old = os.environ.get("BENCH_ROOT")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BENCH_ROOT"] = tmp
        try:
            (Path(tmp) / "private").mkdir()
            os.symlink(real, Path(tmp) / "private" / "datasets")
            for tid, t in cfg["tasks"].items():
                if not (real / t["input"]).exists():
                    out.append((tid, "all", None, "dataset missing"))
                    continue
                new = lambda: runs.new_run(cfg, tid, platform, "human", "sanity", "first-timer", runs.ENV_LABELS[0])
                if "output_columns" in t:
                    r = new()
                    _empty_output(r, t)
                    try:
                        res = scoring.score(r, cfg)
                        out.append((tid, "empty", not res["accepted_records"], f"accepted {res['accepted_records']}"))
                    except scoring.ScoreError as e:
                        out.append((tid, "empty", None, f"needs review files ({e})"))
                if tid not in oracles:
                    out.append((tid, "oracle", None, "no oracle defined"))
                    continue
                r = new()
                expected = oracles[tid](r, cfg)
                if not expected:
                    out.append((tid, "oracle", None, "answer key not built yet"))
                    continue
                got = scoring.score(r, cfg)["metrics"]
                bad = {k: (got.get(k), v) for k, v in expected.items() if got.get(k) != v}
                out.append((tid, "oracle", not bad, "all keyed metrics match" if not bad else f"got/expected {bad}"))
        finally:
            if old is None:
                os.environ.pop("BENCH_ROOT", None)
            else:
                os.environ["BENCH_ROOT"] = old
    return out


def write_rows(path, header, rows):
    """Helper for ORACLES: write a CSV into a run folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def mark_valid(run, emails=(), phones=()):
    """Helper for ORACLES: verifier results that accept the given emails and phones."""
    paths.write_json(runs.run_dir(run["run_id"]) / "verifier" / "results.json",
                     {"email": {e.strip().lower(): "valid" for e in emails if e},
                      "phone": {_digits(p): "valid" for p in phones if p}})
