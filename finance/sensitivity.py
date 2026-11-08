"""Module 5 : sensibilités et scénarios (plan, section 7). Fonctions pures sur DealInputs, sans LLM.

- tornado         : une variable à la fois (optimiste / pessimiste), classée par amplitude d'effet sur le TRI FP ;
- seuils de bascule : valeur du paramètre pour laquelle la marge s'annule, ou le TRI FP atteint l'objectif ;
- heatmaps        : deux paramètres croisés (taux de sortie x loyer, travaux x retard) ;
- scénarios       : variations simultanées (favorable, central, défavorable, stress) + prix maximum de chacun.
"""
from __future__ import annotations

import math
from typing import Callable

from config.schema import BreakevenSpec, HeatmapSpec, ScenarioSpec, SensitivityConfig
from finance.engine import run_model
from finance.inputs import DealInputs
from finance.max_price import max_price
from finance.variations import apply_changes


def metrics(result: dict) -> dict:
    p, r = result["profitability"], result["returns"]
    return {"levered_irr": r["levered_irr"], "unlevered_irr": r["unlevered_irr"], "equity_multiple": r["equity_multiple"],
            "peak_equity": r["peak_equity"], "yield_on_cost": p["yield_on_cost"], "spread_bps": p["spread_bps"],
            "margin": p["margin"], "margin_on_cost": p["margin_on_cost"], "total_cost": result["cost"]["total_cost"],
            "net_exit_value": result["exit"]["net_value"], "exit_date": result["timeline"]["exit"]}


def _irr_or_floor(v: float | None) -> float:
    return -1.0 if v is None else v


# ------------------------------------------------------------------ tornado (une variable à la fois)

def tornado(inp: DealInputs, cfg: SensitivityConfig) -> dict:
    central = metrics(run_model(inp))
    rows = []
    for name, var in cfg.variables.items():
        opt = metrics(run_model(apply_changes(inp, var.optimistic)))
        pes = metrics(run_model(apply_changes(inp, var.pessimistic)))
        rows.append({
            "variable": name, "label": var.label, "optimistic": opt, "pessimistic": pes,
            "delta_irr_pts": {"optimistic": (_irr_or_floor(opt["levered_irr"]) - _irr_or_floor(central["levered_irr"])) * 100,
                              "pessimistic": (_irr_or_floor(pes["levered_irr"]) - _irr_or_floor(central["levered_irr"])) * 100},
            "delta_margin_pts": {"optimistic": (opt["margin_on_cost"] - central["margin_on_cost"]) * 100,
                                 "pessimistic": (pes["margin_on_cost"] - central["margin_on_cost"]) * 100},
        })
    for r in rows:
        r["irr_range_pts"] = abs(r["delta_irr_pts"]["optimistic"] - r["delta_irr_pts"]["pessimistic"])
    rows.sort(key=lambda r: -r["irr_range_pts"])
    for rank, r in enumerate(rows, 1):
        r["rank"] = rank
    return {"central": central, "rows": rows}


# ------------------------------------------------------------------ seuils de bascule

def _solve(f: Callable[[float], float], lo: float, hi: float, integer: bool) -> float | None:
    """Plus petite racine de f (changement de signe) dans [lo, hi] ; None si f ne change pas de signe."""
    f_lo, f_hi = f(lo), f(hi)
    if f_lo == 0:
        return lo
    if f_lo * f_hi > 0:
        return None
    if integer:
        prev = f_lo
        for t in range(int(lo) + 1, int(hi) + 1):
            cur = f(t)
            if cur == 0 or cur * prev < 0:
                return float(t)
            prev = cur
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        f_mid = f(mid)
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
        if hi - lo < 1e-9:
            break
    return (lo + hi) / 2


def breakeven(inp: DealInputs, spec: BreakevenSpec, target_irr: float) -> dict:
    lo, hi = spec.bounds

    def run(t):
        return metrics(run_model(apply_changes(inp, spec.changes, value=t)))

    margin_zero = _solve(lambda t: run(t)["margin_on_cost"], lo, hi, spec.integer)
    irr_target = _solve(lambda t: _irr_or_floor(run(t)["levered_irr"]) - target_irr, lo, hi, spec.integer)
    out = {"label": spec.label, "bounds": [lo, hi], "integer": spec.integer,
           "margin_zero_at": margin_zero, "irr_target_at": irr_target, "irr_target": target_irr}
    # traduction en valeur absolue du paramètre (ex. €/m²/an, taux) pour la lecture du comité
    first = spec.changes[0]
    base = getattr(inp, first.field)
    if first.op in ("mult", "add") and not hasattr(base, "year"):
        conv = (lambda t: base * t) if first.op == "mult" else (lambda t: base + t)
        out["field"], out["central_value"] = first.field, base
        out["margin_zero_value"] = None if margin_zero is None else conv(margin_zero)
        out["irr_target_value"] = None if irr_target is None else conv(irr_target)
    # déjà sous l'objectif au scénario central ?
    central = metrics(run_model(inp))
    out["central_below_target"] = _irr_or_floor(central["levered_irr"]) < target_irr
    out["central_margin_negative"] = central["margin_on_cost"] < 0
    return out


def all_breakevens(inp: DealInputs, cfg: SensitivityConfig, target_irr: float) -> dict:
    return {name: breakeven(inp, spec, target_irr) for name, spec in cfg.breakeven.items()}


# ------------------------------------------------------------------ heatmaps

def heatmap(inp: DealInputs, spec: HeatmapSpec) -> dict:
    irr, margin = [], []
    for y in spec.y.values:
        row_i, row_m = [], []
        base_y = apply_changes(inp, spec.y.changes, value=y)
        for x in spec.x.values:
            m = metrics(run_model(apply_changes(base_y, spec.x.changes, value=x)))
            row_i.append(m["levered_irr"])
            row_m.append(m["margin_on_cost"])
        irr.append(row_i)
        margin.append(row_m)
    return {"x": {"label": spec.x.label, "values": spec.x.values},
            "y": {"label": spec.y.label, "values": spec.y.values},
            "levered_irr": irr, "margin_on_cost": margin}


# ------------------------------------------------------------------ scénarios

def scenario_inputs(inp: DealInputs, spec: ScenarioSpec, cfg: SensitivityConfig) -> DealInputs:
    out = inp
    for var, side in spec.variables.items():
        out = apply_changes(out, getattr(cfg.variables[var], side))
    return out


def scenarios(inp: DealInputs, specs: dict[str, ScenarioSpec], cfg: SensitivityConfig, targets: dict[str, float]) -> dict:
    res = {}
    for name, spec in specs.items():
        s_inp = scenario_inputs(inp, spec, cfg)
        r = run_model(s_inp)
        mp = max_price(s_inp, targets)
        res[name] = {"label": spec.label, "rationale": spec.rationale.strip(), "variables": dict(spec.variables),
                     "metrics": metrics(r), "max_price": mp["max_price"], "binding_target": mp["binding_target"],
                     "meets_targets": all(
                         (_irr_or_floor(r["returns"]["levered_irr"]) if m == "levered_irr" else r["profitability"]["margin_on_cost"]) >= t
                         for m, t in targets.items()),
                     "checks": r["checks"]}
    return res


def is_finite(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))
