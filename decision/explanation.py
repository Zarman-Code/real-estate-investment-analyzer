"""Texte explicatif du rapporteur final, écrit par le CODE à partir des sorties des quatre analystes.

Quatre parties, pour que le comité puisse valider l'analyse :
  1. La décision et ses raisons (scénario central, règles du fonds) ;
  2. Les risques déclarés : niveau, justification, effet chiffré s'ils surviennent, mesure de réduction ;
  3. Les hypothèses à valider, classées par influence sur le TRI (sensibilités), avec leur origine, leur seuil
     de bascule et ce qu'il faut obtenir pour les confirmer ;
  4. Ce qui ferait changer la décision (seuils où la marge s'annule ou le TRI tombe à l'objectif).

Chaque chiffre est une phrase traçable (decision/phrase.py) : le dashboard l'affiche avec sa source. Aucun chiffre
n'est inventé : tout provient des faits (decision/facts.py). Les textes génériques (actions de validation) sont
dans config/defaults.yaml (explanation).
"""
from __future__ import annotations

from typing import Any

from config.schema import DecisionRules, ExplanationConfig
from decision.phrase import P, S, cap, join
from decision.rules import GO, GO_COND, GO_CONSTRAINED, NO_GO, UNDETERMINED

TARGET_TEXT = {"levered_irr": ("TRI fonds propres", "pct", "financials.irr_equity", "targets.min_levered_irr_pct"),
               "margin_on_cost": ("marge sur coût", "pct", "financials.margin_pct", "targets.min_margin_on_cost_pct"),
               "spread_bps": ("spread", "bps", "financials.spread_bps", "targets.min_spread_vs_exit_bps"),
               "equity_multiple": ("multiple des fonds propres", "mult", "financials.equity_multiple", "targets.min_equity_multiple")}

# paramètre du modèle -> (libellé, format, suffixe d'unité ajouté au gabarit)
FIELD_TEXT = {"relet_rent": ("loyer de relocation", "rent", ""), "exit_yield": ("taux de sortie", "pct2", ""),
              "capex_per_sqm": ("coût des travaux", "sqm", ""), "euribor": ("Euribor à trois mois", "pct2", ""),
              "stabilised_occupancy": ("taux d'occupation stabilisé", "pct0", ""),
              "letting_void_months": ("délai de relocation", "dec1", " mois"), "works_months": ("durée des travaux", "int", " mois"),
              "acq_fees_pct": ("frais d'acquisition", "pct", ""), "rent_free_months": ("franchise", "dec1", " mois")}

ORIGIN_TEXT = {"registry": "donnée de marché sourcée", "computed": "valeur calculée", "assumption": "hypothèse du modèle",
               "config": "donnée du dossier de l'opération", "override": "valeur imposée par l'utilisateur"}

DECISION_HEAD = {GO: "L'agent recommande d'engager l'opération (GO).",
                 GO_CONSTRAINED: "L'agent recommande d'engager l'opération avec des contraintes à respecter (GO AVEC CONTRAINTES).",
                 GO_COND: "L'agent recommande d'engager l'opération sous conditions (GO SOUS CONDITIONS).",
                 NO_GO: "L'agent recommande de ne pas engager l'opération (NO-GO)."}


def _input(facts: dict, name: str) -> dict | None:
    return next((p for p in facts.get("inputs", []) if p["name"] == name), None)


def _sources_text(facts: dict, reference: str | None) -> str:
    """« source : X (date, confiance) ; 1 source pour 2 demandées » pour les données de marché citées par l'entrée."""
    import re
    if not reference:
        return ""
    parts = []
    for key in dict.fromkeys(re.findall(r"\b(?:market|rates|deal)\.[a-z_0-9]+", reference)):
        prov = facts.get("provenance", {}).get(key)
        if not prov or not prov["sources"]:
            continue
        s0 = prov["sources"][0]
        conf = {"high": "élevée", "medium": "moyenne", "low": "faible"}.get(s0["confidence"], s0["confidence"])
        txt = f"{s0['source']} ({s0['date'] or 'non daté'}, confiance {conf})"
        n = len(prov["sources"])
        if n < prov["min_sources"]:
            txt += f", source unique pour {prov['min_sources']} demandées"
        elif n > 1:
            txt += f", recoupée par {n - 1} autre(s) source(s)"
        parts.append(txt)
    return " ; ".join(parts)


