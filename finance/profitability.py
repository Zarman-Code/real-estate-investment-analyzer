"""Module 4 : valeur de sortie et rentabilité (formules du plan, section 5.4)."""
from __future__ import annotations

from finance.inputs import DealInputs
from finance.revenue import let_rent_annual, noi_stabilised
from finance.timeline import Timeline


def gross_exit_value(noi: float, exit_yield: float) -> float:
    if exit_yield <= 0:
        raise ValueError("taux de sortie <= 0")
    return noi / exit_yield


def exit_deductions(inp: DealInputs, tl: Timeline) -> dict[str, float]:
    """Ce qu'un acquéreur déduit d'une valeur « loué et stabilisé » quand la cession intervient avant :
    franchise restant à courir, vide locatif restant, honoraires de commercialisation non engagés."""
    n = tl.n_months
    monthly_rent = let_rent_annual(inp) / 12
    void_after = max(0, tl.lease_start - n)
    rent_free_after = max(0.0, tl.rent_start - max(n, tl.lease_start))
    return {
        "remaining_rent_free": rent_free_after * monthly_rent,
        "remaining_void_rent": void_after * monthly_rent,
        "remaining_void_holding": void_after * inp.surface_after * inp.vacant_holding_cost / 12,
        "unspent_marketing": let_rent_annual(inp) * inp.marketing_fees_pct if tl.lease_start >= n else 0.0,
    }


def exit_value(inp: DealInputs, tl: Timeline) -> dict[str, float]:
    noi = noi_stabilised(inp)
    gross = gross_exit_value(noi, inp.exit_yield)
    ded = exit_deductions(inp, tl)
    sale_costs = gross * inp.sale_costs_pct
    net = gross - sum(ded.values()) - sale_costs
    return {"noi_stabilised": noi, "gross": gross, "deductions": ded, "deductions_total": sum(ded.values()),
            "sale_costs": sale_costs, "net": net, "let_at_exit": tl.lease_start < tl.n_months}


def yield_on_cost(noi: float, total_cost: float) -> float:
    return noi / total_cost


def margin(net_exit: float, total_cost: float) -> tuple[float, float]:
    m = net_exit - total_cost
    return m, m / total_cost


def spread_bps(yoc: float, exit_yield: float) -> float:
    return (yoc - exit_yield) * 10_000
