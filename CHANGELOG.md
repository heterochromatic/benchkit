# Changelog

Bench repos pin a tag. A frozen bench (tagged `v1.0-prereg`) never moves to a newer benchkit tag mid-run.

## v0.1.3 (2026-11-19)

- `bench freeze` refuses to overwrite an existing `results/run-plan.json` (delete it to re-freeze), and fails if a task's dataset is missing. Before, it overwrote the plan and froze an empty checksum with a warning. `artifacts.freeze` now returns the plan only.
- `bench audit` reports an audit file that isn't valid JSON instead of skipping it.
- Version numbers in `pyproject.toml` and `benchkit.__version__` match the tag (they still said 0.1.0 in v0.1.1 and v0.1.2, so run plans and manifests from those tags record `benchkit_version` 0.1.0).
- The docs and the install line point at github.com/heterochromatic/benchkit, where this repository lives; the install line pins this tag.
- The README example adapter is a neutral `ExampleEndpoint`, and the command list points to `bench -h`.
- Error messages and docstrings no longer cite one benchmark's methodology section numbers.
- `bench usage --from-cli` help says what it records (the balance drop).

## v0.1.2 (2026-11-18)

- `blind-sample` puts a sheet ID in every item ID, so a filled sheet from an earlier sampling is refused on import instead of being remapped to other runs.
- Agent runs also withhold every secret named in the bench repo's `.env.example`, not only those in `.env`, so a key exported in the shell profile never reaches the agent.

## v0.1.1 (2026-11-18)

- Scripted runs: a smoke test resumed in full now re-measures balance usage and the stop time over the whole run (v0.1.0 kept the smoke test's).
- After a `StopRun` (e.g. out of credits), automatic balance usage is skipped and the operator records it with `bench usage --before/--after`, because a top-up can fall between the readings.
- Agent runs and scoring are unchanged.

## v0.1.0 (2026-11-10)

- The version that scored every published result in clay-waterfall-enrichment-benchmark and clay-precall-briefs-benchmark. Counted runs (7–10 November 2026) used commits from `c58a40e` to this tag; the changes in that span were verifier retries, Twilio error handling, agent key isolation and manifest file-name hashing. None changes the scoring rules, and every published score was recomputed at this tag.
- First release as its own package; before, the harness was part of one benchmark's repo.
- Each bench repo supplies its own `scoring/scorers.py` (`SCORERS`, `output_contacts`).
- `bench` finds the bench repo from `BENCH_REPO`, `BENCH_ROOT` and `BENCH_CONFIG`; `BENCH_VERIFIER_CACHE` shares one verifier cache.
- `reset-check` works for any task with `crm_columns`.
- Fully loaded cost projects from the task named in `scenario.projection_task`.
- `summary_metrics` in the config adds bench-specific medians to `summary.csv`.
- New `bench check-anon`: fails if results name a labelled vendor or contain an email address.
- Scripted runs: provider adapters in the bench repo's `providers/`, `bench run` (resumable; `--limit` smoke tests are recorded as Void), `bench list-providers`, a redacted HTTP audit per row and automatic balance reads.
- `bench freeze` (run plan; config changes refused afterwards), `bench manifest` (file hashes), `bench audit`.
- Only `results/summary.csv` is published; `runs.csv` and `warnings.txt` moved to `private/`.
- New table columns: `cost_per_1000_accepted_usd`, `median_latency_ms`, `endpoint` and `price_effective_date`.
- `bench` reads the bench repo's `.env` (existing environment variables win).
- Answer-key helpers in `benchkit.scoring`: `on_domain`, `accepted_email` (verifier-valid and on the company's domain), `domain_aliases`, `load_gold` and `gold_metrics` (precision and recall against known-correct emails and phones).

## Fixes before v1.0-prereg (2026-11-04)

- `same()` compares whole words, so "CTO" no longer matches "Director", nor "AI" "Mailchimp".
- Answer-key values marked unknown or blank are left out of scoring (`scoring.known`), including in `gold_metrics`.
- Batch runs never pay twice: `enrich_batch(rows, save, journal)` saves each row as it finishes and records paid-for work in flight in `journal.json`.
- 429, 5xx and no-response failures are retried, up to 3 attempts per row; cells record `attempts`, runs record `retried`.
- `StopRun` halts a run cleanly (e.g. out of credits).
- Run records, cells, the verifier cache and the journal are written to a temp file and then replaced (`paths.write_json`), so a crash can't leave a half-written file.
- The verifier cache merges what's on disk before saving, so benches running at the same time keep each other's paid results.
- The run plan publishes platform labels only; it used to publish config keys, which can name an anonymized vendor.
- `check-anon` also scans published JSON and catches names inside IDs like `vendor_match`.
- `bench audit` looks for credential-like keys only inside `headers` objects, so response fields such as `next_page_token` aren't flagged; secret values are still found anywhere.
- `median_latency_ms` is a true median.
- `http_json` turns timeouts and dropped connections into failed cells instead of crashing.
- `bench usage --unit` names the balance unit instead of recording zero.
- `verify --mode import` without `--email-results` gives an error, not a traceback.
- The platform lock is created atomically.
- `agent-batch` refuses tasks not in `agent.tasks`.
- Column `seat_cost_3_users_usd_month` renamed `seat_cost_usd_month` (seats come from `scenario.users`).
- One module loader (`paths.load_module`) for scorers and providers, one secret-key list, one phone normalizer; `cfg_description` removed.

## Anonymized vendors (2026-11-04)

- `private/platforms.json` is merged into the config per platform key (real names, prices, tiers, `anonymity_extra_terms`).
- Its SHA-256 is frozen in `run-plan.json` as `overlay_sha256`, and runs are refused if it changes.
- Adapters in `private/providers/` join the endpoint roster, so vendor-identifying API code stays unpublished.
- `check-anon` also flags `anonymity_extra_terms` from the overlay.

## Apples-to-apples support (2026-11-04)

- Cost is counted only for the first ranked run of each task version's input on each `account_id` (new manual field, default `shared`); later runs keep steps, time and quality.
- `summary.csv` reports `cost_runs`, and the fully loaded projection uses counted runs only.
- Summary rows are split by `operator_profile`, so operator profiles are never pooled.
- `bench freeze` refuses while any config path listed in `prereg_required` (e.g. `disclosures.control_provider`) is blank.

## Rigor checks (2026-11-04)

- `bench sanity`: empty-output and oracle checks on the real answer keys, in a throwaway folder (helpers `sanity.write_rows`, `sanity.mark_valid`).
- Agent runs execute in a temp workspace outside the bench repo.
- `bench compliance` flags transcript hosts outside `allowed_domains` and answer-key access; unreviewed flags fail `bench audit` and show in warnings (new manual field `compliance_reviewed`).
- Held-out split (`scoring.holdout_ids`, salted).
- `bench export-public` refuses emails and vendor names.
- A canary is written into the run plan and exports.
- Per-table usage can carry a balance reading; over 5% apart is a warning.
- New operator profile `solo` for a single operator's illustrative walkthroughs (kept in its own summary rows).
- `bench blind-sample` / `bench blind-import`: mixed, shuffled review sheets with the platform hidden, for tasks with a `review` config.
- 2026-11-06: `bench verify --phone-mode twilio` checks phones with Twilio Lookup (line type), cached per number.
- 2026-11-06: agent prompts fill any scalar task field (e.g. `{phone_rows}`).
- 2026-11-07: platforms with `cli_usage` get a wrapper on the agent's PATH that keeps every CLI call's output; costs are summed from configured keys, the balance is read before and after the run, and `bench usage --from-cli` records it.
