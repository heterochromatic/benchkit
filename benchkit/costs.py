"""Pricing views from the methodology: cost per run and fully loaded monthly cost."""
import statistics


def unit_cost(cfg, platform, usage_units):
    """Units used × reference-tier unit price. None if any price is unknown (quote pending)."""
    prices = cfg["platforms"][platform]["unit_prices"]
    total = 0.0
    for unit, n in usage_units.items():
        price = prices.get(unit)
        if price is None:
            return None
        total += n * price
    return round(total, 4)


def run_costs(cfg, run):
    usage = unit_cost(cfg, run["platform"], run["usage_units"]) if run["usage_units"] else None
    add_on = run["manual"].get("add_on_cost_usd", 0.0)
    accepted = run["score"].get("accepted_records")
    tier = cfg["platforms"][run["platform"]]["tiers"][run["reference_tier"]]
    seats = cfg["scenario"]["users"]
    seat_cost = None if tier["seat_price"] is None else tier["seat_price"] * seats
    plan_floor = None if tier["plan_monthly"] is None or tier["seat_price"] is None else tier["plan_monthly"] + tier["seat_price"]
    total = None if usage is None else usage + add_on
    cpa = round(total / accepted, 4) if total is not None and accepted else None
    return {
        "usage_cost_usd": usage,
        "add_on_cost_usd": add_on,
        "seat_cost_usd_month": seat_cost,
        "plan_floor_usd_month": plan_floor,
        "cost_per_accepted_record_usd": cpa,
    }


def fully_loaded(cfg, platform, runs):
    """scenario.users seats, scenario.records_per_month records, usage projected from the measured
    units per input row (median) of scenario.projection_task."""
    per_row = {}
    for r in runs:
        rows = r["score"].get("records_in")
        if not rows or not r["usage_units"]:
            continue
        for unit, n in r["usage_units"].items():
            per_row.setdefault(unit, []).append(n / rows)
    if not per_row:
        return None
    sc = cfg["scenario"]
    projected = {u: statistics.median(v) * sc["records_per_month"] for u, v in per_row.items()}
    usage = unit_cost(cfg, platform, projected)
    tier = cfg["platforms"][platform]["tiers"][cfg["platforms"][platform]["task_tiers"][sc["projection_task"]]]
    if usage is None or tier["plan_monthly"] is None or tier["seat_price"] is None:
        return None
    monthly = tier["plan_monthly"] + tier["seat_price"] * sc["users"] + usage
    return {"monthly_usd": round(monthly, 2), "per_record_usd": round(monthly / sc["records_per_month"], 4),
            "projected_units": {u: round(v) for u, v in projected.items()}}
