"""Run lifecycle: create, start, stop, usage, manual record. One folder and one run.json per run."""
import hashlib
import json
import shutil
from datetime import datetime, timezone

from . import paths

STATUSES = ["Pass", "Partial", "Fail", "Did not finish", "Not supported", "Not comparable", "Void"]
FAILURE_CATEGORIES = ["task wording", "vendor gap", "setup", "operator error", "product friction"]
ENV_LABELS = ["self-serve monthly", "self-serve annual", "quoted package", "free trial"]
PROFILES = ["first-timer", "practitioner", "agent", "script"]
EXECUTORS = ["human", "agent", "script"]


class RunError(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def run_dir(run_id):
    return paths.runs() / run_id


def load(run_id) -> dict:
    p = run_dir(run_id) / "run.json"
    if not p.exists():
        raise RunError(f"No run {run_id}")
    return json.loads(p.read_text(encoding="utf-8"))


def save(run: dict):
    paths.write_json(run_dir(run["run_id"]) / "run.json", run, indent=2)


def all_runs() -> list:
    if not paths.runs().exists():
        return []
    out = []
    for d in sorted(paths.runs().iterdir()):
        if d.is_dir() and (d / "run.json").exists():
            out.append(json.loads((d / "run.json").read_text(encoding="utf-8")))
    return out


def _next_id() -> str:
    existing = [r["run_id"] for r in all_runs()]
    n = 1 + max((int(x[1:]) for x in existing if x[1:].isdigit()), default=0)
    return f"R{n:04d}"


def new_run(cfg, task, platform, executor, operator, profile, env_label, order=None, cost_only=False,
            endpoint=None) -> dict:
    if task not in cfg["tasks"]:
        raise RunError(f"Unknown task {task}")
    if platform not in cfg["platforms"]:
        raise RunError(f"Unknown platform {platform}")
    if executor not in EXECUTORS:
        raise RunError(f"executor must be one of {EXECUTORS}")
    if profile not in PROFILES:
        raise RunError(f"profile must be one of {PROFILES}")
    if executor in ("agent", "script") and profile != executor or executor == "human" and profile in ("agent", "script"):
        raise RunError("agent and script runs use the profile of the same name; human runs use first-timer or practitioner")
    if (executor == "script") != bool(endpoint):
        raise RunError("script runs need an endpoint, and only script runs take one")
    _check_frozen(cfg)
    if env_label not in ENV_LABELS:
        raise RunError(f"environment label must be one of {ENV_LABELS}")
    pcfg = cfg["platforms"][platform]
    if cost_only and not pcfg.get("parallel_cost_runs"):
        raise RunError(f"{pcfg['name']} usage is read from the balance; cost-only parallel runs aren't allowed (methodology 5.9)")

    tcfg = cfg["tasks"][task]
    src = paths.datasets() / tcfg["input"]
    if not src.exists():
        raise RunError(f"Dataset missing: {src}")
    checksum = sha256(src)
    for other in all_runs():
        if other["task_id"] == task and other["task_version"] == tcfg["version"] and other["input_checksum"] != checksum:
            raise RunError(f"Input for {task} differs from run {other['run_id']} (checksum mismatch, methodology 5.2)")

    run_id = _next_id()
    d = run_dir(run_id)
    for sub in ("input", "output", "work", "verifier", "review", "transcript"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, d / "input" / tcfg["input"])

    tier = pcfg["task_tiers"][task]
    run = {
        "run_id": run_id,
        "created_at": now(),
        "task_id": task,
        "task_version": tcfg["version"],
        "platform": platform,
        "platform_label": pcfg["label"],
        "executor": executor,
        "operator_id": operator,
        "operator_profile": profile,
        "run_order": order,
        "environment_label": env_label,
        "reference_tier": tier,
        "quote_required": bool(pcfg.get("quote_only")),
        "cost_only": cost_only,
        "input_checksum": checksum,
        "started_at": None,
        "stopped_at": None,
        "wallclock_min": None,
        "usage_units": {},
        "usage_method": pcfg["usage_method"],
        "manual": {},
        "score": {},
        "status": None,
        "status_source": None,
        "endpoint": endpoint,
    }
    save(run)
    return run


def _check_frozen(cfg):
    """After `bench freeze`, the config can't change without re-freezing (and a CHANGELOG entry)."""
    plan = paths.results() / "run-plan.json"
    if plan.exists():
        frozen = json.loads(plan.read_text(encoding="utf-8"))
        if frozen["config_sha256"] != sha256(paths.config_path()):
            raise RunError("config/bench.json changed after `bench freeze`; log the change in CHANGELOG.md and re-freeze")
        if frozen.get("overlay_sha256") != overlay_sha256():
            raise RunError("private/platforms.json changed after `bench freeze`; log the change in CHANGELOG.md and re-freeze")


def overlay_sha256():
    ov = paths.platform_overlay()
    return sha256(ov) if ov.exists() else None


def _lock_path(platform):
    return paths.locks() / f"{platform}.lock"


def start(run_id) -> dict:
    run = load(run_id)
    if run["started_at"]:
        raise RunError(f"{run_id} already started")
    if not run["cost_only"]:
        paths.locks().mkdir(parents=True, exist_ok=True)
        lp = _lock_path(run["platform"])
        try:
            with open(lp, "x") as f:
                f.write(run_id)
        except FileExistsError:
            holder = lp.read_text().strip()
            raise RunError(f"Timed run {holder} is still active on {run['platform']}; timed runs can't overlap (methodology 5.7)") from None
    run["started_at"] = now()
    save(run)
    return run


def stop(run_id) -> dict:
    run = load(run_id)
    if not run["started_at"]:
        raise RunError(f"{run_id} was never started")
    if run["stopped_at"]:
        raise RunError(f"{run_id} already stopped")
    run["stopped_at"] = now()
    t0 = datetime.fromisoformat(run["started_at"])
    t1 = datetime.fromisoformat(run["stopped_at"])
    run["wallclock_min"] = round((t1 - t0).total_seconds() / 60, 2)
    lp = _lock_path(run["platform"])
    if lp.exists() and lp.read_text().strip() == run_id:
        lp.unlink()
    save(run)
    return run


def set_usage(run_id, before=None, after=None, units=None, unit=None) -> dict:
    run = load(run_id)
    if run["usage_method"] == "balance":
        if before is None or after is None:
            raise RunError("This platform's usage is read from the balance: pass --before and --after")
        if after > before:
            raise RunError("Balance after is higher than before; check the readings")
        run["usage_units"] = {unit or "credits": before - after}
        run["usage_balance"] = {"before": before, "after": after}
    else:
        if not units:
            raise RunError("This platform uses per-table attribution: pass --units name=value (e.g. data_credits=812 actions=1000)")
        run["usage_units"] = units
    run["usage_recorded_at"] = now()
    save(run)
    return run


MANUAL_FIELDS = {
    "steps": float, "step_counter_1": float, "step_counter_2": float, "context_switches": float,
    "help_lookups": float, "errors_restarts": float, "active_min": float, "processing_min": float,
    "ease_rating_1to7": float, "add_on_cost_usd": float, "add_on_tools": str, "tool_version": str,
    "not_supported_citation": str, "failure_category": str, "reset_verified": str, "notes": str,
    "pricing_snapshot_date": str, "starting_state_ok": str,
}


def record(run_id, fields: dict, status=None) -> dict:
    run = load(run_id)
    for k, v in fields.items():
        if k not in MANUAL_FIELDS:
            raise RunError(f"Unknown field {k}; allowed: {sorted(MANUAL_FIELDS)}")
        run["manual"][k] = MANUAL_FIELDS[k](v)
    fc = run["manual"].get("failure_category")
    if fc and fc not in FAILURE_CATEGORIES:
        raise RunError(f"failure_category must be one of {FAILURE_CATEGORIES}")
    c1, c2 = run["manual"].get("step_counter_1"), run["manual"].get("step_counter_2")
    if c1 is not None and c2 is not None and "steps" not in fields:
        if max(c1, c2) > 0 and abs(c1 - c2) / max(c1, c2) > 0.10:
            run["manual"]["steps_needs_reconcile"] = True
        else:
            run["manual"]["steps"] = (c1 + c2) / 2
            run["manual"].pop("steps_needs_reconcile", None)
    if "steps" in fields:
        run["manual"].pop("steps_needs_reconcile", None)
    if status:
        if status not in STATUSES:
            raise RunError(f"status must be one of {STATUSES}")
        if status == "Not supported" and not run["manual"].get("not_supported_citation"):
            raise RunError("Not supported needs --not_supported_citation (vendor doc URL or recording reference)")
        if status == "Void" and not run["manual"].get("failure_category"):
            raise RunError("Void needs --failure_category (setup or operator error)")
        run["status"] = status
        run["status_source"] = "manual"
    save(run)
    return run
