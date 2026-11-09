"""Règles de décision CODÉES (plan, section 8.1). Le LLM ne tranche pas.

La décision se prend sur le SCÉNARIO CENTRAL (choix du fonds, config/defaults.yaml, decision_rules).
Critères calculés et publiés :
  A. Attractivité : TRI fonds propres, marge, spread et multiple du scénario central vs objectifs du fonds
  B. Risque      : risques critiques et élevés (déclarés ; dans la décision seulement si risks_in_decision)
  C. Robustesse  : marge du scénario défavorable (publiée ; dans la décision seulement si robustness_in_decision)
Décision :
  NON DÉTERMINÉ      : modèle central incomplet (données P1 manquantes) -> aucune décision inventée
  NO-GO              : prix maximum (central) sous le prix demandé au-delà de la décote négociable, OU marge centrale <= 0
                       [+ risques critiques au-delà du seuil admis, si risks_in_decision]
  GO                 : A conforme au prix demandé, et prix au m² cohérent avec le marché (critère M)
                       [+ aucun risque critique / C satisfaisant, si activés]
  GO AVEC CONTRAINTES: A conforme, mais le prix au m² dépasse la référence de marché (ou n'intègre pas la décote
                       attendue d'un immeuble à restructurer) : le prix doit être négocié (critère M)
  GO SOUS CONDITIONS : sinon : objectifs non atteints au prix demandé mais prix maximum dans la décote négociable
  M. Prix au m²      : prix demandé au m² (surface actuelle) comparé à la référence de marché du registre (médiane des
                       transactions), avec la bande price_gap_band ; les autres références sont publiées pour information
"""
from __future__ import annotations

from typing import Any

from config.schema import DecisionRules
from decision import fmt
from decision.phrase import P, S, join

GO, GO_CONSTRAINED, GO_COND, NO_GO, UNDETERMINED = "GO", "GO AVEC CONTRAINTES", "GO SOUS CONDITIONS", "NO-GO", "NON DÉTERMINÉ"


CRITERIA_LABELS = {"levered_irr": ("TRI fonds propres", fmt.pct), "margin_on_cost": ("marge", fmt.pct),
                   "spread_bps": ("spread", fmt.bps), "equity_multiple": ("multiple", fmt.mult)}


# critère -> (format de phrase, clé de la valeur, clé de l'objectif) : chiffres traçables jusqu'au dashboard
CRITERIA_SLOTS = {"levered_irr": ("pct", "financials.irr_equity", "targets.min_levered_irr_pct"),
                  "margin_on_cost": ("pct", "financials.margin_pct", "targets.min_margin_on_cost_pct"),
                  "spread_bps": ("bps", "financials.spread_bps", "targets.min_spread_vs_exit_bps"),
                  "equity_multiple": ("mult", "financials.equity_multiple", "targets.min_equity_multiple")}


def _check_text(key: str, check: dict):
    label, _ = CRITERIA_LABELS[key]
    f, kv, kt = CRITERIA_SLOTS[key]
    return P(label + " {0} pour un objectif de {1}", S(check["value"], f, kv), S(check["target"], f, kt))


def _input_value(facts: dict, name: str):
    return next((p["value"] for p in facts.get("inputs", []) if p["name"] == name), None)


def market_price(facts: dict, rules: DecisionRules) -> dict[str, Any]:
    """Critère M : le prix demandé au m² face à la référence de marché (première source de la clé configurée,
    base surface actuelle). Aucune décote n'est inventée : la cible de négociation est la référence diminuée de la
    bande `price_gap_band`, seuil à partir duquel le prix est jugé inférieur au marché."""
    refs = [r for r in facts.get("price_references", []) if r["key"] == rules.price_reference and r["basis"] == "surface_before"]
    asking_sqm = facts["central"]["price_per_sqm_before"]
    out = {"available": bool(refs), "asking_per_sqm": asking_sqm, "band": rules.price_gap_band,
           "works": (_input_value(facts, "capex_per_sqm") or 0) > 0, "constraint": False}
    if not refs:
        return out
    ref = refs[0]
    gap = ref["gap_pct"]
    position = "above" if gap > rules.price_gap_band else "below" if gap < -rules.price_gap_band else "in_line"
    surface = _input_value(facts, "surface_before") or facts["asking_price"] / asking_sqm
    target_sqm = ref["value_per_sqm"] * (1 - rules.price_gap_band)
    out.update({"reference": ref, "gap_pct": gap, "position": position,
                "constraint": position == "above" or (position == "in_line" and out["works"] and rules.restructuring_requires_discount),
                "target_per_sqm": target_sqm, "target_price": target_sqm * surface})
    return out


