"""AI-agent executor. Runs Claude Code headless in a throwaway workspace outside the bench repo
(so answer keys aren't reachable by relative path), records the same lifecycle as a human run
(start/stop/lock), counts tool calls as steps, saves the transcript with credentials redacted,
and checks the transcript for sources other than the platform."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import paths, runs
from .adapters import REDACTED


def build_prompt(cfg, run) -> str:
    p = cfg["platforms"][run["platform"]]
    t = cfg["tasks"][run["task_id"]]
    fill = {
        **{k: v for k, v in t.items() if isinstance(v, (str, int)) and k != "instruction"},   # e.g. {phone_rows}
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
    """Pass only this platform's credentials; strip every other secret: other platforms' keys, the verifier
    key, and anything else in the repo's .env (e.g. the CRM sandbox token, which the agent must reach
    through the platform, not directly)."""
    env = dict(os.environ)
    blocked = {cfg["verifier"]["email_api_env"]} | paths.env_file_keys()
    for name, p in cfg["platforms"].items():
        if name != platform:
            blocked.update(p["credential_env"])
    blocked -= set(cfg["platforms"][platform]["credential_env"])   # a key two platforms share stays
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


URL_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")
KEY_HINTS = ("ground-truth", "-gold", "answer_key", "answer-key", "private/datasets", "verifier-cache")


def tool_inputs(lines):
    """Every tool call's input, as (tool name, flattened text)."""
    for line in lines:
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if ev.get("type") != "assistant":
            continue
        for block in (ev.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block.get("name", ""), json.dumps(block.get("input") or {})


def compliance(cfg, platform, lines):
    """Flag hosts outside the platform's allowed domains, and any reach for answer keys or the
    bench repo. A flagged run is reviewed by a person before it can be ranked."""
    allowed = cfg["platforms"][platform].get("allowed_domains", []) + cfg["agent"].get("allowed_domains_common", [])
    root = str(paths.root())
    flags, hosts = [], set()
    for tool, text in tool_inputs(lines):
        for host in URL_HOST.findall(text):
            host = host.lower().rstrip(".")
            hosts.add(host)
            if not any(host == d or host.endswith("." + d) for d in allowed):
                flags.append(f"{tool}: request to {host}")
        if root in text or any(h in text for h in KEY_HINTS):
            flags.append(f"{tool}: touches the bench repo or an answer key")
        if tool in ("WebSearch", "WebFetch"):
            flags.append(f"{tool}: web tool used")
    return {"ok": not flags, "flags": sorted(set(flags))[:50], "hosts": sorted(hosts)}


def recheck(cfg, run_id):
    run = runs.load(run_id)
    stream = runs.run_dir(run_id) / "transcript" / "stream.jsonl"
    if not stream.exists():
        raise runs.RunError(f"{run_id} has no transcript")
    run.setdefault("agent", {})["compliance"] = compliance(cfg, run["platform"], stream.read_text(encoding="utf-8").splitlines())
    runs.save(run)
    return run


SEPARATOR = "### benchkit-cli-call ###"


def cli_shim(command, log_path):
    """A wrapper dir to put first on the agent's PATH: it runs the real `command` unchanged and appends a
    copy of each call's output to log_path, so the platform's own per-call cost reports are kept whatever
    the agent saves. shortcut: calls by absolute path or straight to the REST API aren't logged; the
    balance reading catches that gap."""
    real = shutil.which(command)
    if not real:
        return None
    d = tempfile.mkdtemp(prefix="bench-shim-")
    script = Path(d) / command
    script.write_text(f"""#!/bin/bash
set -o pipefail
printf '%s\\n' '{SEPARATOR}' >> '{log_path}'
'{real}' "$@" | tee -a '{log_path}'
""", encoding="utf-8")
    script.chmod(0o755)
    return d


def _dig(obj, dotted):
    for part in dotted.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def cli_cost(text, cost_keys):
    """Sum the cost fields of every logged call: {"total", "calls", "unparsed"}. A call's keys are looked
    up at the top level and under "metadata" (where Clay's CLI reports credit usage)."""
    total, calls, unparsed = 0.0, 0, 0
    for chunk in text.split(SEPARATOR)[1:]:
        chunk = chunk.strip()
        if not chunk:
            continue
        calls += 1
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            unparsed += "credit" in chunk.lower() and "Usage:" not in chunk   # help text carries no cost
            continue
        for key in cost_keys:
            v = _dig(obj, key)
            if v is None and isinstance(obj, dict):
                v = _dig(obj.get("metadata") or {}, key)
            if isinstance(v, (int, float)):
                total += v
    return {"total": round(total, 4), "calls": calls, "unparsed": unparsed}


def read_balance(usage_cfg):
    """The platform's credit balance from its CLI (e.g. `clay credits balance`), or None if unreadable."""
    cmd = usage_cfg.get("balance_command")
    if not cmd:
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
        v = _dig(json.loads(out), usage_cfg.get("balance_path", "balance"))
        return float(v) if isinstance(v, (int, float)) else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


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
        "--disallowedTools", "WebFetch", "WebSearch",
        "--max-budget-usd", str(a["max_budget_usd"]),
        "--no-session-persistence", "--strict-mcp-config",
        "--setting-sources", "project",
    ]
    if dry_run:
        return {"cmd": cmd[:3] + ["<prompt>"] + cmd[3:], "prompt": prompt}

    env = _child_env(cfg, run["platform"])
    secrets = [os.environ.get(k) for k in cfg["platforms"][run["platform"]]["credential_env"]]
    usage_cfg = cfg["platforms"][run["platform"]].get("cli_usage")
    shim_dir = cli_log = None
    if usage_cfg:
        cli_log = Path(tempfile.mkdtemp(prefix="bench-cli-log-")) / f"{usage_cfg['command']}.log"
        cli_log.touch()
        shim_dir = cli_shim(usage_cfg["command"], cli_log)
        if shim_dir:
            env["PATH"] = shim_dir + os.pathsep + env.get("PATH", "")
    # shortcut: a temp folder blocks relative paths to the answer keys, not an absolute-path search;
    # the compliance check flags that, and a container (Harbor) closes it fully.
    ws = tempfile.mkdtemp(prefix="bench-agent-")
    shutil.copytree(d / "input", f"{ws}/input")
    for sub in ("output", "work"):
        os.makedirs(f"{ws}/{sub}", exist_ok=True)
    balance_before = read_balance(usage_cfg) if usage_cfg else None
    runs.start(run_id)
    timed_out = False
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=ws, env=env, capture_output=True, text=True,
                              timeout=a["timeout_min"] * 60)
        out, err, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        code = None
    finally:
        runs.stop(run_id)
        for sub in ("output", "work"):
            shutil.rmtree(d / sub, ignore_errors=True)
            shutil.copytree(f"{ws}/{sub}", d / sub)
        shutil.rmtree(ws, ignore_errors=True)
        if shim_dir:
            shutil.rmtree(shim_dir, ignore_errors=True)
    elapsed = round((time.monotonic() - t0) / 60, 2)
    cli_usage = None
    if cli_log and cli_log.exists():
        log = redact(cli_log.read_text(encoding="utf-8", errors="replace"), secrets)
        (d / "usage").mkdir(exist_ok=True)
        (d / "usage" / cli_log.name).write_text(log, encoding="utf-8")
        cli_usage = {**cli_cost(log, usage_cfg["cost_keys"]), "unit": usage_cfg.get("unit", "credits")}
        shutil.rmtree(cli_log.parent, ignore_errors=True)
    if usage_cfg and balance_before is not None:
        time.sleep(usage_cfg.get("balance_settle_s", 60))   # charges post a little after the work finishes
        after = read_balance(usage_cfg)
        cli_usage = {**(cli_usage or {}), "balance_before": balance_before, "balance_after": after,
                     "balance_drop": None if after is None else round(balance_before - after, 4)}

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
        "compliance": compliance(cfg, run["platform"], out.splitlines()),
        "cli_usage": cli_usage,
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