# ------------------------------------------------------------------ 1. décision

def decision_section(facts: dict, verdict: dict, rules: DecisionRules) -> list:
    d = verdict["decision"]
    if d == UNDETERMINED:
        return [join("", ["L'agent ne peut pas trancher (NON DÉTERMINÉ) : ", verdict["triggers"][0], "."]),
                "Aucune décision n'est inventée : les données manquantes doivent être collectées ou fournies avec leur source."]
    cr, c, t = verdict["criteria"], facts["central"], facts["targets"]
    out = [join("", [DECISION_HEAD[d], " La décision est prise sur le scénario central, par les règles du fonds : ",
                     join(" ; ", list(verdict["triggers"])), "."])]
    checks = []
    for k, (label, f, kv, kt) in TARGET_TEXT.items():
        ok = cr["A"]["checks"][k]["ok"]
        checks.append(P(label + " {0} pour un objectif de {1} (" + ("atteint" if ok else "non atteint") + ")",
                        S(c[k], f, kv), S(t[k], f, kt)))
    out.append(join("", ["Au prix demandé, le scénario central donne : ", join(", ", checks), "."]))
    mp = facts["max_price"]
    binding = {"levered_irr": "l'objectif de TRI fonds propres", "margin_on_cost": "l'objectif de marge"}.get(mp["binding_target"], "les objectifs du fonds")
    if mp["value"] is not None:
        rel = "au-dessus" if mp["value"] >= facts["asking_price"] else "en dessous"
        out.append(P("Le prix maximum compatible avec ces objectifs est de {0}, soit {1} " + rel + " du prix demandé ({2}) ; "
                     "il est fixé par " + binding + ".",
                     S(mp["value"], "meur", "financials.max_price.central"), S(abs(mp["gap_pct"]), "pct"),
                     S(facts["asking_price"], "meur", "deal.asking_price_eur")))
    out += price_per_sqm_paragraphs(facts, cr)
    gates = cr.get("gates", {})
    if not gates.get("risks", True) or not gates.get("robustness", True):
        ignored = [x for x, on in (("les risques", gates.get("risks", True)), ("le scénario défavorable", gates.get("robustness", True))) if not on]
        out.append(f"Par choix du fonds, {' et '.join(ignored)} n'entrent pas dans la décision : "
                   "ils sont déclarés ci-dessous pour que le comité en juge.")
    sc = facts.get("scenarios", {}).get(rules.robustness_scenario)
    if sc:
        out.append(P("Pour information, en scénario " + sc["label"].lower() + ", la marge serait de {0} et le TRI fonds propres de {1}"
                     + (" : les objectifs resteraient atteints." if sc["meets_targets"] else " : les objectifs ne seraient plus tous atteints."),
                     S(sc["margin_on_cost"], "pct", f"scenarios.{rules.robustness_scenario}.margin_pct"),
                     S(sc["levered_irr"], "pct", f"scenarios.{rules.robustness_scenario}.irr_equity")))
    return out


