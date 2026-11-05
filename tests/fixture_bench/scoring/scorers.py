"""Fixture scorer: share of input rows with a verified email."""
from benchkit import paths
from benchkit.runs import run_dir
from benchkit.sanity import mark_valid, write_rows
from benchkit.scoring import check_columns, email_ok, holdout_ids, output_csv, rate, read_csv, verifier


def score_x1(run, cfg):
    t = cfg["tasks"]["X1"]
    header, rows = output_csv(run)
    check_columns(header, t["output_columns"])
    ver = verifier(run)
    ok = sum(email_ok(r["email"], ver, cfg) for r in rows)
    filled = sum(bool(r["email"].strip()) for r in rows)
    r = rate(ok, t["rows"])
    floor = r is not None and r >= t["floor"]["accepted_email_rate"]
    return {"records_in": t["rows"], "accepted_records": ok, "accepted_rate": r,
            "metrics": {"email_fill_rate": rate(filled, t["rows"])},
            "floor_met": floor, "proposed_status": "Pass" if floor else "Partial"}


SCORERS = {"X1": score_x1}


def output_contacts(run, cfg):
    _, rows = output_csv(run)
    return [r["email"] for r in rows], []


def oracle_x1(run, cfg):
    _, rows = read_csv(run_dir(run["run_id"]) / "input" / cfg["tasks"]["X1"]["input"])
    emails = [f"p{r['input_id']}@example.com" for r in rows]
    write_rows(run_dir(run["run_id"]) / "output" / "output.csv", ["input_id", "email"],
               [[r["input_id"], e] for r, e in zip(rows, emails)])
    mark_valid(run, emails)
    return {"email_fill_rate": 1.0}


ORACLES = {"X1": oracle_x1}


def export_domains(cfg):
    _, rows = read_csv(paths.datasets() / "x1-input.csv")
    held = holdout_ids([r["input_id"] for r in rows], cfg)
    return ["domain"], [[r["domain"]] for r in rows if r["input_id"] not in held]


PUBLIC_EXPORTS = {"x1-domains": export_domains}
