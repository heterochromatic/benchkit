"""Fixture endpoints: deterministic fakes, no network."""
from benchkit.adapters import Adapter, StopRun, result


class FakeClay(Adapter):
    name = "fake_clay_batch"
    platform = "clay"
    docs_url = "https://example.com/docs/clay"

    retry_wait_s = 0
    started = []

    def enrich_batch(self, rows, save, journal):
        for r in rows:
            if r["input_id"] not in journal:
                FakeClay.started.append(r["input_id"])  # the paid step
                journal[r["input_id"]] = f"run-{r['input_id']}"
        for r in rows:
            save(r["input_id"], result({"email": f"p{r['input_id']}@example.com"}, latency_ms=100.0,
                                       audit={"request": {"headers": {"clay-api-key": "***REDACTED***"}}}))


class FakeAcme(Adapter):
    name = "fake_acme_match"
    platform = "acme"
    docs_url = "https://example.com/docs/acme"
    retry_wait_s = 0
    calls = 0
    _balance = 100.0
    flaky = set()      # input_ids that get one 429 before succeeding
    stop_at = None     # input_id that raises StopRun

    def enrich(self, row):
        if row["input_id"] == FakeAcme.stop_at:
            raise StopRun("out of credits")
        if row["input_id"] in FakeAcme.flaky:
            FakeAcme.flaky.discard(row["input_id"])
            return result(ok=False, status=429, error="rate limited")
        FakeAcme.calls += 1
        FakeAcme._balance -= 1
        email = "a@example.com" if row["input_id"] == "1" else ""
        return result({"email": email}, latency_ms=50.0, audit={"request": {"headers": {"x-api-key": "***REDACTED***"}}})

    def balance(self):
        return FakeAcme._balance


ADAPTERS = {a.name: a for a in (FakeClay(), FakeAcme())}
