"""`bench` command line. Run `bench -h` or `bench <command> -h`."""
import argparse
import csv
import hashlib
import json
import sys

from . import adapters, agent, artifacts, paths, runs, scoring, summary, verify


def _units(pairs):
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        if not v:
            raise runs.RunError(f"--units expects name=value, got {p}")
        out[k] = float(v)
    return out


def crm_hash(path, columns):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=lambda r: r["record_id"])
    h = hashlib.sha256()
    for r in rows:
        h.update("\x1f".join((r.get(c) or "").strip() for c in columns).encode())
        h.update(b"\x1e")
    return h.hexdigest(), len(rows)


def cmd_new(cfg, a):
    r = runs.new_run(cfg, a.task, a.platform, a.executor, a.operator, a.profile, a.env, a.order, a.cost_only)
    print(r["run_id"])


def cmd_start(cfg, a):
    r = runs.start(a.run_id)
    print(f"{r['run_id']} started {r['started_at']}")


def cmd_stop(cfg, a):
    r = runs.stop(a.run_id)
    print(f"{r['run_id']} stopped; wall-clock {r['wallclock_min']} min")


def cmd_usage(cfg, a):
    r = runs.set_usage(a.run_id, a.before, a.after, _units(a.units), unit=a.unit)
    print(f"{r['run_id']} usage {r['usage_units']}")


def cmd_record(cfg, a):
    fields = {k: v for k, v in vars(a).items() if k in runs.MANUAL_FIELDS and v is not None}
    r = runs.record(a.run_id, fields, a.status)
    if r["manual"].get("steps_needs_reconcile"):
        print("Step counters differ by more than 10%: reconcile and record --steps", file=sys.stderr)
    print(f"{r['run_id']} recorded; status {r['status']}")


def cmd_verify(cfg, a):
    run = runs.load(a.run_id)
    emails, phones = scoring.output_contacts(run, cfg)
    ev = verify.verify_emails(emails, a.mode, cfg, a.email_results)
    pv = verify.verify_phones(phones, a.phone_results) if phones else {}
    out = runs.run_dir(a.run_id) / "verifier" / "results.json"
    out.write_text(json.dumps({"email": ev, "phone": pv}, indent=1), encoding="utf-8")
    ok = sum(s in cfg["verifier"]["accept_email_statuses"] for s in ev.values())
    unchecked = sum(v is None for v in pv.values())
    print(f"{a.run_id}: {len(ev)} emails checked, {ok} accepted; {len(pv)} phones, {unchecked} without a result")


def cmd_score(cfg, a):
    run = runs.load(a.run_id)
    res = scoring.score(run, cfg)
    run["score"] = res
    runs.save(run)
    print(json.dumps({"run_id": run["run_id"], "proposed_status": res["proposed_status"],
                      "manual_status": run["status"], "accepted_rate": res["accepted_rate"],
                      "floor_met": res["floor_met"], **res["metrics"]}, indent=1))


def cmd_prompt(cfg, a):
    print(agent.build_prompt(cfg, runs.load(a.run_id)))


def cmd_agent(cfg, a):
    if a.dry_run:
        print(json.dumps(agent.run_agent(cfg, a.run_id, dry_run=True), indent=1))
        return
    r = agent.run_agent(cfg, a.run_id)
    print(json.dumps({"run_id": r["run_id"], **r["agent"], "steps": r["manual"]["steps"],
                      "wallclock_min": r["wallclock_min"]}, indent=1))


def cmd_agent_batch(cfg, a):
    if a.task not in cfg["agent"]["tasks"]:
        raise runs.RunError(f"{a.task} isn't in the agent track (config agent.tasks: {cfg['agent']['tasks']})")
    reps = a.reps or cfg["agent"]["default_repetitions"]
    for i in range(reps):
        r = runs.new_run(cfg, a.task, a.platform, "agent", f"agent-{cfg['agent']['model']}", "agent", a.env, i + 1)
        print(f"[{i + 1}/{reps}] {r['run_id']}", flush=True)
        if not a.create_only:
            res = agent.run_agent(cfg, r["run_id"])
            print(f"    exit={res['agent']['exit_code']} steps={res['manual']['steps']} "
                  f"wallclock={res['wallclock_min']} min agent_cost=${res['agent']['agent_cost_usd']}", flush=True)
            print(f"    next: read usage for table '{r['run_id']}' and run `bench usage {r['run_id']} ...`", flush=True)


