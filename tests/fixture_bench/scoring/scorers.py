"""Fixture scorer: share of input rows with a verified email."""
from benchkit.scoring import check_columns, email_ok, output_csv, rate, verifier


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
