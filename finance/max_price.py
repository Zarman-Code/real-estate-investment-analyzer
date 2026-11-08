"""Prix maximum (goal seek, plan section 5.6) : prix d'acquisition le plus élevé qui respecte encore
les objectifs du fonds (TRI fonds propres et marge sur coût). Dichotomie sur une fonction décroissante du prix."""
from __future__ import annotations

from typing import Callable

from finance.engine import run_model, with_price
from finance.inputs import DealInputs


def _metric(inp: DealInputs, name: str) -> Callable[[float], float]:
    def f(price: float) -> float:
        r = run_model(with_price(inp, price))
        v = r["returns"]["levered_irr"] if name == "levered_irr" else r["profitability"]["margin_on_cost"]
        return float("-inf") if v is None else v
    return f


def solve_max_price(inp: DealInputs, metric: str, target: float, tol_eur: float = 1_000.0) -> dict:
    """Renvoie {"price": p | None, "status": ...}. Recherche entre 1 € et 3 x le prix demandé."""
    f = _metric(inp, metric)
    lo, hi = 1.0, inp.price * 3
    if f(lo) < target:
        return {"metric": metric, "target": target, "price": None, "status": "objectif inatteignable même à un prix nul"}
    if f(hi) >= target:
        return {"metric": metric, "target": target, "price": hi, "status": f"objectif atteint jusqu'à {hi:,.0f} € au moins"}
    while hi - lo > tol_eur:
        mid = (lo + hi) / 2
        if f(mid) >= target:
            lo = mid
        else:
            hi = mid
    return {"metric": metric, "target": target, "price": lo, "status": "ok", "value_at_price": f(lo)}


def max_price(inp: DealInputs, targets: dict[str, float]) -> dict:
    """targets : {"levered_irr": 0.15, "margin_on_cost": 0.15}. Le prix maximum retenu est le plus contraignant."""
    results = {m: solve_max_price(inp, m, t) for m, t in targets.items()}
    prices = [r["price"] for r in results.values() if r["price"] is not None]
    binding = None
    if len(prices) == len(results):
        binding = min(results, key=lambda m: results[m]["price"])
    best = results[binding]["price"] if binding else None
    return {"by_target": results, "max_price": best, "binding_target": binding,
            "asking_price": inp.price, "gap_to_asking": (best - inp.price) if best is not None else None,
            "gap_to_asking_pct": (best / inp.price - 1) if best is not None else None}
