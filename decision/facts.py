"""Faits de la décision, tirés UNIQUEMENT des sorties des quatre nœuds d'analyse
(financial_analysis, risk_analysis, sensitivity_analysis, scenario_analysis).
Le nœud final n'accède ni au registre ni aux sources : il ne voit que ce que les analystes ont produit."""
from __future__ import annotations

from typing import Any

ANALYSIS_KEYS = ("financials", "risks", "sensitivity", "scenarios")


def analysis_outputs(state) -> dict[str, Any]:
    """Seule porte d'entrée du nœud final sur l'état du graphe."""
    return {k: state.get(k) or {} for k in ANALYSIS_KEYS}


def _central(fin: dict) -> dict:
    r = fin["results"]
    return {
        "yield_on_cost": r["profitability"]["yield_on_cost"], "spread_bps": r["profitability"]["spread_bps"],
        "margin": r["profitability"]["margin"], "margin_on_cost": r["profitability"]["margin_on_cost"],
        "unlevered_irr": r["returns"]["unlevered_irr"], "levered_irr": r["returns"]["levered_irr"],
        "equity_multiple": r["returns"]["equity_multiple"], "peak_equity": r["returns"]["peak_equity"],
        "exit_yield": r["exit"]["exit_yield"], "gross_exit_value": r["exit"]["gross_value"],
        "net_exit_value": r["exit"]["net_value"], "total_cost": r["cost"]["total_cost"],
        "total_cost_per_sqm": r["cost"]["total_cost_per_sqm_after"], "noi": r["revenue"]["noi_stabilised"],
        "relet_rent": r["revenue"]["relet_rent_eur_sqm_year"],
        "price_per_sqm_before": r["acquisition"]["price_per_sqm_before"],
        "price_per_sqm_after": r["acquisition"]["price_per_sqm_after"], "exit_date": r["timeline"]["exit"],
    }


def collect_facts(outputs: dict[str, Any]) -> dict[str, Any]:
    fin, risks = outputs["financials"], outputs["risks"]
    required = fin.get("required_data", {})
    dq = fin.get("data_quality", {})
    facts: dict[str, Any] = {
        "asset": fin.get("asset"), "asking_price": fin.get("asking_price"),
        "targets": fin.get("targets_full", {}),
        "complete": fin.get("status") == "complete",
        "missing_inputs": fin.get("missing_inputs", []),
        "required_missing": [k for k, ok in required.items() if not ok],
        "risks": risks.get("risks", []),
        "seller_requests": risks.get("seller_requests", []),
        "data_missing": dq.get("missing", []), "data_rejected": dq.get("rejected", []),
        "market": fin.get("market_context", {}),
        "reports": {k: v.get("report") for k, v in outputs.items() if v.get("report")},
    }
    if not facts["complete"]:
        return facts

    facts["central"] = _central(fin)
    mp = fin["max_price"]
    facts["max_price"] = {"value": mp["max_price"], "binding_target": mp["binding_target"],
                          "by_target": {k: v["price"] for k, v in mp["by_target"].items()},
                          "gap_pct": mp["gap_to_asking_pct"]}
    facts["assumptions"] = [p["name"] for p in fin["inputs"] if p["origin"] == "assumption"]
    facts["overrides"] = [p["name"] for p in fin["inputs"] if p["origin"] == "override"]
    # valeurs de repli faute de la donnée de marché visée : signalées une à une dans les réserves de la note
    facts["fallbacks"] = [{"name": p["name"], "value": p["value"], "reference": p["reference"]}
                          for p in fin["inputs"] if p.get("fallback")]
    facts["price_references"] = fin["price_comparison"]["references"]
    # détail des entrées du modèle et provenance des données de marché : base du chapitre « hypothèses à valider »
    facts["inputs"] = [{k: p.get(k) for k in ("name", "value", "unit", "origin", "reference", "note", "fallback")}
                       for p in fin["inputs"]]
    facts["provenance"] = fin.get("input_provenance", {})

    sc = outputs["scenarios"]
    if sc.get("status") == "complete":
        facts["scenarios"] = {k: {"label": v["label"], "levered_irr": v["metrics"]["levered_irr"],
                                  "margin_on_cost": v["metrics"]["margin_on_cost"], "max_price": v["max_price"],
                                  "meets_targets": v["meets_targets"]} for k, v in sc["scenarios"].items()}
    se = outputs["sensitivity"]
    if se.get("status") == "complete":
        facts["tornado_top"] = [{"label": r["label"], "irr_range_pts": r["irr_range_pts"],
                                 "pessimistic_pts": r["delta_irr_pts"]["pessimistic"]} for r in se["tornado"]["rows"][:3]]
        facts["tornado"] = [{k: r[k] for k in ("variable", "label", "delta_irr_pts", "delta_margin_pts", "irr_range_pts")}
                            for r in se["tornado"]["rows"]]
        facts["breakeven"] = {k: {kk: v.get(kk) for kk in ("label", "field", "integer", "central_value", "margin_zero_value",
                                                           "irr_target_value")}
                              for k, v in se["breakeven"].items()}
    return facts
