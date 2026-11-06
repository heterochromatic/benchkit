"""private/runs.csv (one row per run), results/summary.csv (per task × platform × executor,
the only table that's published) and private/warnings.txt (rule checks). Platforms appear by
label; a labelled vendor's real name never reaches results/."""
import csv
import re
import statistics
from datetime import datetime
from pathlib import Path

from . import costs, paths, runs

RANKED = {"Pass", "Partial", "Fail", "Did not finish"}

RUN_COLUMNS = [
    "run_id", "run_date", "task_id", "task_version", "platform_label", "executor", "tool_version",
    "environment_label", "reference_tier", "operator_id", "operator_profile", "run_order",
    "steps", "context_switches", "help_lookups", "errors_restarts", "active_min", "wallclock_min",
    "processing_min", "status", "failure_category", "not_supported_citation", "ease_rating_1to7",
    "records_in", "records_accepted", "accepted_rate", "quality_floor_met", "usage_units",
    "usage_cost_usd", "add_on_tools", "add_on_cost_usd", "seat_cost_usd_month",
    "plan_floor_usd_month", "cost_per_accepted_record_usd", "cost_per_1000_accepted_usd", "agent_cost_usd",
    "quote_required", "endpoint", "median_latency_ms", "price_effective_date", "account_id", "cost_counted",
    "step_counter_1", "step_counter_2", "reset_verified", "in_ranking",
]


def final_status(run):
    return run["status"] or run["score"].get("proposed_status")


def cost_counted(all_runs):
    """Run IDs whose cost counts: the first run of each task version's input on each account.
    Vendors may not charge again for records an account has already enriched, so later runs
    would look cheaper than a buyer's first run. Steps, time and quality still use every run."""
    first = {}
    for r in sorted(all_runs, key=lambda r: r["started_at"] or r["created_at"]):
        if r["usage_units"] and final_status(r) in RANKED:
            first.setdefault((r["task_version"], r["platform"], r["manual"].get("account_id") or "shared"), r["run_id"])
    return set(first.values())


def run_row(cfg, run, counted=True):
    m, s = run["manual"], run["score"]
    c = costs.run_costs(cfg, run)
    if not counted:
        c = dict.fromkeys(c, None) | {"seat_cost_usd_month": c["seat_cost_usd_month"], "plan_floor_usd_month": c["plan_floor_usd_month"]}
    status = final_status(run)
    return {
        "run_id": run["run_id"],
        "run_date": (run["started_at"] or run["created_at"])[:10],
        "task_id": run["task_id"], "task_version": run["task_version"],
        "platform_label": run["platform_label"], "executor": run["executor"],
        "tool_version": m.get("tool_version"), "environment_label": run["environment_label"],
        # Tier names can identify a labelled vendor, so they're shown only for named platforms.
        "reference_tier": run["reference_tier"] if run["platform_label"] == cfg["platforms"][run["platform"]]["name"] else "",
        "operator_id": run["operator_id"], "operator_profile": run["operator_profile"],
        "run_order": run["run_order"], "steps": m.get("steps"),
        "context_switches": m.get("context_switches"), "help_lookups": m.get("help_lookups"),
        "errors_restarts": m.get("errors_restarts"), "active_min": m.get("active_min"),
        "wallclock_min": run["wallclock_min"], "processing_min": m.get("processing_min"),
        "status": status, "failure_category": m.get("failure_category"),
        "not_supported_citation": m.get("not_supported_citation"),
        "ease_rating_1to7": m.get("ease_rating_1to7"), "records_in": s.get("records_in"),
        "records_accepted": s.get("accepted_records"), "accepted_rate": s.get("accepted_rate"),
        "quality_floor_met": s.get("floor_met"),
        "usage_units": ";".join(f"{k}={v}" for k, v in run["usage_units"].items()),
        **c, "add_on_tools": m.get("add_on_tools"),
        "agent_cost_usd": (run.get("agent") or {}).get("agent_cost_usd"),
        "cost_per_1000_accepted_usd": round(c["cost_per_accepted_record_usd"] * 1000, 2)
        if c["cost_per_accepted_record_usd"] is not None else None,
        "endpoint": run.get("endpoint"), "median_latency_ms": (run.get("script") or {}).get("median_latency_ms"),
        "price_effective_date": cfg.get("price_effective_date"),
        "account_id": m.get("account_id") or "shared", "cost_counted": counted,
        "quote_required": run["quote_required"], "step_counter_1": m.get("step_counter_1"),
        "step_counter_2": m.get("step_counter_2"), "reset_verified": m.get("reset_verified"),
        "in_ranking": status in RANKED,
    }


def _stats(vals):
    vals = [float(v) for v in vals if v not in (None, "")]
    if not vals:
        return "", "", ""
    return round(statistics.median(vals), 2), round(min(vals), 2), round(max(vals), 2)


