"""benchkit tests on a fixture bench (tests/fixture_bench). Run:
    python3 -I -m unittest discover -s tests
"""
import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixture_bench"
sys.path.insert(0, str(REPO))

from benchkit import adapters, agent, artifacts, cli, costs, paths, review, runs, sanity, scoring, summary, verify  # noqa: E402


def write_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


class BenchkitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["BENCH_REPO"] = str(FIXTURE)
        os.environ["BENCH_ROOT"] = str(self.root)
        os.environ.pop("BENCH_CONFIG", None)
        self.cfg = paths.load_config()
        write_csv(paths.datasets() / "x1-input.csv", ["input_id", "name", "domain"],
                  [[str(i), f"P{i}", "example.com"] for i in range(1, 4)])

    def tearDown(self):
        self.tmp.cleanup()
        for k in ("BENCH_REPO", "BENCH_ROOT"):
            os.environ.pop(k, None)

    def _output(self, run_id):
        write_csv(runs.run_dir(run_id) / "output" / "output.csv", ["input_id", "email"],
                  [["1", "a@example.com"], ["2", "b@example.com"], ["3", ""]])
        write_csv(self.root / "zb.csv", ["email", "status"], [["a@example.com", "valid"], ["b@example.com", "invalid"]])

    def test_lifecycle_plugin_scoring_and_summary(self):
        r = runs.new_run(self.cfg, "X1", "acme", "human", "OP01", "first-timer", "self-serve monthly", 1)
        runs.start(r["run_id"])
        runs.stop(r["run_id"])
        runs.set_usage(r["run_id"], before=100, after=90)
        runs.record(r["run_id"], {"step_counter_1": 10, "step_counter_2": 10, "active_min": 5})
        self._output(r["run_id"])
        cli.main(["verify", r["run_id"], "--mode", "import", "--email-results", str(self.root / "zb.csv")])
        cli.main(["score", r["run_id"]])
        run = runs.load(r["run_id"])
        self.assertEqual(run["score"]["accepted_records"], 1)
        self.assertEqual(run["score"]["proposed_status"], "Partial")
        self.assertAlmostEqual(costs.run_costs(self.cfg, run)["cost_per_accepted_record_usd"], 0.2)
        _, summ, _ = summary.write_all(self.cfg)
        self.assertEqual(summ[0]["email_fill_rate_median"], 0.67)
        cfg2 = dict(self.cfg, summary_metrics={"X1": ["email_*"]})
        _, summ2, _ = summary.write_all(cfg2)
        self.assertEqual(summ2[0]["email_fill_rate_median"], 0.67)
        self.assertEqual(summary.check_anonymized(self.cfg), [])
        self.assertNotIn("basic", (paths.private() / "runs.csv").read_text())
        self.assertFalse((paths.results() / "runs.csv").exists())

    def test_scripted_runs_resume_freeze_manifest_audit(self):
        cli.main(["freeze"])
        cli.main(["run", "--task", "X1", "--env", "self-serve monthly", "--endpoints", "fake_clay_batch,fake_acme_match"])
        clay, acme = runs.all_runs()
        self.assertEqual((clay["endpoint"], clay["executor"], clay["operator_profile"]),
                         ("fake_clay_batch", "script", "script"))
        self.assertTrue((runs.run_dir(acme["run_id"]) / "output" / "output.csv").exists())
        self.assertEqual(acme["usage_units"], {"credits": 3.0})
        self.assertEqual(acme["script"]["median_latency_ms"], 50.0)
        calls = adapters.registry()["fake_acme_match"].calls
        cli.main(["run", "--resume", acme["run_id"]])
        self.assertEqual(adapters.registry()["fake_acme_match"].calls, calls)
        for r in runs.all_runs():
            runs.record(r["run_id"], {}, status="Not comparable")
        cli.main(["manifest"])
        self.assertEqual(artifacts.audit(self.cfg), [])
        (runs.run_dir(acme["run_id"]) / "cells" / "1.json").write_text("{}")
        self.assertTrue(any("hash mismatch" in e for e in artifacts.audit(self.cfg)))
        p = runs.run_dir(acme["run_id"]) / "audit" / "2.json"
        p.write_text('{"request": {"headers": {"x-api-key": "live-key-123456"}}, "response": {"body": {"next_page_token": "abc"}}}')
        errs = [e for e in artifacts.audit(self.cfg) if "unredacted" in e]
        self.assertEqual(len(errs), 1)

    def test_freeze_refuses_changed_config(self):
        cfg_copy = self.root / "cfg.json"
        cfg_copy.write_text((FIXTURE / "config" / "bench.json").read_text())
        os.environ["BENCH_CONFIG"] = str(cfg_copy)
        try:
            cli.main(["freeze"])
            cfg_copy.write_text(cfg_copy.read_text().replace('"rows": 3', '"rows": 4'))
            with self.assertRaises(runs.RunError):
                runs.new_run(paths.load_config(), "X1", "clay", "human", "OP01", "first-timer", "self-serve monthly")
        finally:
            os.environ.pop("BENCH_CONFIG", None)

    def test_smoke_limit_is_void(self):
        cli.main(["run", "--task", "X1", "--env", "self-serve monthly", "--endpoints", "fake_clay_batch", "--limit", "1"])
        r = runs.all_runs()[0]
        self.assertEqual((r["status"], r["manual"]["failure_category"]), ("Void", "setup"))

    def test_env_file_does_not_override(self):
        envf = self.root / "repo"
        (envf / "config").mkdir(parents=True)
        (envf / "config" / "bench.json").write_text("{}")
        (envf / ".env").write_text("# keys\nBK_TEST_A=from-file\nBK_TEST_B='quoted'\nBK_TEST_EMPTY=\n")
        os.environ["BENCH_REPO"] = str(envf)
        os.environ["BK_TEST_A"] = "from-shell"
        try:
            paths.load_env()
            self.assertEqual((os.environ["BK_TEST_A"], os.environ["BK_TEST_B"]), ("from-shell", "quoted"))
            self.assertNotIn("BK_TEST_EMPTY", os.environ)
        finally:
            for k in ("BK_TEST_A", "BK_TEST_B"):
                os.environ.pop(k, None)

    def test_domain_check_and_gold_metrics(self):
        write_csv(paths.datasets() / "email-domain-aliases.csv", ["domain", "email_domain"], [["acme.com", "acmecorp.io"]])
        al = scoring.domain_aliases()
        self.assertTrue(scoring.on_domain("a@acme.com", "www.acme.com", al))
        self.assertTrue(scoring.on_domain("a@eu.acme.com", "acme.com", al))
        self.assertTrue(scoring.on_domain("a@acmecorp.io", "acme.com", al))
        self.assertFalse(scoring.on_domain("a@gmail.com", "acme.com", al))
        out = {"1": {"email": "ann@acme.com", "phone": "+1 (555) 000-0001"},
               "2": {"email": "wrong@acme.com", "phone": ""}, "3": {"email": "", "phone": ""}}
        gold = [{"input_id": "1", "known_email": "Ann@acme.com", "known_phone": "555-000-0001"},
                {"input_id": "2", "known_email": "bo@acme.com", "known_phone": ""},
                {"input_id": "3", "known_email": "cy@acme.com", "known_phone": ""}]
        m = scoring.gold_metrics(out, gold)
        self.assertEqual((m["gold_email_precision"], m["gold_email_recall"], m["gold_email_wrong"]), (0.5, 0.3333, 1))
        self.assertEqual((m["gold_phone_rows"], m["gold_phone_precision"]), (1, 1.0))

    def test_same_matches_whole_words_only(self):
        self.assertFalse(scoring.same("CTO", "Director"))
        self.assertFalse(scoring.same("VP", "SVP Sales"))
        self.assertFalse(scoring.same("AI", "Mailchimp"))
        self.assertTrue(scoring.same("Delta Inc", "delta"))
        self.assertTrue(scoring.same("Acme", "Acme Software"))
        self.assertFalse(scoring.same("", "Acme"))

    def test_gold_unknown_rows_excluded(self):
        gold = [{"input_id": "1", "known_email": "unknown", "known_phone": ""},
                {"input_id": "2", "known_email": "b@acme.com", "known_phone": ""}]
        m = scoring.gold_metrics({"1": {"email": "x@acme.com"}, "2": {"email": "b@acme.com"}}, gold)
        self.assertEqual((m["gold_email_rows"], m["gold_email_precision"]), (1, 1.0))

    def test_transient_failures_retried_and_batch_never_repays(self):
        reg = adapters.registry()
        type(reg["fake_acme_match"]).flaky = {"2"}
        cli.main(["run", "--task", "X1", "--env", "self-serve monthly", "--endpoints", "fake_acme_match"])
        r = runs.all_runs()[0]
        cell = json.loads((runs.run_dir(r["run_id"]) / "cells" / "2.json").read_text())
        self.assertEqual((cell["ok"], cell["attempts"]), (True, 2))
        self.assertEqual(r["script"]["retried"], 1)
        # Batch: a crash after paying to start must not pay again on resume.
        fake = type(reg["fake_clay_batch"])
        fake.started = []
        b = runs.new_run(self.cfg, "X1", "clay", "script", "s", "script", "self-serve monthly", endpoint="fake_clay_batch")
        adapters.Journal(runs.run_dir(b["run_id"]) / "journal.json")["1"] = "run-1"   # started before a crash
        adapters.run_endpoint(self.cfg, b["run_id"], log=lambda m: None)
        self.assertEqual(fake.started, ["2", "3"])

    def test_stoprun_halts_and_resumes(self):
        acme = type(adapters.registry()["fake_acme_match"])
        acme.stop_at = "2"
        try:
            with self.assertRaises(SystemExit):
                cli.main(["run", "--task", "X1", "--env", "self-serve monthly", "--endpoints", "fake_acme_match"])
            rid = runs.all_runs()[0]["run_id"]
            self.assertEqual(sorted(p.stem for p in (runs.run_dir(rid) / "cells").glob("*.json")), ["1"])
            acme.stop_at = None
            cli.main(["run", "--resume", rid])
            self.assertTrue((runs.run_dir(rid) / "output" / "output.csv").exists())
        finally:
            acme.stop_at = None

    def test_usage_unit_cache_merge_and_import_guard(self):
        r = runs.new_run(self.cfg, "X1", "acme", "human", "OP01", "first-timer", "self-serve monthly")
        cli.main(["usage", r["run_id"], "--before", "50", "--after", "40", "--unit", "export_credits"])
        self.assertEqual(runs.load(r["run_id"])["usage_units"], {"export_credits": 10.0})
        verify._save_cache({"email": {"a@example.com": "valid"}, "phone": {}})
        paths.write_json(paths.verifier_cache(), {"email": {"a@example.com": "valid", "b@example.com": "invalid"}, "phone": {}})
        verify._save_cache({"email": {"c@example.com": "valid"}, "phone": {}})
        self.assertEqual(len(verify._load_cache()["email"]), 3)
        with self.assertRaises(verify.VerifyError):
            verify.verify_emails(["a@example.com"], "import", self.cfg)

    def test_agent_batch_respects_agent_tasks(self):
        cfg = dict(self.cfg, agent=dict(self.cfg["agent"], tasks=[]))
        with self.assertRaises(runs.RunError):
            cli.cmd_agent_batch(cfg, type("A", (), {"task": "X1"})())

    def test_cost_counts_first_run_per_account_and_profiles_split(self):
        ids = []
        for op, profile, acct in (("OP01", "first-timer", None), ("OP02", "first-timer", None),
                                  ("OP03", "first-timer", "acme-2"), ("PR01", "practitioner", None)):
            r = runs.new_run(self.cfg, "X1", "acme", "human", op, profile, "self-serve monthly")
            runs.start(r["run_id"]); runs.stop(r["run_id"])
            runs.set_usage(r["run_id"], before=100, after=90)
            runs.record(r["run_id"], {"account_id": acct} if acct else {}, status="Pass")
            ids.append(r["run_id"])
        counted = summary.cost_counted(runs.all_runs())
        self.assertEqual(counted, {ids[0], ids[2]})   # first on the shared account, first on acme-2
        _, summ, _ = summary.write_all(self.cfg)
        by_profile = {rec["operator_profile"]: rec for rec in summ}
        self.assertEqual((by_profile["first-timer"]["runs"], by_profile["first-timer"]["cost_runs"]), (3, 2))
        self.assertEqual(by_profile["practitioner"]["cost_runs"], 0)

    def test_freeze_refuses_undecided(self):
        cfg = dict(self.cfg, prereg_required=["disclosures.control_provider", "scenario.users"], disclosures={})
        with self.assertRaises(runs.RunError) as e:
            artifacts.freeze(cfg)
        self.assertIn("disclosures.control_provider", str(e.exception))
        self.assertNotIn("scenario.users", str(e.exception))

    def test_agent_runs_outside_repo_and_compliance_flags(self):
        script = self.root / "fake-claude"
        script.write_text("#!/bin/sh\n"
                          "pwd > output/cwd.txt\n"
                          "echo 'input_id,email' > output/output.csv\n"
                          "echo '{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"tool_use\",\"name\":\"Bash\","
                          "\"input\":{\"command\":\"curl https://api.clay.com/x; curl https://lookup.example.net/p; cat ../../datasets/t5-ground-truth.csv\"}}]}}'\n"
                          "echo '{\"type\":\"result\",\"total_cost_usd\":0.1}'\n")
        script.chmod(0o755)
        cfg = dict(self.cfg, agent=dict(self.cfg["agent"], command=str(script)))
        r = runs.new_run(cfg, "X1", "clay", "agent", "agent-x", "agent", "self-serve monthly")
        run = agent.run_agent(cfg, r["run_id"])
        d = runs.run_dir(r["run_id"])
        self.assertTrue((d / "output" / "output.csv").exists())
        self.assertNotIn(str(self.root), (d / "output" / "cwd.txt").read_text())
        c = run["agent"]["compliance"]
        self.assertFalse(c["ok"])
        self.assertTrue(any("lookup.example.net" in f for f in c["flags"]))
        self.assertTrue(any("answer key" in f for f in c["flags"]))
        self.assertFalse(any("api.clay.com" in f for f in c["flags"]))
        runs.record(r["run_id"], {}, status="Pass")
        cli.main(["manifest"])
        self.assertTrue(any("flagged and not reviewed" in e for e in artifacts.audit(cfg)))
        runs.record(r["run_id"], {"compliance_reviewed": "yes"})
        self.assertFalse(any("flagged" in e for e in artifacts.audit(cfg)))

    def test_sanity_empty_and_oracle(self):
        rows = sanity.run_sanity(self.cfg)
        self.assertIn(("X1", "empty", True, "accepted 0"), rows)
        self.assertIn(("X1", "oracle", True, "all keyed metrics match"), rows)
        self.assertEqual(runs.all_runs(), [])   # nothing left in private/runs

    def test_twilio_phone_check_caches_and_maps_line_types(self):
        from unittest import mock
        self.assertEqual((verify.e164("(415) 555-0100"), verify.e164("+44 20 7946 0000"), verify.e164("14155550100")),
                         ("+14155550100", "+442079460000", "+14155550100"))
        calls = []

        def fake(phone, sid, secret, timeout=30):
            calls.append(phone)
            return {"+14155550100": "mobile", "4155550199": "landline"}.get(phone, "invalid")
        with mock.patch.dict(os.environ, {"TWILIO_API_KEY_SID": "SKx", "TWILIO_API_KEY_SECRET": "s"}), \
                mock.patch.object(verify, "twilio_line_type", side_effect=fake):
            got = verify.verify_phones(["+1 415 555 0100", "415-555-0199", "+1 415 555 0100"], mode="twilio", pause_s=0)
            again = verify.verify_phones(["+1 415 555 0100"], mode="twilio", pause_s=0)
        self.assertEqual(got, {"+14155550100": "mobile", "4155550199": "landline"})
        self.assertEqual(again, {"+14155550100": "mobile"})
        self.assertEqual(len(calls), 2)   # the repeat and the second call came from the cache, not Twilio

    def test_holdout_and_public_export(self):
        ids = [str(i) for i in range(200)]
        held = scoring.holdout_ids(ids, self.cfg)
        self.assertTrue(60 < len(held) < 140)
        self.assertEqual(held, scoring.holdout_ids(ids, self.cfg))
        self.assertEqual(scoring.holdout_ids(ids, dict(self.cfg, holdout={"share": 0})), set())
        out = self.root / "repo-copy"
        import shutil
        shutil.copytree(FIXTURE, out)
        os.environ["BENCH_REPO"] = str(out)
        try:
            [(f, n)] = artifacts.export_public(self.cfg)
            self.assertEqual(n, 3 - len(scoring.holdout_ids(["1", "2", "3"], self.cfg)))
            self.assertIn("bench-canary", f.read_text())
        finally:
            os.environ["BENCH_REPO"] = str(FIXTURE)

    def test_second_credit_reading(self):
        r = runs.new_run(self.cfg, "X1", "clay", "human", "OP01", "first-timer", "self-serve monthly")
        runs.set_usage(r["run_id"], before=1000, after=880, units={"data_credits": 100.0}, unit="data_credits")
        runs.record(r["run_id"], {}, status="Pass")
        self.assertTrue(any("differ by more than 5%" in w for w in summary.warnings_for(self.cfg, runs.all_runs())))

    def test_blind_review_round_trip(self):
        ids = []
        for plat in ("clay", "acme"):
            r = runs.new_run(self.cfg, "X1", plat, "human", "you", "solo", "self-serve monthly")
            write_csv(runs.run_dir(r["run_id"]) / "output" / "output.csv", ["input_id", "email", "domain"],
                      [["1", "a@example.com", "a.example.com"], ["2", "b@example.com", "b.example.com"], ["3", "", "c.example.com"]])
            ids.append(r["run_id"])
        sheet, n = review.blind_sample(self.cfg, "X1")
        self.assertEqual(n, 4)
        text = sheet.read_text()
        self.assertNotIn("R000", text)
        self.assertNotIn("clay", text.lower())
        _, rows = scoring.read_csv(sheet)
        filled = self.root / "filled.csv"
        write_csv(filled, list(rows[0].keys()), [[*list(r.values())[:-1], "Y"] for r in rows])
        counts = review.blind_import(self.cfg, "X1", filled)
        self.assertEqual(counts, {ids[0]: 2, ids[1]: 2})
        _, back = scoring.read_csv(runs.run_dir(ids[0]) / "review" / "sample.csv")
        self.assertEqual({r["matches"] for r in back}, {"Y"})

    def test_check_anon_catches_names_and_emails(self):
        p = self.root / "leak.csv"
        p.write_text("platform,contact\nAcmedata,x@example.com\n")
        problems = summary.check_anonymized(self.cfg, [p])
        self.assertEqual(len(problems), 2)
        q = self.root / "ids.json"
        q.write_text('{"endpoint": "acmedata_match"}')
        self.assertEqual(len(summary.check_anonymized(self.cfg, [q])), 1)

    def test_private_overlay_providers_and_freeze(self):
        self.assertEqual(paths.load_config(), self.cfg)  # no overlay: config unchanged
        ov = paths.platform_overlay()
        paths.write_json(ov, {"platforms": {"quoteco": {"name": "Realname", "unit_prices": {"credits": 0.5}}},
                              "anonymity_extra_terms": ["realname.io"]})
        cfg = paths.load_config()
        self.assertEqual((cfg["platforms"]["quoteco"]["name"], cfg["platforms"]["quoteco"]["label"]), ("Realname", "Vendor B"))
        self.assertEqual(cfg["platforms"]["quoteco"]["unit_prices"]["credits"], 0.5)
        leak = self.root / "leak.json"
        leak.write_text('{"note": "see docs.realname.io", "endpoint": "realname_match"}')
        self.assertEqual(len(summary.check_anonymized(cfg, [leak])), 2)
        priv = paths.private() / "providers"
        priv.mkdir(parents=True)
        (priv / "__init__.py").write_text(
            "from benchkit.adapters import Adapter\n"
            "class P(Adapter):\n    name = 'secret_endpoint'\n    platform = 'quoteco'\n"
            "ADAPTERS = {'secret_endpoint': P()}\n")
        self.assertIn("secret_endpoint", adapters.registry())
        self.assertIn("fake_acme_match", adapters.registry())
        cli.main(["freeze"])
        runs.new_run(cfg, "X1", "clay", "human", "OP01", "first-timer", "self-serve monthly")
        paths.write_json(ov, {"platforms": {"quoteco": {"unit_prices": {"credits": 0.1}}}})
        with self.assertRaises(runs.RunError):
            runs.new_run(paths.load_config(), "X1", "clay", "human", "OP01", "first-timer", "self-serve monthly")
        paths.write_json(ov, {"platforms": {"nope": {}}})
        with self.assertRaises(paths.PathError):
            paths.load_config()

    def test_locks_checksum_and_quote(self):
        a = runs.new_run(self.cfg, "X1", "acme", "human", "OP01", "first-timer", "self-serve monthly")
        b = runs.new_run(self.cfg, "X1", "acme", "human", "OP02", "first-timer", "self-serve monthly")
        runs.start(a["run_id"])
        with self.assertRaises(runs.RunError):
            runs.start(b["run_id"])
        with self.assertRaises(runs.RunError):
            runs.new_run(self.cfg, "X1", "acme", "human", "OP03", "first-timer", "self-serve monthly", cost_only=True)
        c = runs.new_run(self.cfg, "X1", "clay", "human", "OP03", "first-timer", "self-serve monthly", cost_only=True)
        runs.start(c["run_id"])
        q = runs.new_run(self.cfg, "X1", "quoteco", "human", "OP01", "first-timer", "quoted package")
        runs.set_usage(q["run_id"], before=10, after=7)
        self.assertIsNone(costs.run_costs(self.cfg, runs.load(q["run_id"]))["usage_cost_usd"])
        with open(paths.datasets() / "x1-input.csv", "a") as f:
            f.write("4,P4,example.com\n")
        with self.assertRaises(runs.RunError):
            runs.new_run(self.cfg, "X1", "clay", "human", "OP01", "first-timer", "self-serve monthly")

    def test_repo_discovery_from_cwd(self):
        os.environ.pop("BENCH_REPO")
        old = os.getcwd()
        try:
            os.chdir(FIXTURE / "scoring")
            self.assertEqual(paths.repo().resolve(), FIXTURE.resolve())
            os.chdir(self.root)
            with self.assertRaises(paths.PathError):
                paths.repo()
        finally:
            os.chdir(old)

    def test_agent_env_and_stream(self):
        os.environ["CLAY_API_KEY"] = "clay-secret-123"
        os.environ["ACME_API_KEY"] = "acme-secret-456"
        try:
            r = runs.new_run(self.cfg, "X1", "clay", "agent", "agent-x", "agent", "self-serve monthly")
            prompt = agent.build_prompt(self.cfg, r)
            self.assertIn(r["run_id"], prompt)
            self.assertNotIn("clay-secret-123", prompt)
            env = agent._child_env(self.cfg, "clay")
            self.assertIn("CLAY_API_KEY", env)
            self.assertNotIn("ACME_API_KEY", env)
            shared = dict(self.cfg, platforms=dict(self.cfg["platforms"], clay_new=dict(self.cfg["platforms"]["clay"], label="Clay new")))
            self.assertIn("CLAY_API_KEY", agent._child_env(shared, "clay"))
            lines = [json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}),
                     json.dumps({"type": "result", "total_cost_usd": 0.5})]
            steps, res = agent.parse_stream(lines)
            self.assertEqual((steps, res["total_cost_usd"]), (1, 0.5))
        finally:
            os.environ.pop("CLAY_API_KEY", None)
            os.environ.pop("ACME_API_KEY", None)


if __name__ == "__main__":
    unittest.main()
