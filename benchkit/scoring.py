"""Shared scoring helpers and the scorer plugin loader.

Each bench repo defines its acceptance tests in scoring/scorers.py:
- SCORERS: {task_id: fn(run, cfg) -> dict} returning records_in, accepted_records,
  accepted_rate, metrics, floor_met and proposed_status
- output_contacts(run, cfg) -> (emails, phones) for the verifier step
A manual status (Not supported, Void, Did not finish, Not comparable) always wins over the proposal.
"""
import csv
import hashlib
import json
import re

from . import paths
from .runs import run_dir
from .verify import _digits as phone_digits


class ScoreError(Exception):
    pass


YES = {"y", "yes", "true", "1"}


def read_csv(path):
    if not path.exists():
        raise ScoreError(f"Missing file: {path}")
    with open(path, newline="", encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        return r.fieldnames or [], list(r)


def check_columns(header, required):
    missing = [c for c in required if c not in header]
    if missing:
        raise ScoreError(f"Output is missing columns: {missing}")


def verifier(run):
    p = run_dir(run["run_id"]) / "verifier" / "results.json"
    if not p.exists():
        raise ScoreError("Run `bench verify` first")
    return json.loads(p.read_text(encoding="utf-8"))


def email_ok(email, ver, cfg):
    e = (email or "").strip().lower()
    return bool(e) and ver["email"].get(e) in cfg["verifier"]["accept_email_statuses"]


def phone_ok(phone, ver, cfg):
    p = phone_digits(phone)
    return bool(p) and ver.get("phone", {}).get(p) in cfg["verifier"]["accept_phone_statuses"]


def rate(n, d):
    return round(n / d, 4) if d else None


def norm(s):
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    s = re.sub(r"\b(inc|llc|ltd|corp|co|gmbh|the)\b", " ", s)
    return " ".join(s.split())


def same(a, b):
    """Same after normalizing, or one is the other's words plus extra words ("Acme" vs "Acme Software").
    Whole words only: "VP" doesn't match "SVP Sales", "AI" doesn't match "Mailchimp"."""
    a, b = set(norm(a).split()), set(norm(b).split())
    return bool(a) and bool(b) and (a <= b or b <= a)


UNKNOWN = {"", "unknown", "n/a", "na", "?"}


def known(value):
    """Answer-key values marked unknown (or left blank) are excluded from scoring (ANSWER-KEYS.md)."""
    return (value or "").strip().lower() not in UNKNOWN


def output_csv(run):
    return read_csv(run_dir(run["run_id"]) / "output" / "output.csv")


def plugin():
    try:
        return paths.load_module(paths.scorers_path(), "bench_scorers")
    except paths.PathError as e:
        raise ScoreError(str(e)) from None


def score(run, cfg):
    scorers = plugin().SCORERS
    if run["task_id"] not in scorers:
        raise ScoreError(f"No scorer for {run['task_id']} in {paths.scorers_path()}")
    return scorers[run["task_id"]](run, cfg)


def output_contacts(run, cfg):
    return plugin().output_contacts(run, cfg)


def email_domain(email):
    e = (email or "").strip().lower()
    return e.rpartition("@")[2] if "@" in e else ""


def domain_aliases():
    """Optional private/datasets/email-domain-aliases.csv (domain, email_domain): companies whose
    staff use a different email domain from their website, found while building the gold set."""
    p = paths.datasets() / "email-domain-aliases.csv"
    out = {}
    if p.exists():
        for r in read_csv(p)[1]:
            out.setdefault(r["domain"].strip().lower(), set()).add(r["email_domain"].strip().lower())
    return out


def on_domain(email, domain, aliases):
    ed, d = email_domain(email), (domain or "").strip().lower().removeprefix("www.")
    if not ed or not d:
        return False
    return ed == d or ed.endswith("." + d) or ed in aliases.get(d, set())


def accepted_email(email, domain, ver, cfg, aliases):
    """Accepted = verifier-valid AND on the company's domain (or a known alias)."""
    return email_ok(email, ver, cfg) and on_domain(email, domain, aliases)


def _last10(p):
    return "".join(ch for ch in (p or "") if ch.isdigit())[-10:]


def gold_metrics(out_by_id, gold_rows, email_col="email", phone_col="phone"):
    """Compare returned values with the gold subset (input_id, known_email, known_phone, source_url).
    precision = exact ÷ returned on gold rows; recall = exact ÷ gold rows with a known value."""
    m = {}
    for kind, col, known_col, norm_ in (("email", email_col, "known_email", lambda v: (v or "").strip().lower()),
                                        ("phone", phone_col, "known_phone", _last10)):
        rows = [g for g in gold_rows if known(g.get(known_col)) and norm_(g.get(known_col))]
        returned = exact = 0
        for g in rows:
            got = norm_((out_by_id.get(g["input_id"]) or {}).get(col))
            if got:
                returned += 1
                exact += got == norm_(g[known_col])
        m[f"gold_{kind}_rows"] = len(rows)
        m[f"gold_{kind}_precision"] = rate(exact, returned)
        m[f"gold_{kind}_recall"] = rate(exact, len(rows))
        m[f"gold_{kind}_wrong"] = returned - exact
    return m


def holdout_ids(ids, cfg):
    """The held-out share of ids (config holdout.share), chosen by a salted hash so the split is
    stable across runs. The salt lives in the private overlay, so outsiders can't tell which rows
    are held out. Held-out rows are never published and are what re-runs are mainly scored on."""
    h = cfg.get("holdout") or {}
    share, salt = h.get("share", 0), h.get("salt", "")
    if not share:
        return set()
    return {i for i in ids if int(hashlib.sha256(f"{salt}:{i}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < share}


def load_gold(name):
    """Gold subset file in private/datasets/, or [] if it hasn't been built (metrics then show None)."""
    p = paths.datasets() / name
    return read_csv(p)[1] if p.exists() else []
