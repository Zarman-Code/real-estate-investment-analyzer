"""Nœud risk_analysis : risques notés par règles explicites, sans LLM (plan, section 6).

Pour chaque risque du catalogue (risk/catalog.py) :
  - PROBABILITÉ : règle déterministe sur les données sourcées et le case ;
  - IMPACT : le moteur financier est rejoué avec le choc du risque (defaults.yaml/risk_scoring.shocks) ;
    impact 5 si la marge devient négative, sinon selon la baisse du TRI fonds propres ;
  - CRITICITÉ = P x I et niveau ; si l'une des deux notes manque : « incertain », informations manquantes listées.
Le nœud tourne en parallèle de financial_analysis : il rejoue lui-même le moteur (fonction pure) et
n'écrit pas dans le registre.
"""
from __future__ import annotations

from agent.state import AgentState
from finance.engine import run_model
from finance.inputs import resolve_inputs
from risk.catalog import CATALOG, apply_shock, rule_context
from risk.matrix import Risk, finalise, impact_from_effect, matrix
from report.figures import risk_figures
from report.node_report import make_report


def make_risk_node(llm=None):
    def node(state: AgentState) -> dict:
        out = risk_analysis_core(state)
        figs, summary, answers = risk_figures(out["risks"])
        out["risks"]["report"] = make_report("risk_analysis", figs, summary, answers, state["defaults"], llm)
        out["logs"].append(f"  rapport des risques : {out['risks']['report']['drafting']['mode']} "
                           f"({out['risks']['report']['drafting']['status']})")
        return out
    node.__name__ = "risk_analysis"
    return node


def risk_analysis_core(state: AgentState) -> dict:
    cfg, defaults, registry = state["config"], state["defaults"], state["registry"]
    rs = defaults.risk_scoring
    ctx = rule_context(cfg, registry, defaults)
    ri = resolve_inputs(cfg, registry, defaults)
    base = run_model(ri.inputs) if ri.complete else None

    risks: list[Risk] = []
    for spec in CATALOG:
        res = spec.rule(ctx)
        if not res.applicable:
            continue
        missing = list(res.missing)
        impact, impact_why, effect = None, "", None
        if spec.shock is None:
            missing.append("aucun choc financier défini pour ce risque")
        elif base is None:
            missing.append(f"impact non chiffrable : entrées financières manquantes {ri.missing}")
        else:
            shock = rs.shocks[spec.shock]
            shocked = run_model(apply_shock(ri.inputs, shock))
            b_irr, s_irr = base["returns"]["levered_irr"], shocked["returns"]["levered_irr"]
            b_m, s_m = base["profitability"]["margin_on_cost"], shocked["profitability"]["margin_on_cost"]
            impact, impact_why = impact_from_effect(b_irr, s_irr, b_m, s_m, rs.impact_irr_drop_pts)
            effect = {"shock": spec.shock, "shock_rationale": shock.rationale,
                      "base_levered_irr": b_irr, "shocked_levered_irr": s_irr,
                      "delta_irr_pts": None if b_irr is None or s_irr is None else (s_irr - b_irr) * 100,
                      "base_margin_on_cost": b_m, "shocked_margin_on_cost": s_m,
                      "delta_margin_pts": (s_m - b_m) * 100,
                      "base_margin": base["profitability"]["margin"], "shocked_margin": shocked["profitability"]["margin"]}
        if res.probability is None and not res.missing:
            missing.append("probabilité non évaluable")
        risks.append(finalise(Risk(
            id=spec.id, category=spec.category, title=spec.title, description=spec.description,
            evidence=res.evidence, probability=res.probability, probability_rationale=res.rationale,
            impact=impact, impact_rationale=impact_why, financial_effect=effect,
            missing_information=missing, mitigations=list(spec.mitigations),
            seller_requests=list(spec.seller_requests), financial_link=list(spec.financial_link),
            uncertain=not (res.probability is not None and impact is not None)), rs.criticality_levels))

    order = {"critique": 0, "élevé": 1, "modéré": 2, "faible": 3, "incertain": 4}
    risks.sort(key=lambda r: (order[r.level], -(r.criticality or 0), r.id))
    # règle : un risque qu'aucune donnée ne caractérise (probabilité non évaluable) n'est PAS communiqué ;
    # il reste tracé dans `excluded` pour l'audit, sans figurer dans les analyses ni dans la note
    communicated = [r for r in risks if r.probability is not None]
    excluded = [{"id": r.id, "title": r.title, "reason": r.missing_information} for r in risks if r.probability is None]
    seller = sorted({s for r in risks for s in r.seller_requests})
    out = {"risks": [r.model_dump() for r in communicated], "matrix": matrix(communicated), "excluded": excluded,
           "financial_inputs_complete": ri.complete, "seller_requests": seller,
           "missing_information": sorted({m for r in communicated for m in r.missing_information})}
    counts = {lv: sum(r.level == lv for r in communicated) for lv in order}
    logs = [f"risk_analysis : {len(communicated)} risques caractérisés ({', '.join(f'{v} {k}' for k, v in counts.items() if v)}), "
            f"{len(excluded)} non communiqués faute de données {[e['id'] for e in excluded]}"]
    logs += [f"  {r.id} {r.title} : P {r.probability or '?'} x I {r.impact or '?'} -> {r.level}" for r in communicated]
    return {"risks": out, "logs": logs}


risk_analysis = make_risk_node(None)