def market_price_phrase(m: dict):
    """Phrase traçable : prix demandé au m² face à la référence, et ce que l'analyste en conclut."""
    ref = m["reference"]
    where = {"above": "au-dessus de", "in_line": "en ligne avec", "below": "en dessous de"}[m["position"]]
    why = {"above": "le prix dépasse le marché",
           "in_line": ("il n'intègre pas la décote attendue pour un immeuble à restructurer" if m["works"]
                       else "il est cohérent avec le marché"),
           "below": ("il intègre une décote, cohérente avec un immeuble à restructurer" if m["works"]
                     else "il est inférieur au marché")}[m["position"]]
    slots = [S(m["asking_per_sqm"], "sqm", "financials.price_per_sqm_asked"),
             S(ref["value_per_sqm"], "sqm", "market.dvf_price_per_sqm", "median"),
             S(m["gap_pct"], "pct_signed", "financials.price_vs_median_pct")]
    date = ""
    if ref.get("data_date"):
        date = ", donnée du {3}"
        slots.append(S(ref["data_date"], "text"))
    return P("prix demandé de {0} " + where + " la " + ref["label"] + " ({1}, écart {2}, " + ref["source"].split(" (")[0]
             + date + ") : " + why, *slots)


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
        "basis": "central",
        "gates": {"risks": rules.risks_in_decision, "robustness": rules.robustness_in_decision},
        "A": {"checks": a, "conform": all(x["ok"] for x in a.values())},
        "B": b | {"manageable": len(b["critical"]) <= rules.max_critical_risks_conditional},
        "C": {"scenario": rules.robustness_scenario, "margin_on_cost": robust_margin,
              "satisfactory": None if robust_margin is None else robust_margin >= 0},
        "price": {"max_price": mp, "asking": asking,
                  "floor": asking * (1 - rules.max_negotiable_discount),
                  "negotiable": mp is not None and mp >= asking * (1 - rules.max_negotiable_discount)},
        "central_profitable": facts["central"]["margin_on_cost"] > 0,
        "M": market_price(facts, rules),
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
        triggers.append(P("prix maximum {0} inférieur au prix demandé {1} de plus de {2}",
                          S(price["max_price"], "meur", "financials.max_price.central"),
                          S(price["asking"], "meur", "deal.asking_price_eur"),
                          S(rules.max_negotiable_discount, "pct0", "rules.max_negotiable_discount")))
    if not cr["central_profitable"]:
        triggers.append(P("marge centrale négative ou nulle ({0})", S(facts["central"]["margin_on_cost"], "pct", "financials.margin_pct")))
    if rules.risks_in_decision and not cr["B"]["manageable"]:
        triggers.append(P("{0} risques critiques (seuil admis : {1})", S(len(cr["B"]["critical"]), "int", "risks.critical_count"),
                          S(rules.max_critical_risks_conditional, "int", "rules.max_critical_risks")))
    if triggers:
        return {"decision": NO_GO, "criteria": cr, "triggers": triggers}

    a_fail = [k for k, v in cr["A"]["checks"].items() if not v["ok"]]
    risk_block = rules.risks_in_decision and bool(cr["B"]["critical"])
    robust_block = rules.robustness_in_decision and not cr["C"]["satisfactory"]
    m = cr["M"]
    if cr["A"]["conform"] and not risk_block and not robust_block:
        extra = (", aucun risque critique" if rules.risks_in_decision else "") + \
                (", marge positive en scénario " + cr["C"]["scenario"] if rules.robustness_in_decision else "")
        head = "objectifs du fonds atteints dans le scénario central au prix demandé" + extra
        if m["constraint"]:
            return {"decision": GO_CONSTRAINED, "criteria": cr,
                    "triggers": [join("", [head, " ; mais ", market_price_phrase(m), " : le prix doit être négocié"])]}
        tail = [" ; ", market_price_phrase(m)] if m["available"] else [" ; comparaison au prix de marché au m² impossible (référence absente)"]
        return {"decision": GO, "criteria": cr, "triggers": [join("", [head, *tail])]}
    if a_fail:
        triggers.append(join("", ["objectifs non atteints au prix demandé dans le scénario central : ",
                                  join(", ", [_check_text(k, cr["A"]["checks"][k]) for k in a_fail])]))
    if risk_block:
        triggers.append(f"risques critiques à réduire : {cr['B']['critical']}")
    if robust_block and cr["C"]["satisfactory"] is False:
        triggers.append(P("marge négative en scénario " + cr["C"]["scenario"] + " ({0})",
                          S(cr["C"]["margin_on_cost"], "pct", f"scenarios.{cr['C']['scenario']}.margin_pct")))
    if robust_block and cr["C"]["satisfactory"] is None:
        triggers.append("robustesse non évaluée (scénarios indisponibles)")
    if m["available"]:
        triggers.append(market_price_phrase(m))
    return {"decision": GO_COND, "criteria": cr, "triggers": triggers}


