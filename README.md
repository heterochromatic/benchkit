# benchkit

Shared harness for The Prompting Company's GTM benchmarks. The public ones are [clay-waterfall-enrichment-benchmark](https://github.com/promptingcompany/clay-waterfall-enrichment-benchmark) and [clay-precall-briefs-benchmark](https://github.com/promptingcompany/clay-precall-briefs-benchmark); benchkit also runs a study that isn't published. Every benchmark repo pins a benchkit tag and supplies only what's specific to it.

| benchkit provides | Each bench repo provides |
|---|---|
| Run lifecycle (`new`, `start`, `stop`) with one lock per platform, so timed runs never overlap | `config/bench.json`: platforms, labels, tiers, unit prices, tasks, agent settings |
| Input checksums, so every run of a task version gets identical input | `scoring/scorers.py`: `SCORERS` and `output_contacts` |
| Usage attribution: per table or balance before/after | `tasks/`: versioned task specs |
| Shared verifier (ZeroBounce API or import), with a cache | `METHODOLOGY.md`, tagged `v1.0-prereg` before runs |
| Cost per accepted record, fully loaded monthly cost | |
| Scripted runs: direct API calls per endpoint, resumable, with redacted HTTP audit | `providers/`: one adapter per vendor endpoint (scripted benches) |
| Agent runner (Claude Code headless), credentials scoped to one platform | |
| Run plan (`freeze`), file hashes (`manifest`) and `audit` | |
| Published `results/summary.csv` only; per-run tables stay in `private/` | |

Python 3.10+, standard library only.

## Install

In a bench repo, either:

```bash
python3 -m venv .venv && .venv/bin/pip install "benchkit @ git+https://github.com/heterochromatic/benchkit@v0.1.3"
```

or keep a checkout of benchkit next to the bench repo. Each bench repo's `./bench` shim falls back to `../benchkit`.

## Paths

Run `bench` from anywhere inside a bench repo. It finds the nearest `config/bench.json`.

| Variable | Default | Use |
|---|---|---|
| `BENCH_REPO` | nearest parent with `config/bench.json` | the bench repo |
| `BENCH_ROOT` | `BENCH_REPO` | where `private/` and `results/` live |
| `BENCH_CONFIG` | `BENCH_REPO/config/bench.json` | alternative config (tests) |
| `BENCH_VERIFIER_CACHE` | `private/verifier-cache.json` | share one cache across benches, so each email is paid for once |

## Writing scorers

```python
# scoring/scorers.py
from benchkit.scoring import check_columns, email_ok, output_csv, rate, verifier

def score_w1(run, cfg):
    ...
    return {"records_in": n, "accepted_records": ok, "accepted_rate": rate(ok, n),
            "metrics": {...}, "floor_met": floor, "proposed_status": "Pass"}

SCORERS = {"W1": score_w1}

def output_contacts(run, cfg):
    return emails, phones
```

To add scorer metrics to `summary.csv` as medians, list them under `summary_metrics` in the config.

## Three ways to execute a run

| Executor | Who does the task | Steps and time | Use for |
|---|---|---|---|
| `human` | An operator in the vendor's web app | Counted from the recording | Ease-of-use benches |
| `agent` | Claude Code, headless, through the vendor's CLI or API | Tool calls, wall-clock | Agent track |
| `script` | A provider adapter calling the vendor API directly | Latency per request | Data-quality benches (most reproducible, cheapest) |

## Scripted runs

```python
# providers/__init__.py
from benchkit.adapters import Adapter, http_json, require_env, result

class ExampleEndpoint(Adapter):
    name = "example_lookup"          # stable endpoint ID
    platform = "example"             # key in config platforms
    docs_url = "https://example.com/docs"
    required_env = ("EXAMPLE_API_KEY",)

    def enrich(self, row):           # or enrich_batch(rows, save, journal) for batch APIs
        (key,) = require_env(*self.required_env)
        status, body, ms, audit = http_json("POST", "...", headers={"Authorization": f"Bearer {key}"}, body={...})
        return result({"email": ...}, ok=status == 200, status=status, latency_ms=ms, audit=audit)

ADAPTERS = {a.name: a for a in (ExampleEndpoint(),)}
```

