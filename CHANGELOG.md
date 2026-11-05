# Changelog

Bench repos pin a tag. A frozen bench (tagged `v1.0-prereg`) never moves to a newer benchkit tag mid-run.

## v0.1.0 (unreleased)

- Extracted from the Clay ease-of-use and pricing harness (methodology v0.5).
- Scorers moved out: each bench repo supplies `scoring/scorers.py` (`SCORERS`, `output_contacts`).
- Repo discovery: `BENCH_REPO`, `BENCH_ROOT`, `BENCH_CONFIG`; shared verifier cache via `BENCH_VERIFIER_CACHE`.
- `reset-check` works for any task with `crm_columns`, not just T3.
- Fully loaded cost projects from `scenario.projection_task` instead of T1.
- `summary_metrics` in the config adds bench-specific medians to `summary.csv`.
- New `bench check-anon`: fails if results name a labelled vendor or contain an email address.
- Scripted executor: provider adapters in the bench repo's `providers/`, `bench run` (resumable, `--limit` smoke tests recorded as Void), `bench list-providers`, redacted HTTP audit per row, automatic balance reads.
- `bench freeze` (run plan; config changes refused afterwards), `bench manifest` (file hashes), `bench audit`.
- Only `results/summary.csv` is published; `runs.csv` and `warnings.txt` moved to `private/`.
- `cost_per_1000_accepted_usd`, `median_latency_ms`, `endpoint` and `price_effective_date` in the tables.
- `bench` reads the bench repo's `.env` (existing environment variables win).
- Answer-key helpers in `benchkit.scoring`: `on_domain`, `accepted_email` (verifier-valid and on the company's domain), `domain_aliases`, `load_gold`, `gold_metrics` (precision and recall against known-correct emails and phones).

## Fixes before v1.0-prereg (2026-11-04, from the bloat audit)

- **Scoring:** `same()` compares whole words, so "CTO" no longer matches "Director", nor "AI" "Mailchimp". Answer-key values marked unknown or blank are excluded (`scoring.known`), including in `gold_metrics`.
- **Batch runs never pay twice:** `enrich_batch(rows, save, journal)` saves each row as it finishes and journals paid-for work in flight (`journal.json`).
- **Retries:** 429, 5xx and no-response failures are retried up to 3 attempts per row; cells record `attempts`, runs record `retried`. `StopRun` halts a run cleanly (e.g. out of credits).
- **No corrupted files:** run records, cells, the verifier cache and the journal are written to a temp file and then replaced (`paths.write_json`). The verifier cache merges what's on disk before saving, so concurrent benches don't drop each other's paid results.
- **Run plan publishes platform labels only** (it published config keys, which can name an anonymized vendor). `check-anon` now also scans published JSON and catches names inside IDs like `vendor_match`.
- `bench audit` checks credential-like keys only inside `headers` objects, so response fields such as `next_page_token` aren't flagged; secret values are still found anywhere.
- `median_latency_ms` is a true median. `http_json` turns timeouts and dropped connections into failed cells instead of crashing.
- `bench usage --unit` names the balance unit instead of recording zero. `verify --mode import` without `--email-results` gives an error, not a traceback. The platform lock is created atomically. `agent-batch` refuses tasks not in `agent.tasks`.
- Column `seat_cost_3_users_usd_month` renamed `seat_cost_usd_month` (seats come from `scenario.users`).
- Cuts: one module loader (`paths.load_module`) for scorers and providers, one secret-key list and redaction marker, one phone normalizer, `cfg_description` removed, roadmap moved out of the README.

## Anonymized vendors (2026-11-04)

- `private/platforms.json` overlay merged into the config per platform key (real names, prices, tiers, `anonymity_extra_terms`); its SHA-256 is frozen in `run-plan.json` as `overlay_sha256`, and runs are refused if it changes.
- `private/providers/` adapters join the endpoint roster, so vendor-identifying API code stays unpublished.
- `check-anon` also flags `anonymity_extra_terms` from the overlay.

## Apples-to-apples support (2026-11-04)

- **Cost from the first run per account:** `summary.cost_counted` keeps cost only for the first ranked run of each task version's input on each `account_id` (new manual field, default `shared`). Later runs keep steps, time and quality; `summary.csv` reports `cost_runs`, and the fully loaded projection uses counted runs only.
- **Operator profiles never pooled:** summary rows are split by `operator_profile`.
- **Freeze gate:** `prereg_required` in the config lists dotted paths (e.g. `disclosures.control_provider`); `bench freeze` refuses while any is blank.

## Rigor checks (2026-11-04)

- `bench sanity`: empty-output and oracle checks on the real answer keys, in a throwaway folder (helpers `sanity.write_rows`, `sanity.mark_valid`).
- Agent runs execute in a temp workspace outside the bench repo; transcript compliance check (`agent.compliance`, `bench compliance`) flags hosts outside `allowed_domains` and answer-key access; unreviewed flags fail `bench audit` and show in warnings. New manual field `compliance_reviewed`.
- Held-out split (`scoring.holdout_ids`, salted), `bench export-public` (refuses emails and vendor names), canary in the run plan and exports.
- Per-table usage can carry a balance reading; over 5% apart is a warning.
