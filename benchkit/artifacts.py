"""Run plan, manifest and audit.

- `bench freeze` writes results/run-plan.json: config hash, dataset checksums, task versions,
  price date, benchkit version. Runs are refused if the config changes afterwards.
- `bench manifest` writes results/manifest.json: SHA-256 of every file in every run folder.
  Hashes are published; the files stay private, so an auditor given access can confirm nothing changed.
- `bench audit` checks the manifest against the files, that every run has a status, that no
  credential survived redaction, and that published results are clean.
"""
import csv
import hashlib
import json
import os
from datetime import datetime, timezone

from . import __version__, paths, runs, scoring, summary
from .adapters import REDACTED, SECRET_KEYS


def prereg_missing(cfg):
    """Dotted config paths listed in prereg_required that are still empty: decisions the method
    says must be made, and written down, before freezing."""
    missing = []
    for dotted in cfg.get("prereg_required", []):
        v = cfg
        for part in dotted.split("."):
            v = v.get(part) if isinstance(v, dict) else None
        if v in (None, "", [], {}):
            missing.append(dotted)
    return missing


def freeze(cfg):
    undecided = prereg_missing(cfg)
    if undecided:
        raise runs.RunError(f"Can't freeze: still undecided in the config: {', '.join(undecided)}")
    tasks = {}
    for tid, t in cfg["tasks"].items():
        src = paths.datasets() / t["input"]
        tasks[tid] = {"version": t["version"], "input": t["input"],
                      "input_sha256": runs.sha256(src) if src.exists() else None}
    plan = {
        "title": cfg.get("title"), "methodology_version": cfg.get("methodology_version"),
        "frozen_at": runs.now(), "benchkit_version": __version__,
        "config_sha256": runs.sha256(paths.config_path()),
        "overlay_sha256": runs.overlay_sha256(),  # private names and prices: hash only
        "price_effective_date": cfg.get("price_effective_date"),
        "canary": cfg.get("canary"),
        "holdout_share": (cfg.get("holdout") or {}).get("share", 0),
        "platforms": sorted({p["label"] for p in cfg["platforms"].values()}),  # labels only: keys can name vendors
        "tasks": tasks,
    }
    missing = [tid for tid, t in tasks.items() if t["input_sha256"] is None]
    out = paths.results() / "run-plan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return plan, missing


def export_public(cfg):
    """Write the scorers' PUBLIC_EXPORTS (non-personal answer-key rows outside the held-out set) to
    datasets/public/<name>.csv, for others to reproduce scoring. Refuses anything with an email."""
    exports = getattr(scoring.plugin(), "PUBLIC_EXPORTS", {})
    written = []
    for name, fn in exports.items():
        header, rows = fn(cfg)
        f = paths.repo() / "datasets" / "public" / f"{name}.csv"
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([f"# {cfg.get('canary', '')}"] if cfg.get("canary") else [])
            w.writerow(header)
            w.writerows(rows)
        if summary.check_anonymized(cfg, [f]):
            f.unlink()
            raise runs.RunError(f"{name}: export would publish a vendor name or an email address; not written")
        written.append((f, len(rows)))
    return written


def _run_files(d):
    """{key: file}. Keys hash the relative path: agents name scratch files after vendors, and the manifest is published."""
    return {"path-" + hashlib.sha256(str(f.relative_to(d)).encode()).hexdigest()[:16]: f
            for f in sorted(d.rglob("*")) if f.is_file()}


def build_manifest(cfg):
    entries = []
    for r in runs.all_runs():
        d = runs.run_dir(r["run_id"])
        files = {k: runs.sha256(f) for k, f in _run_files(d).items()}
        entries.append({"run_id": r["run_id"], "task_version": r["task_version"], "platform": r["platform_label"],
                        "status": summary.final_status(r), "files": files})
    manifest = {"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "benchkit_version": __version__, "run_count": len(entries), "runs": entries}
    out = paths.results() / "manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def _unredacted(value, where):
    """Credential-like keys inside any `headers` object must be redacted (values elsewhere are
    caught by the secret-value scan, so response fields like next_page_token aren't flagged)."""
    errs = []
    if isinstance(value, dict):
        for k, v in value.items():
            if k.lower() == "headers" and isinstance(v, dict):
                errs += [f"unredacted credential header {where}.headers.{h}" for h, hv in v.items()
                         if any(s in h.lower() for s in SECRET_KEYS) and hv not in ("", None, REDACTED)]
            errs += _unredacted(v, f"{where}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            errs += _unredacted(v, f"{where}[{i}]")
    return errs


def audit(cfg):
    errors = []
    mp = paths.results() / "manifest.json"
    if not mp.exists():
        errors.append("no results/manifest.json (run `bench manifest`)")
        manifest = {"runs": []}
    else:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    on_disk = {r["run_id"] for r in runs.all_runs()}
    listed = {e["run_id"] for e in manifest["runs"]}
    for rid in sorted(on_disk ^ listed):
        errors.append(f"{rid}: {'not in manifest' if rid in on_disk else 'in manifest but missing on disk'}")
    for e in manifest["runs"]:
        on_disk_files = _run_files(runs.run_dir(e["run_id"]))
        for rel, h in e["files"].items():
            f = on_disk_files.get(rel)
            if f is None:
                errors.append(f"{e['run_id']}: missing {rel}")
            elif runs.sha256(f) != h:
                errors.append(f"{e['run_id']}: hash mismatch {rel}")
    secrets = [os.environ[k] for p in cfg["platforms"].values() for k in p["credential_env"] if os.environ.get(k)]
    for r in runs.all_runs():
        d = runs.run_dir(r["run_id"])
        if summary.final_status(r) is None:
            errors.append(f"{r['run_id']}: no status")
        for f in list((d / "audit").glob("*.json")) + list((d / "transcript").glob("*")):
            text = f.read_text(encoding="utf-8", errors="replace")
            if any(s in text for s in secrets if len(s) >= 6):
                errors.append(f"{r['run_id']}: credential value found in {f.relative_to(d)}")
            if f.suffix == ".json":
                try:
                    errors += _unredacted(json.loads(text), f"{r['run_id']}/{f.relative_to(d)}")
                except json.JSONDecodeError:
                    pass
    for r in runs.all_runs():
        comp = (r.get("agent") or {}).get("compliance")
        if comp and not comp["ok"] and r["manual"].get("compliance_reviewed") != "yes" and summary.final_status(r) != "Void":
            errors.append(f"{r['run_id']}: agent transcript flagged and not reviewed")
    errors += [f"results: {p}" for p in summary.check_anonymized(cfg)]
    return errors