def conditions(facts: dict, verdict: dict) -> list[str]:
    """Actions à satisfaire avant validation (plan 8.2), déduites des critères qui ont échoué."""
    if verdict["decision"] == UNDETERMINED:
        from report.labels import labels
        missing = facts.get("required_missing") or facts["missing_inputs"]
        return [f"obtenir les données manquantes : {labels(missing)}"]
    cr, out = verdict["criteria"], []
    if cr["price"]["max_price"] is not None and cr["price"]["max_price"] < cr["price"]["asking"]:
        mp_s = S(cr["price"]["max_price"], "meur", "financials.max_price.central")
        ask_s = S(cr["price"]["asking"], "meur", "deal.asking_price_eur")
        if verdict["decision"] == NO_GO:
            # sous un NO-GO, le prix maximum n'est pas une condition de réalisation mais un seuil de réexamen
            out.append(P("le dossier ne pourrait être réexaminé qu'à un prix de {0} au plus (contre {1} demandés)", mp_s, ask_s))
        else:
            out.append(P("ramener le prix d'acquisition à {0} au plus (contre {1} demandés)", mp_s, ask_s))
    m = cr.get("M") or {}
    if m.get("constraint") and verdict["decision"] in (GO_CONSTRAINED, GO_COND):
        ceiling = min(m["target_price"], cr["price"]["max_price"]) if cr["price"]["max_price"] is not None else m["target_price"]
        out.append(P("négocier le prix d'acquisition sous {0}, soit {1} (" + m["reference"]["label"]
                     + " diminuée de {2}, seuil à partir duquel le prix est inférieur au marché) ; la décote propre à un "
                     "immeuble à restructurer n'est pas chiffrée par les données et reste à fixer par le comité ; le prix "
                     "maximum de {3} est un plafond, jamais un objectif",
                     S(ceiling, "meur2"), S(ceiling / m["target_price"] * m["target_per_sqm"], "sqm"),
                     S(m["band"], "pct0"), S(cr["price"]["max_price"], "meur", "financials.max_price.central")))
    gates = cr.get("gates", {"risks": True, "robustness": True})
    if gates["robustness"] and cr["C"]["satisfactory"] is False:
        out.append("sécuriser le scénario défavorable : marché de travaux forfaitaire et pré-commercialisation")
    by_id = {r["id"]: r for r in facts["risks"]}
    if gates["risks"]:                          # sinon les risques sont déclarés dans la note, pas posés en conditions
        for rid in cr["B"]["critical"] + cr["B"]["high"]:
            r = by_id[rid]
            out.append(f"{r['title']} : {r['mitigations'][0]}")
    from report.labels import PARAM_LABELS
    for p in ("relet_rent", "exit_yield"):
        if p in facts.get("overrides", []):
            out.append(f"confirmer par des références de marché la valeur imposée du {PARAM_LABELS[p]}")
    if gates["risks"]:
        for rid in cr["B"]["uncertain"]:
            out.append(f"lever l'incertitude sur « {by_id[rid]['title']} »")
    return out


def top_risks(facts: dict, n: int) -> list[dict]:
    rated = sorted((r for r in facts["risks"] if r["level"] != "incertain"),
                   key=lambda r: (-(r["criticality"] or 0), r["id"]))
    unrated = sorted((r for r in facts["risks"] if r["level"] == "incertain"),
                     key=lambda r: (-(r["probability"] or 0), r["id"]))
    return (rated + unrated)[:n]
