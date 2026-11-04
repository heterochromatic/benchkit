"""Shared independent verifier (methodology 5.1).

Emails: ZeroBounce single-validate API, or an imported results file (e.g. a
ZeroBounce bulk export). Phones: imported line-type results only, because the
phone-check provider isn't chosen yet. Results are cached per address so the
same email is never paid for twice across runs.
"""
import csv
import json
import os
import time
import urllib.parse
import urllib.request

from . import paths

ZB_URL = "https://api.zerobounce.net/v2/validate"
EMAIL_COLS = ("email", "email address", "email_address")
STATUS_COLS = ("status", "zb status", "zb_status")


class VerifyError(Exception):
    pass


def _norm(v):
    return (v or "").strip().lower()


def _load_cache():
    p = paths.verifier_cache()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"email": {}, "phone": {}}


def _save_cache(cache):
    # Merge what's on disk first: another bench may have verified addresses since we loaded.
    disk = _load_cache()
    for kind in ("email", "phone"):
        disk.setdefault(kind, {}).update(cache.get(kind, {}))
        cache[kind] = disk[kind]
    paths.write_json(paths.verifier_cache(), disk)


def _pick(header, options):
    lower = {h.strip().lower(): h for h in header}
    for o in options:
        if o in lower:
            return lower[o]
    raise VerifyError(f"None of the columns {options} found in {header}")


def import_email_results(path) -> dict:
    with open(path, newline="", encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        ec, sc = _pick(r.fieldnames, EMAIL_COLS), _pick(r.fieldnames, STATUS_COLS)
        return {_norm(row[ec]): _norm(row[sc]) for row in r if _norm(row[ec])}


def import_phone_results(path) -> dict:
    with open(path, newline="", encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        pc, sc = _pick(r.fieldnames, ("phone",)), _pick(r.fieldnames, ("status",))
        return {_digits(row[pc]): _norm(row[sc]) for row in r if _digits(row[pc])}


def _digits(v):
    return "".join(ch for ch in (v or "") if ch.isdigit() or ch == "+")


def zerobounce_validate(email, api_key, timeout=30) -> str:
    q = urllib.parse.urlencode({"api_key": api_key, "email": email, "ip_address": ""})
    with urllib.request.urlopen(f"{ZB_URL}?{q}", timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        raise VerifyError(f"ZeroBounce error: {data['error']}")
    return _norm(data.get("status"))


def verify_emails(emails, mode, cfg, import_path=None, pause_s=0.2) -> dict:
    """Return {email: status}. Uses the cache first; only new addresses hit the service."""
    cache = _load_cache()
    wanted = sorted({_norm(e) for e in emails if _norm(e)})
    if mode == "import":
        if not import_path:
            raise VerifyError("--mode import needs --email-results <verifier export CSV>")
        imported = import_email_results(import_path)
        cache["email"].update(imported)
    elif mode == "zerobounce":
        key = os.environ.get(cfg["verifier"]["email_api_env"])
        if not key:
            raise VerifyError(f"Set {cfg['verifier']['email_api_env']} to use the ZeroBounce API")
        for e in wanted:
            if e not in cache["email"]:
                cache["email"][e] = zerobounce_validate(e, key)
                _save_cache(cache)
                time.sleep(pause_s)
    elif mode != "cache":
        raise VerifyError("mode must be import, zerobounce or cache")
    _save_cache(cache)
    missing = [e for e in wanted if e not in cache["email"]]
    if missing:
        raise VerifyError(f"{len(missing)} emails have no verifier result (first: {missing[0]})")
    return {e: cache["email"][e] for e in wanted}


def verify_phones(phones, import_path=None) -> dict:
    cache = _load_cache()
    if import_path:
        cache["phone"].update(import_phone_results(import_path))
        _save_cache(cache)
    wanted = sorted({_digits(p) for p in phones if _digits(p)})
    return {p: cache["phone"].get(p) for p in wanted}