```bash
bench list-providers
bench run --task W1 --env "self-serve monthly" --endpoints example_lookup --limit 5    # smoke test, recorded as Void
bench run --task W1 --env "self-serve monthly"                                       # every endpoint, one run each
bench run --resume R0007                                                             # finished rows are never re-called
```

Each row's result goes to `cells/<input_id>.json` as soon as it arrives, and its HTTP envelope (credential headers redacted) to `audit/<input_id>.json`.

- **Batch adapters** call `save(input_id, result)` as each row finishes, and record anything they've paid to start (such as a routine run ID) in `journal[input_id]`. A resumed run polls those instead of paying again.
- **Retries:** rate limits (429), server errors (5xx) and no-response failures are retried, up to 3 attempts per row in total, with `retry_wait_s` between passes. Every other failure is final. Each cell records its `attempts`. State this rule in the bench's methodology before freezing.
- **Stopping:** an adapter raises `StopRun` when carrying on is pointless, for example out of credits. The run stops; `bench run --resume` continues it.
- **Usage:** when an adapter implements `balance()`, usage is read before and after automatically.

## Rigor checks

```bash
bench sanity          # every task: empty output accepts nothing; the scorers' ORACLES (answer key as output) score as expected
bench export-public   # the scorers' PUBLIC_EXPORTS: non-personal key rows outside the held-out set, to datasets/public/
bench compliance R7   # re-check an agent transcript for hosts outside allowed_domains and any reach for answer keys
bench blind-sample --task T6   # mixed, shuffled review sheet with the platform hidden; then bench blind-import
```

- **Agent isolation:** agent runs execute in a temp folder outside the bench repo holding only their input; output and work are copied back. Keep `WebFetch`/`WebSearch` out of `agent.allowed_tools`; set `allowed_domains` per platform (competitors' in the private overlay) and `agent.allowed_domains_common`. Flagged runs need `--compliance_reviewed yes` or a Void status; `bench audit` fails otherwise.
- **Held-out split:** `holdout.share` in the config, `holdout.salt` in the private overlay; `scoring.holdout_ids(ids, cfg)`.
- **Canary:** `canary` in the config is written into `run-plan.json` and every public export.
- **Second credit reading:** per-table platforms can also record `--before/--after --unit <unit>`; a gap over 5% is a warning.

## Run plan, manifest and audit

```bash
bench freeze      # results/run-plan.json: config hash, dataset checksums, task versions, price date
bench manifest    # results/manifest.json: SHA-256 of every file in every run folder
bench audit       # hashes match, every run has a status, no credential survived, results clean
```

After `freeze`, any change to `config/bench.json` is refused until it's logged and re-frozen. `freeze` fails if a task's dataset is missing, and never overwrites an existing run plan: to re-freeze, delete `results/run-plan.json` first.

## Published vs private

| Published (`results/`) | Private (`private/`, gitignored) |
|---|---|
| `summary.csv`: medians per task × platform × endpoint, including `cost_per_1000_accepted_usd` and `price_effective_date` | `runs.csv`, `warnings.txt`, run folders, datasets, verifier cache |
| `run-plan.json`, `manifest.json` (hashes only) | |

## Anonymized vendors

The committed `config/bench.json` names an anonymized vendor only by its label (for example `"name": "Provider A", "label": "Provider A"`), with no prices, tier names or product details. Everything that identifies it lives in the gitignored `private/`:

- `private/platforms.json` is merged into the config per platform key: real `name`, `agent_interface`, `tiers`, `unit_prices` and price notes, plus `anonymity_extra_terms` (domains, product and tier names) for `check-anon`. An unknown platform key is an error.
- `private/providers/__init__.py` adds that vendor's adapters to the roster. They can import shared helpers from `bench_providers`.

`bench freeze` publishes the overlay's SHA-256 (`overlay_sha256`), and runs are refused if the overlay changes afterwards. `check-anon` and `audit` fail if a real name or an extra term appears in `results/`.

## Commands

Run `bench -h` for the full list and `bench <command> -h` for options.

## Tests

```bash
python3 -I -m unittest discover -s tests
```