def price_per_sqm_paragraphs(facts: dict, cr: dict) -> list:
    """Prix au m² : le prix demandé face à CHAQUE référence de marché du registre (écarts calculés par le module de
    comparaison des prix), puis la conclusion du critère M et la cible de négociation qui en découle."""
    m = cr.get("M") or {}
    refs = facts.get("price_references") or []
    if not refs:
        return ["Prix au m² : aucune référence de marché disponible pour comparer le prix demandé (Information non disponible)."]
    c = facts["central"]
    items = []
    for r in refs:
        conf = {"high": "élevée", "medium": "moyenne", "low": "faible"}.get(r.get("confidence"), r.get("confidence"))
        basis = "surface actuelle" if r["basis"] == "surface_before" else "surface après travaux"
        asked = c["price_per_sqm_before"] if r["basis"] == "surface_before" else c["price_per_sqm_after"]
        slots = [S(r["value_per_sqm"], "sqm"), S(asked, "sqm"), S(r["gap_pct"], "pct_signed")]
        date = ""
        if r.get("data_date"):
            date = ", donnée du {3}"
            slots.append(S(r["data_date"], "text"))
        items.append(P(r["label"] + " {0} (" + r["source"].split(" (")[0] + date
                       + ", confiance " + str(conf) + ") contre {1} demandés sur la " + basis + ", écart {2}", *slots))
    out = [join("", ["Prix au m², sous tous les angles disponibles : ", join(" ; ", items), "."])]
    if m.get("available"):
        from decision.rules import market_price_phrase
        concl = [cap(market_price_phrase(m)), "."]
        if m["constraint"]:
            ceiling = min(m["target_price"], facts["max_price"]["value"] or m["target_price"])
            asking = facts["asking_price"]
            concl += [P(" Contrainte : négocier le prix sous {0} ({1}), niveau où il passerait sous la référence de marché "
                        "de plus de {2}, soit {3} de moins que le prix demandé ({4}). Les données ne chiffrent pas la décote "
                        "propre à un immeuble à restructurer : ce seuil est un minimum, à approfondir avec l'audit technique. "
                        "Le prix maximum financier ({5}) reste un plafond, pas un objectif.",
                        S(ceiling, "meur2"), S(m["target_per_sqm"], "sqm"), S(m["band"], "pct0"),
                        S(asking - ceiling, "meur2"), S(ceiling / asking - 1, "pct_signed"),
                        S(facts["max_price"]["value"], "meur", "financials.max_price.central"))]
        out.append(join("", concl))
    return out


# ------------------------------------------------------------------ 2. risques déclarés

def risks_section(facts: dict, verdict: dict) -> list:
    risks = sorted(facts["risks"], key=lambda r: (-(r.get("criticality") or 0), r["id"]))
    if not risks:
        return ["Aucun risque n'est caractérisé par les données : aucun n'est communiqué."]
    gated = ((verdict.get("criteria") or {}).get("gates") or {}).get("risks", True)
    levels = {}
    for r in risks:
        levels[r["level"]] = levels.get(r["level"], 0) + 1
    order = ["critique", "élevé", "modéré", "faible", "incertain"]
    count = join(", ", [P("{0} " + lv + ("s" if n > 1 and lv not in ("incertain",) else ""), S(n, "int"))
                        for lv in order if (n := levels.get(lv))])
    out = [join("", [P("{0} risques sont caractérisés par les données (", S(len(risks), "int")), count, ")",
                     " ; ils n'entrent pas dans la décision." if not gated else " ; les risques critiques entrent dans la décision.",
                     " Chacun est noté en probabilité et en impact, l'impact étant mesuré en rejouant le modèle avec le choc du risque."])]
    t = facts["targets"]
    for r in risks:
        rid = r["id"]
        if r["level"] == "incertain" or r.get("criticality") is None:
            out.append(P(r["title"] + " (évaluation incertaine) : {0}.", S(r.get("probability_rationale") or "justification non fournie", "text", f"risks.{rid}.probability_rationale")))
            continue
        fe = r.get("financial_effect") or {}
        head = P(r["title"] + " (" + r["level"] + ", probabilité {0} × impact {1} = {2}) : {3}.",
                 S(r["probability"], "prob", f"risks.{rid}.probability"), S(r["impact"], "prob", f"risks.{rid}.impact"),
                 S(r["criticality"], "crit", f"risks.{rid}.criticality"),
                 S(r.get("probability_rationale") or "justification non fournie", "text", f"risks.{rid}.probability_rationale"))
        parts = [head]
        if fe.get("shocked_margin_on_cost") is not None:
            still = (fe["shocked_margin_on_cost"] >= t["margin_on_cost"] and fe["shocked_levered_irr"] is not None
                     and fe["shocked_levered_irr"] >= t["levered_irr"])
            parts.append(P(" S'il survient ({0}), la marge passerait de {1} à {2} et le TRI fonds propres de {3} à {4}"
                           + (" : les objectifs du fonds resteraient atteints." if still else " : les objectifs du fonds ne seraient plus atteints."),
                           S(fe["shock_rationale"], "text", f"risks.{rid}.shock"),
                           S(fe["base_margin_on_cost"], "pct", "financials.margin_pct"), S(fe["shocked_margin_on_cost"], "pct"),
                           S(fe["base_levered_irr"], "pct", "financials.irr_equity"), S(fe["shocked_levered_irr"], "pct")))
        if r.get("mitigations"):
            parts.append(" Mesure : " + r["mitigations"][0] + ".")
        out.append(join("", parts))
    return out


