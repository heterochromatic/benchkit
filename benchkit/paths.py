"""Filesystem layout.

A bench repo holds config/bench.json, scoring/scorers.py and tasks/. Its data (private/,
results/) sits in the same folder unless overridden.
- BENCH_REPO: the bench repo (default: nearest parent of the working directory with config/bench.json)
- BENCH_ROOT: where private/ and results/ live (default: BENCH_REPO)
- BENCH_CONFIG: config file (default: BENCH_REPO/config/bench.json)
- BENCH_VERIFIER_CACHE: share one verifier cache across benches, so an email is paid for once

Anonymized vendors: the committed config holds labels only. private/platforms.json (gitignored)
adds real names, prices and tiers per platform key, and private/providers/ adds their adapters.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path


class PathError(Exception):
    pass


def repo() -> Path:
    if os.environ.get("BENCH_REPO"):
        return Path(os.environ["BENCH_REPO"])
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / "config" / "bench.json").exists():
            return d
    raise PathError("Not inside a bench repo (no config/bench.json found); set BENCH_REPO")


def root() -> Path:
    return Path(os.environ.get("BENCH_ROOT") or repo())


def config_path() -> Path:
    return Path(os.environ.get("BENCH_CONFIG") or repo() / "config" / "bench.json")


def scorers_path() -> Path:
    return repo() / "scoring" / "scorers.py"


def private() -> Path:
    return root() / "private"


def datasets() -> Path:
    return private() / "datasets"


def runs() -> Path:
    return private() / "runs"


def locks() -> Path:
    return runs() / ".locks"


def results() -> Path:
    return root() / "results"


def verifier_cache() -> Path:
    return Path(os.environ.get("BENCH_VERIFIER_CACHE") or private() / "verifier-cache.json")


def env_file_keys() -> set:
    """Names of every variable in the bench repo's .env and .env.example (its secrets), whether or not they're set.
    .env.example covers a secret exported in the shell profile but never written to .env."""
    try:
        files = [repo() / ".env", repo() / ".env.example"]
    except PathError:
        return set()
    return {ln.partition("=")[0].strip() for f in files if f.exists() for ln in f.read_text(encoding="utf-8").splitlines()
            if "=" in ln and not ln.strip().startswith("#")}


def load_env():
    """Read KEY=VALUE lines from the bench repo's .env (gitignored). Existing variables win."""
    try:
        f = repo() / ".env"
    except PathError:
        return
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


def platform_overlay() -> Path:
    return private() / "platforms.json"


def _merge(base, extra):
    for k, v in extra.items():
        base[k] = _merge(base.get(k, {}), v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return base


def load_config() -> dict:
    """config/bench.json, with private/platforms.json merged in when present:
    {"platforms": {key: {...}}, "anonymity_extra_terms": [...]}."""
    with open(config_path(), encoding="utf-8") as f:
        cfg = json.load(f)
    ov = platform_overlay()
    if ov.exists():
        extra = json.loads(ov.read_text(encoding="utf-8"))
        unknown = set(extra.get("platforms", {})) - set(cfg["platforms"])
        if unknown:
            raise PathError(f"{ov} names platforms not in the config: {sorted(unknown)}")
        _merge(cfg, extra)
    return cfg


def write_json(path, payload, indent=1):
    """Write to a temp file, then replace, so a crash never leaves a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=indent, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)


_modules = {}


def load_module(path, name):
    """Import a bench repo file (scoring/scorers.py, providers/__init__.py) once per path."""
    path = Path(path)
    key = str(path.resolve())
    if key not in _modules:
        if not path.exists():
            raise PathError(f"Missing {path}")
        spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=
                                                      [str(path.parent)] if path.name == "__init__.py" else None)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        _modules[key] = mod
    return _modules[key]
