"""AI-agent executor. Runs Claude Code headless inside the run folder, records the
same lifecycle as a human run (start/stop/lock), counts tool calls as steps,
saves the transcript with credentials redacted."""
import json
import os
import subprocess
import time

from . import runs
from .adapters import REDACTED


def build_prompt(cfg, run) -> str:
    p = cfg["platforms"][run["platform"]]
    t = cfg["tasks"][run["task_id"]]
    fill = {
        "platform_name": p["name"],
        "agent_interface": p["agent_interface"],
        "credential_env": ", ".join(p["credential_env"]),
        "run_id": run["run_id"],
        "input": t["input"],
        "columns": ", ".join(t.get("output_columns", [])),
    }
    rules = "\n".join(f"- {r.format(**fill)}" for r in cfg["common_agent_rules"])
    return (
        f"Benchmark task {run['task_version']} (run {run['run_id']}).\n\n"
        f"{t['instruction'].format(**fill)}\n\n"
        f"Rules:\n{rules}\n\n"
        "When finished, reply with a short summary of what you did and anything you could not do."
    )


def _child_env(cfg, platform):
    """Pass only this platform's credentials; strip every other platform's and the verifier key."""
    env = dict(os.environ)
    blocked = {cfg["verifier"]["email_api_env"]}
    for name, p in cfg["platforms"].items():
        if name != platform:
            blocked.update(p["credential_env"])
    for k in blocked:
        env.pop(k, None)
    return env


def parse_stream(lines):
    """Count tool calls and pull the final result from stream-json output."""
    tool_calls, result = 0, {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_calls += 1
        elif ev.get("type") == "result":
            result = ev
    return tool_calls, result


def redact(text, secrets):
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, REDACTED)
    return text


def run_agent(cfg, run_id, dry_run=False) -> dict:
    run = runs.load(run_id)
    if run["executor"] != "agent":
        raise runs.RunError(f"{run_id} is a human run")
    a = cfg["agent"]
    prompt = build_prompt(cfg, run)
    d = runs.run_dir(run_id)
    (d / "transcript" / "prompt.txt").write_text(prompt, encoding="utf-8")
    cmd = [
        a["command"], "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--model", a["model"],
        "--allowedTools", *a["allowed_tools"],
        "--max-budget-usd", str(a["max_budget_usd"]),
        "--no-session-persistence", "--strict-mcp-config",
        "--setting-sources", "project",
    ]
    if dry_run:
        return {"cmd": cmd[:3] + ["<prompt>"] + cmd[3:], "prompt": prompt}

    env = _child_env(cfg, run["platform"])
    secrets = [os.environ.get(k) for k in cfg["platforms"][run["platform"]]["credential_env"]]
    runs.start(run_id)
    timed_out = False
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=d, env=env, capture_output=True, text=True,
                              timeout=a["timeout_min"] * 60)
        out, err, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        code = None
    finally:
        runs.stop(run_id)
    elapsed = round((time.monotonic() - t0) / 60, 2)

    (d / "transcript" / "stream.jsonl").write_text(redact(out, secrets), encoding="utf-8")
    (d / "transcript" / "stderr.txt").write_text(redact(err, secrets), encoding="utf-8")
    for f in (d / "work").rglob("*"):
        if f.is_file() and f.stat().st_size < 5_000_000:
            try:
                txt = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            clean = redact(txt, secrets)
            if clean != txt:
                f.write_text(clean, encoding="utf-8")

    steps, result = parse_stream(out.splitlines())
    run = runs.load(run_id)
    run["manual"].update({"steps": steps, "active_min": elapsed, "processing_min": 0.0})
    run["agent"] = {
        "model": a["model"], "exit_code": code, "timed_out": timed_out,
        "num_turns": result.get("num_turns"), "agent_cost_usd": result.get("total_cost_usd"),
        "is_error": result.get("is_error"), "duration_ms": result.get("duration_ms"),
        "not_supported_note": (d / "output" / "not_supported.md").exists(),
    }
    if timed_out:
        run["status"], run["status_source"] = "Did not finish", "agent-timeout"
    elif result.get("is_error") and steps == 0:
        # The agent never got going (auth, budget, CLI problem): a setup failure, not a product result.
        run["status"], run["status_source"] = "Void", "agent-setup-error"
        run["manual"]["failure_category"] = "setup"
        run["manual"]["notes"] = str(result.get("result") or err.strip())[:500]
    runs.save(run)
    return run