def warnings_for(cfg, all_runs):
    w = []
    if not cfg.get("price_effective_date"):
        w.append("config has no price_effective_date; confirm unit prices on the run date")
    by_task = {}
    for r in all_runs:
        if r["started_at"] and r["executor"] == "human":
            by_task.setdefault((r["task_id"], r["task_version"]), []).append(r)
    for (task, ver), rs in by_task.items():
        ts = [datetime.fromisoformat(r["started_at"]) for r in rs]
        span = (max(ts) - min(ts)).total_seconds() / 3600
        if span > 48:
            w.append(f"{task} {ver}: human runs span {span:.0f} h across platforms (rule 5.2 allows 48 h)")
    for r in all_runs:
        m = r["manual"]
        if m.get("steps_needs_reconcile"):
            w.append(f"{r['run_id']}: step counters differ by more than 10%; record reconciled --steps")
        if "crm_columns" in cfg["tasks"].get(r["task_id"], {}) and m.get("reset_verified") != "yes" and final_status(r) != "Void":
            w.append(f"{r['run_id']}: {r['task_id']} run without a verified sandbox reset")
        if final_status(r) in RANKED and not r["usage_units"]:
            w.append(f"{r['run_id']}: no usage recorded, so cost can't be computed")
        if final_status(r) is None:
            w.append(f"{r['run_id']}: no status yet (run `bench score` or record a status)")
        if r["executor"] == "human" and r["started_at"] and not m.get("active_min"):
            w.append(f"{r['run_id']}: active time not recorded from the recording")
        comp = (r.get("agent") or {}).get("compliance")
        if comp and not comp["ok"] and m.get("compliance_reviewed") != "yes":
            w.append(f"{r['run_id']}: agent transcript flagged ({'; '.join(comp['flags'][:3])}); review, then record "
                     "--compliance_reviewed yes (or Void the run)")
        bal = r.get("usage_balance")
        if r["usage_method"] == "per_table" and bal and bal.get("unit") in r["usage_units"]:
            table, drop = r["usage_units"][bal["unit"]], bal["before"] - bal["after"]
            if drop and abs(table - drop) / drop > 0.05:
                w.append(f"{r['run_id']}: per-table {bal['unit']} {table} vs balance drop {drop} differ by more than 5%")
    return w


def write_all(cfg):
    out, priv = paths.results(), paths.private()
    out.mkdir(parents=True, exist_ok=True)
    priv.mkdir(parents=True, exist_ok=True)
    all_runs = runs.all_runs()
    counted = cost_counted(all_runs)
    rows = [run_row(cfg, r, r["run_id"] in counted) for r in all_runs]
    scores = {r["run_id"]: r["score"].get("metrics", {}) for r in all_runs}
    with open(priv / "runs.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=RUN_COLUMNS, extrasaction="ignore")
        wr.writeheader()
        wr.writerows(rows)

    groups = {}
    for row in rows:
        # Profiles stay apart: one practitioner per platform is illustrative, not a sample to pool.
        key = (row["task_id"], row["task_version"], row["platform_label"], row["endpoint"] or "", row["executor"],
               row["operator_profile"])
        groups.setdefault(key, []).append(row)
    summary = []
    for (task, ver, label, endpoint, ex, profile), rs in sorted(groups.items()):
        ranked = [r for r in rs if r["in_ranking"]]
        rec = {"task_id": task, "task_version": ver, "platform_label": label, "endpoint": endpoint, "executor": ex,
               "operator_profile": profile, "cost_runs": sum(r["cost_counted"] and r["in_ranking"] for r in rs),
               "runs": len(rs), "ranked_runs": len(ranked),
               "statuses": ";".join(f"{s}={sum(r['status'] == s for r in rs)}" for s in runs.STATUSES if any(r["status"] == s for r in rs)),
               "environment_labels": ";".join(sorted({r["environment_label"] for r in rs}))}
        for k in ("steps", "active_min", "wallclock_min", "accepted_rate", "cost_per_accepted_record_usd",
                  "cost_per_1000_accepted_usd", "agent_cost_usd", "median_latency_ms"):
            rec[f"{k}_median"], rec[f"{k}_min"], rec[f"{k}_max"] = _stats(r[k] for r in ranked)
        ranked_ids = {r["run_id"] for r in ranked}
        wanted = cfg.get("summary_metrics", {}).get(task, [])
        keys = list(dict.fromkeys(k for w in wanted for k in ([w] if not w.endswith("*") else
                                   sorted({m for i in ranked_ids for m in scores[i] if m.startswith(w[:-1])}))))
        for k in keys:   # "prefix*" in the config expands to every scorer metric with that prefix
            rec[f"{k}_median"] = _stats(scores[i].get(k) for i in ranked_ids)[0]
        summary.append(rec)

    loaded = {}
    proj = cfg["scenario"]["projection_task"]
    for name, p in cfg["platforms"].items():
        t1 = [r for r in all_runs if r["platform"] == name and r["task_id"] == proj and r["run_id"] in counted]
        fl = costs.fully_loaded(cfg, name, t1)
        loaded[p["label"]] = fl
    for rec in summary:
        fl = loaded.get(rec["platform_label"])
        rec["fully_loaded_monthly_usd"] = fl["monthly_usd"] if fl else ""
        rec["fully_loaded_per_record_usd"] = fl["per_record_usd"] if fl else ""
        rec["price_effective_date"] = cfg.get("price_effective_date", "")

    if summary:
        fields = list(dict.fromkeys(k for rec in summary for k in rec))
        with open(out / "summary.csv", "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=fields, restval="")
            wr.writeheader()
            wr.writerows(summary)
    warns = warnings_for(cfg, all_runs)
    (priv / "warnings.txt").write_text("\n".join(warns) + ("\n" if warns else ""), encoding="utf-8")
    return rows, summary, warns


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def check_anonymized(cfg, files=None):
    """Names of labelled vendors, and any email address, must never appear in published results."""
    files = [Path(f) for f in files] if files else sorted(paths.results().glob("*.csv")) + sorted(paths.results().glob("*.json"))
    names = [p["name"] for p in cfg["platforms"].values() if p["name"] != p["label"]]
    names += cfg.get("anonymity_extra_terms", [])  # domains, product and tier names, from the private overlay
    problems = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        low = text.lower()
        for n in names:
            # Letters/digits only as boundaries, so endpoint IDs like "acme_match" are caught too.
            if re.search(rf"(?<![a-z0-9]){re.escape(n.lower())}(?![a-z0-9])", low):
                problems.append(f"{f.name}: names '{n}'")
        if EMAIL_RE.search(text):
            problems.append(f"{f.name}: contains an email address")
    return problems