def cmd_reset_check(cfg, a):
    t = cfg["tasks"][runs.load(a.run_id)["task_id"]]
    if "crm_columns" not in t:
        raise runs.RunError("reset-check is for tasks that write to a sandbox CRM (crm_columns in the task config)")
    seed = t["seed_hash"] or crm_hash(paths.datasets() / t["input"], t["crm_columns"])[0]
    got, n = crm_hash(a.export, t["crm_columns"])
    ok = got == seed and n == t["rows"]
    runs.record(a.run_id, {"reset_verified": "yes" if ok else "no"})
    print(f"{a.run_id}: reset {'verified' if ok else 'FAILED'} (rows {n}, hash {'match' if got == seed else 'mismatch'})")
    if not ok:
        sys.exit(1)


def cmd_list_providers(cfg, a):
    for name, ad in adapters.registry().items():
        print(f"{name}\t{ad.platform}\t{cfg['platforms'][ad.platform]['label']}\t{ad.docs_url}")


def cmd_run(cfg, a):
    if a.resume:
        adapters.run_endpoint(cfg, a.resume, limit=a.limit)
        return
    if not (a.task and a.env):
        raise runs.RunError("pass --task and --env (or --resume <run_id>)")
    reg = adapters.registry()
    names = [e.strip() for e in a.endpoints.split(",")] if a.endpoints else list(reg)
    unknown = [n for n in names if n not in reg]
    if unknown:
        raise runs.RunError(f"unknown endpoints {unknown}; see `bench list-providers`")
    for i, name in enumerate(names, 1):
        r = runs.new_run(cfg, a.task, reg[name].platform, "script", f"script-{name}", "script", a.env, i,
                         endpoint=name)
        print(f"[{i}/{len(names)}] {r['run_id']} {name}", flush=True)
        adapters.run_endpoint(cfg, r["run_id"], limit=a.limit, log=lambda m: print(f"    {m}", flush=True))


def cmd_freeze(cfg, a):
    plan, missing = artifacts.freeze(cfg)
    print(f"results/run-plan.json written (config {plan['config_sha256'][:12]})")
    for t in missing:
        print(f"WARNING {t}: dataset missing, so its checksum isn't frozen yet")


def cmd_manifest(cfg, a):
    m = artifacts.build_manifest(cfg)
    print(f"results/manifest.json written ({m['run_count']} runs)")


def cmd_audit(cfg, a):
    errors = artifacts.audit(cfg)
    for e in errors:
        print(f"FAIL {e}")
    if errors:
        sys.exit(1)
    print("audit passed: manifest hashes match, every run has a status, no credentials, results clean")


def cmd_check_anon(cfg, a):
    problems = summary.check_anonymized(cfg, a.paths or None)
    for pr in problems:
        print(f"LEAK {pr}")
    if problems:
        sys.exit(1)
    print("results contain labels only")


def cmd_summary(cfg, a):
    rows, summ, warns = summary.write_all(cfg)
    print(f"{len(rows)} runs -> private/runs.csv; {len(summ)} groups -> results/summary.csv")
    for w in warns:
        print(f"WARNING {w}")


def cmd_list(cfg, a):
    for r in runs.all_runs():
        st = summary.final_status(r) or "-"
        print(f"{r['run_id']}  {r['task_version']:6} {r['platform']:9} {r['executor']:5} "
              f"{r['operator_id']:14} started={bool(r['started_at'])!s:5} stopped={bool(r['stopped_at'])!s:5} status={st}")


def cmd_show(cfg, a):
    print(json.dumps(runs.load(a.run_id), indent=1))