# ------------------------------------------------------------------ 3. hypothèses à valider

def _value_phrase(field: str, value: Any, key: str | None) -> Any:
    label, f, suffix = FIELD_TEXT.get(field, (field, "dec1", ""))
    if value is None:
        return label
    return P(label + " de {0}" + suffix, S(value, f, key))


def hypotheses_section(facts: dict, cfg: ExplanationConfig, sensitivity_vars: dict) -> list:
    rows = facts.get("tornado") or []
    if not rows:
        return ["Sensibilités non calculées : les hypothèses ne peuvent pas être classées par influence."]
    out = ["Le résultat repose d'abord sur les hypothèses suivantes, classées par leur influence sur le TRI fonds propres "
           "(chaque hypothèse variée seule dans sa fourchette, les autres restant au central) :"]
    be = facts.get("breakeven", {})
    seen = set()
    for r in rows:
        var = sensitivity_vars.get(r["variable"])
        field = var.pessimistic[0].field if var else None
        if not field or field in seen:
            continue
        seen.add(field)
        p = _input(facts, field)
        value = p["value"] if p else (be.get(r["variable"], {}).get("central_value"))
        parts = [cap(_value_phrase(field, value, f"params.{field}" if p else None))]
        if p:
            origin = ORIGIN_TEXT.get(p["origin"], p["origin"])
            detail = (p.get("note") or "").strip().rstrip(".") if p["origin"] in ("computed", "assumption") else None
            src = _sources_text(facts, p.get("reference"))
            parts.append(join("", [" (", origin, P(" : {0}", S(detail, "text")) if detail else "", ")"]))
            if src:
                parts.append(P(" ; données de marché : {0}", S(src, "text")))
            if p.get("fallback"):
                parts.append(" ; valeur de REPLI faute de donnée de marché")
        lo, hi = r["delta_irr_pts"].get("pessimistic"), r["delta_irr_pts"].get("optimistic")
        if lo is not None and hi is not None:
            parts.append(P(". Sur sa fourchette, le TRI fonds propres varie de {0} à {1}", S(lo, "pts"), S(hi, "pts")))
        b = be.get(r["variable"])
        if b and b.get("field") == field and not b.get("integer"):
            label, f, suffix = FIELD_TEXT.get(field, (field, "dec1", ""))
            if b.get("margin_zero_value") is not None:
                parts.append(P(" ; la marge s'annule à {0}" + suffix, S(b["margin_zero_value"], f, f"sensitivity.breakeven.{r['variable']}.breakeven")))
            if b.get("irr_target_value") is not None:
                parts.append(P(" et le TRI tombe à l'objectif à {0}" + suffix, S(b["irr_target_value"], f, f"sensitivity.breakeven.{r['variable']}.irr_target")))
        action = cfg.validation_actions.get(field)
        parts.append(". Pour la valider : " + action + "." if action else ".")
        out.append(join("", parts))
        if len(out) - 1 >= cfg.max_hypotheses:
            break
    # autres hypothèses à confirmer : valeurs de repli non classées ci-dessus, objectifs du fonds
    others = []
    for fb in facts.get("fallbacks", []):
        if fb["name"] in seen:
            continue
        action = cfg.validation_actions.get(fb["name"])
        others.append(join("", [_value_phrase(fb["name"], fb["value"], f"params.{fb['name']}"), " (valeur de repli)",
                                (" : " + action) if action else ""]))
    if any(s == "assumption" for s in facts["targets"].get("status", {}).values()):
        others.append("objectifs du fonds fixés par hypothèse" + (" : " + cfg.validation_actions["targets"]
                                                                   if "targets" in cfg.validation_actions else ""))
    if facts.get("data_missing"):
        from report.labels import labels
        others.append(f"données non trouvées, sans effet chiffré dans le modèle : {labels(facts['data_missing'])}")
    if others:
        out.append(join("", ["À confirmer aussi : ", join(" ; ", others), "."]))
    return out


