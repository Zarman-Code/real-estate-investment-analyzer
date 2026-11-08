"""Règles de décision CODÉES (plan, section 8.1). Le LLM ne tranche pas.

Critères :
  A. Attractivité : TRI fonds propres, marge, spread et multiple vs objectifs du fonds
  B. Risque      : nombre de risques critiques
  C. Robustesse  : marge du scénario défavorable (une opération rentable seulement en optimiste est fragile)
Décision :
  NON DÉTERMINÉ      : modèle central incomplet (données P1 manquantes) -> aucune décision inventée
  NO-GO              : prix maximum sous le prix demandé au-delà de la décote négociable, OU marge centrale <= 0,
                       OU risques critiques au-delà du seuil admis
  GO                 : A conforme ET aucun risque critique ET C satisfaisant
  GO SOUS CONDITIONS : sinon (potentiel de création de valeur, conditions identifiables à satisfaire)
"""
from __future__ import annotations

from typing import Any

from config.schema import DecisionRules
from decision import fmt

GO, GO_COND, NO_GO, UNDETERMINED = "GO", "GO SOUS CONDITIONS", "NO-GO", "NON DÉTERMINÉ"


CRITERIA_LABELS = {"levered_irr": ("TRI fonds propres", fmt.pct), "margin_on_cost": ("marge", fmt.pct),
                   "spread_bps": ("spread", fmt.bps), "equity_multiple": ("multiple", fmt.mult)}


def _check_text(key: str, check: dict) -> str:
    label, f = CRITERIA_LABELS[key]
    return f"{label} {f(check['value'])} pour un objectif de {f(check['target'])}"


def criteria(facts: dict, rules: DecisionRules) -> dict[str, Any]:
    c, t = facts["central"], facts["targets"]
    checks = {
        "levered_irr": (c["levered_irr"], t["levered_irr"]),
        "margin_on_cost": (c["margin_on_cost"], t["margin_on_cost"]),
        "spread_bps": (c["spread_bps"], t["spread_bps"]),
        "equity_multiple": (c["equity_multiple"], t["equity_multiple"]),
    }
    a = {k: {"value": v, "target": tg, "ok": v is not None and v >= tg} for k, (v, tg) in checks.items()}
    risks = [r for r in facts["risks"] if r["level"] != "incertain"]
    b = {"critical": [r["id"] for r in risks if r["level"] == "critique"],
         "high": [r["id"] for r in risks if r["level"] == "élevé"],
         "uncertain": [r["id"] for r in facts["risks"] if r["level"] == "incertain"]}
    sc = (facts.get("scenarios") or {}).get(rules.robustness_scenario)
    robust_margin = sc["margin_on_cost"] if sc else None
    mp, asking = facts["max_price"]["value"], facts["asking_price"]
    return {
        "A": {"checks": a, "conform": all(x["ok"] for x in a.values())},
        "B": b | {"manageable": len(b["critical"]) <= rules.max_critical_risks_conditional},
        "C": {"scenario": rules.robustness_scenario, "margin_on_cost": robust_margin,
              "satisfactory": None if robust_margin is None else robust_margin >= 0},
        "price": {"max_price": mp, "asking": asking,
                  "floor": asking * (1 - rules.max_negotiable_discount),
                  "negotiable": mp is not None and mp >= asking * (1 - rules.max_negotiable_discount)},
        "central_profitable": facts["central"]["margin_on_cost"] > 0,
    }


def decide(facts: dict, rules: DecisionRules) -> dict[str, Any]:
    from report.labels import labels
    if facts.get("required_missing"):
        return {"decision": UNDETERMINED, "criteria": None,
                "triggers": [f"données exigées pour décider absentes : {labels(facts['required_missing'])}"]}
    if not facts["complete"]:
        return {"decision": UNDETERMINED, "criteria": None,
                "triggers": [f"modèle financier incomplet : données manquantes {labels(facts['missing_inputs'])}"]}
    cr = criteria(facts, rules)
    triggers = []
    price = cr["price"]
    if not price["negotiable"]:
        triggers.append(f"prix maximum {fmt.meur(price['max_price'])} inférieur au prix demandé "
                        f"{fmt.meur(price['asking'])} de plus de {fmt.pct(rules.max_negotiable_discount, 0)}")
    if not cr["central_profitable"]:
        triggers.append(f"marge centrale négative ou nulle ({fmt.pct(facts['central']['margin_on_cost'])})")
    if not cr["B"]["manageable"]:
        triggers.append(f"{len(cr['B']['critical'])} risques critiques (seuil admis : {rules.max_critical_risks_conditional})")
    if triggers:
        return {"decision": NO_GO, "criteria": cr, "triggers": triggers}

    a_fail = [k for k, v in cr["A"]["checks"].items() if not v["ok"]]
    if cr["A"]["conform"] and not cr["B"]["critical"] and cr["C"]["satisfactory"]:
        return {"decision": GO, "criteria": cr,
                "triggers": ["objectifs du fonds atteints au central, aucun risque critique, marge positive en défavorable"]}
    if a_fail:
        triggers.append("objectifs non atteints au prix demandé : " + ", ".join(
            _check_text(k, cr["A"]["checks"][k]) for k in a_fail))
    if cr["B"]["critical"]:
        triggers.append(f"risques critiques à réduire : {cr['B']['critical']}")
    if cr["C"]["satisfactory"] is False:
        triggers.append(f"marge négative en scénario {cr['C']['scenario']} ({fmt.pct(cr['C']['margin_on_cost'])})")
    if cr["C"]["satisfactory"] is None:
        triggers.append("robustesse non évaluée (scénarios indisponibles)")
    return {"decision": GO_COND, "criteria": cr, "triggers": triggers}


def conditions(facts: dict, verdict: dict) -> list[str]:
    """Actions à satisfaire avant validation (plan 8.2), déduites des critères qui ont échoué."""
    if verdict["decision"] == UNDETERMINED:
        from report.labels import labels
        missing = facts.get("required_missing") or facts["missing_inputs"]
        return [f"obtenir les données manquantes : {labels(missing)}"]
    cr, out = verdict["criteria"], []
    if cr["price"]["max_price"] is not None and cr["price"]["max_price"] < cr["price"]["asking"]:
        out.append(f"ramener le prix d'acquisition à {fmt.meur(cr['price']['max_price'])} au plus "
                   f"(contre {fmt.meur(cr['price']['asking'])} demandés)")
    if cr["C"]["satisfactory"] is False:
        out.append("sécuriser le scénario défavorable : marché de travaux forfaitaire et pré-commercialisation")
    by_id = {r["id"]: r for r in facts["risks"]}
    for rid in cr["B"]["critical"] + cr["B"]["high"]:
        r = by_id[rid]
        out.append(f"{r['title']} : {r['mitigations'][0]}")
    from report.labels import PARAM_LABELS
    for p in ("relet_rent", "exit_yield"):
        if p in facts.get("overrides", []):
            out.append(f"confirmer par des références de marché la valeur imposée du {PARAM_LABELS[p]}")
    for rid in cr["B"]["uncertain"]:
        out.append(f"lever l'incertitude sur « {by_id[rid]['title']} »")
    return out


def top_risks(facts: dict, n: int) -> list[dict]:
    rated = sorted((r for r in facts["risks"] if r["level"] != "incertain"),
                   key=lambda r: (-(r["criticality"] or 0), r["id"]))
    unrated = sorted((r for r in facts["risks"] if r["level"] == "incertain"),
                     key=lambda r: (-(r["probability"] or 0), r["id"]))
    return (rated + unrated)[:n]