def build_parser():
    p = argparse.ArgumentParser(prog="bench", description="benchkit: run, score and publish a benchmark")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("new", help="create a run")
    s.add_argument("--task", required=True)
    s.add_argument("--platform", required=True)
    s.add_argument("--executor", choices=["human", "agent"], default="human")
    s.add_argument("--operator", required=True)
    s.add_argument("--profile", required=True, choices=runs.PROFILES)
    s.add_argument("--env", required=True, choices=runs.ENV_LABELS)
    s.add_argument("--order", type=int)
    s.add_argument("--cost-only", action="store_true", help="untimed run; parallel allowed only for per-table platforms")
    s.set_defaults(fn=cmd_new)

    for name, fn, h in (("start", cmd_start, "start the clock (takes the platform lock)"),
                        ("stop", cmd_stop, "stop the clock (releases the lock)")):
        s = sub.add_parser(name, help=h)
        s.add_argument("run_id")
        s.set_defaults(fn=fn)

    s = sub.add_parser("usage", help="record usage: --before/--after (balance) or --units k=v (per table)")
    s.add_argument("run_id")
    s.add_argument("--before", type=float)
    s.add_argument("--after", type=float)
    s.add_argument("--unit", help="unit name for balance readings (default credits)")
    s.add_argument("--units", nargs="*")
    s.set_defaults(fn=cmd_usage)

    s = sub.add_parser("record", help="record values from the recording, counters, costs, status")
    s.add_argument("run_id")
    for f, typ in runs.MANUAL_FIELDS.items():
        s.add_argument(f"--{f}", type=typ)
    s.add_argument("--status", choices=runs.STATUSES)
    s.set_defaults(fn=cmd_record)

    s = sub.add_parser("verify", help="run the shared verifier on a run's output")
    s.add_argument("run_id")
    s.add_argument("--mode", choices=["import", "zerobounce", "cache"], required=True)
    s.add_argument("--email-results", help="verifier export CSV (import mode)")
    s.add_argument("--phone-results", help="line-type results CSV: phone,status")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("score", help="run the task's acceptance test")
    s.add_argument("run_id")
    s.set_defaults(fn=cmd_score)

    s = sub.add_parser("prompt", help="print the agent prompt for a run")
    s.add_argument("run_id")
    s.set_defaults(fn=cmd_prompt)

    s = sub.add_parser("agent", help="execute an agent run with Claude Code")
    s.add_argument("run_id")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_agent)

    s = sub.add_parser("agent-batch", help="create and execute N agent repetitions")
    s.add_argument("--task", required=True)
    s.add_argument("--platform", required=True)
    s.add_argument("--env", required=True, choices=runs.ENV_LABELS)
    s.add_argument("--reps", type=int)
    s.add_argument("--create-only", action="store_true")
    s.set_defaults(fn=cmd_agent_batch)

    s = sub.add_parser("reset-check", help="verify the sandbox CRM matches the task's seed export")
    s.add_argument("run_id")
    s.add_argument("--export", required=True, help="HubSpot export CSV taken just before the run")
    s.set_defaults(fn=cmd_reset_check)

    s = sub.add_parser("summary", help="write results/summary.csv (published) and private/runs.csv, warnings.txt")
    s.set_defaults(fn=cmd_summary)

    s = sub.add_parser("list-providers", help="list scripted endpoints: id, platform, label, docs")
    s.set_defaults(fn=cmd_list_providers)

    s = sub.add_parser("run", help="scripted run: call each endpoint's API directly (one run per endpoint)")
    s.add_argument("--task")
    s.add_argument("--env", choices=runs.ENV_LABELS)
    s.add_argument("--endpoints", help="comma-separated endpoint IDs (default: all)")
    s.add_argument("--limit", type=int, default=0, help="smoke test on the first N rows (recorded as Void)")
    s.add_argument("--resume", metavar="RUN_ID", help="continue a run without re-calling finished rows")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("freeze", help="write results/run-plan.json; later config changes are refused")
    s.set_defaults(fn=cmd_freeze)

    s = sub.add_parser("manifest", help="write results/manifest.json with a SHA-256 for every run file")
    s.set_defaults(fn=cmd_manifest)

    s = sub.add_parser("audit", help="check manifest hashes, statuses, redaction and published results")
    s.set_defaults(fn=cmd_audit)

    s = sub.add_parser("check-anon", help="fail if results/ names a vendor or contains emails")
    s.add_argument("paths", nargs="*", help="files to check (default: results/*.csv and *.json)")
    s.set_defaults(fn=cmd_check_anon)

    s = sub.add_parser("list", help="list runs")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="print a run record")
    s.add_argument("run_id")
    s.set_defaults(fn=cmd_show)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        paths.load_env()
        cfg = paths.load_config()
        args.fn(cfg, args)
    except (runs.RunError, scoring.ScoreError, verify.VerifyError, paths.PathError, adapters.AdapterError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