# ------------------------------------------------------------------ 4. ce qui changerait la décision

def thresholds_section(facts: dict, verdict: dict) -> list:
    be = facts.get("breakeven", {})
    if verdict["decision"] == UNDETERMINED or not be:
        return []
    nogo, cond = [], []
    for var, b in be.items():
        field = b.get("field")
        label, f, suffix = FIELD_TEXT.get(field, (b["label"], "dec1", ""))
        integer = b.get("integer")
        tpl = label + (" : +{0} mois" if integer else " à {0}" + suffix)
        if b.get("margin_zero_value") is not None:
            nogo.append(P(tpl, S(b["margin_zero_value"], "int" if integer else f, f"sensitivity.breakeven.{var}.breakeven")))
        if b.get("irr_target_value") is not None:
            cond.append(P(tpl, S(b["irr_target_value"], "int" if integer else f, f"sensitivity.breakeven.{var}.irr_target")))
    out = []
    if cond:
        out.append(join("", ["Le TRI fonds propres tomberait à l'objectif du fonds (la décision deviendrait au mieux GO SOUS CONDITIONS) avec : ",
                             join(" ; ", cond), "."]))
    if nogo:
        out.append(join("", ["La marge centrale s'annulerait (NO-GO) avec : ", join(" ; ", nogo),
                             ". Chaque seuil s'entend l'hypothèse variant seule, les autres restant au central."]))
    return out


def build_explanation(facts: dict, verdict: dict, rules: DecisionRules, cfg: ExplanationConfig | None,
                      sensitivity_vars: dict) -> list[dict]:
    """[{title, paragraphs: [Phrase | str]}] : le texte explicatif final."""
    sections = [{"title": "Décision et raisons", "paragraphs": decision_section(facts, verdict, rules)}]
    if verdict["decision"] == UNDETERMINED:
        sections.append({"title": "Risques déclarés", "paragraphs": risks_section(facts, verdict)})
        return sections
    sections.append({"title": "Risques déclarés", "paragraphs": risks_section(facts, verdict)})
    if cfg:
        sections.append({"title": "Hypothèses à valider", "paragraphs": hypotheses_section(facts, cfg, sensitivity_vars)})
    th = thresholds_section(facts, verdict)
    if th:
        sections.append({"title": "Ce qui ferait changer la décision", "paragraphs": th})
    return sections


def as_text(sections: list[dict]) -> str:
    return "\n\n".join(f"{s['title'].upper()}\n" + "\n".join(f"- {p}" if i else str(p) for i, p in enumerate(s["paragraphs"]))
                       for s in sections)
