"""Note go/no-go de 5 à 10 lignes (plan, section 8.3), rédigée SANS LLM à partir de la recommandation.
Contenu imposé par le case : recommandation, prix maximum, 3 risques principaux, demandes au vendeur.
Sert de version de référence ; la version rédigée par le LLM (si elle passe le contrôle) la remplace."""
from __future__ import annotations

from decision import fmt
from decision.phrase import P, S, cap, join
from decision.rules import NO_GO, UNDETERMINED
from report.labels import PARAM_LABELS, labels, param_labels

# lignes écrites par le CODE et toujours publiées telles quelles, même quand le LLM rédige la note :
# aucun LLM ne peut retirer une réserve, une demande au vendeur ou le prix maximum
MANDATORY_PREFIXES = ("Prix maximum", "Prix au m²", "Prix demandé", "Robustesse", "Conditions", "Pour un réexamen",
                      "Prochaine étape", "À demander au vendeur", "Réserves")

# chaque chiffre est une phrase traçable (clé de la donnée) : le dashboard l'affiche avec sa source
FALLBACK_TEXT = {
    "relet_rent": lambda v: P("loyer de relocation de {0}/an égal au loyer moyen de seconde main, faute de loyer prime sourcé",
                              S(v, "sqm", "params.relet_rent")),
    "exit_yield": lambda v: P("taux de sortie de {0} implicite (loyer moyen / prix moyen d'investissement), faute de taux prime sourcé",
                              S(v, "pct2", "params.exit_yield")),
    "rent_free_months": lambda v: P("franchise de {0} mois déduite de mesures d'accompagnement supposées",
                                    S(v, "dec1", "params.rent_free_months")),
    "acq_fees_pct": lambda v: P("frais d'acquisition supposés à {0}", S(v, "pct", "params.acq_fees_pct")),
}


def _fallback_text(fb: dict) -> str:
    f = FALLBACK_TEXT.get(fb["name"])
    if f and isinstance(fb["value"], (int, float)):
        return f(fb["value"])
    return f"{PARAM_LABELS.get(fb['name'], fb['name'])} : valeur de repli"


def _plain(line: str) -> str:
    return line.lstrip("*").strip()


def is_mandatory(line: str) -> bool:
    return _plain(line).startswith(MANDATORY_PREFIXES)


def merge_note(llm_lines: list[str], reference: list[str], max_lines: int = 10) -> list[str]:
    """Note publiée : 1re ligne (recommandation) et lignes libres du LLM, PLUS toutes les lignes obligatoires de
    la référence, à l'identique et dans leur ordre. Les lignes libres sont tronquées pour tenir en `max_lines`."""
    mandatory = [x for x in reference if is_mandatory(x)]
    free = [x for x in llm_lines[1:] if not is_mandatory(x)][:max(0, max_lines - 1 - len(mandatory))]
    head = []                                  # lignes de prix (maximum, au m²) juste après la recommandation
    for x in mandatory:
        if not _plain(x).startswith(("Prix maximum", "Prix au m²", "Prix demandé")):
            break
        head.append(x)
    return [llm_lines[0]] + head + free + [x for x in mandatory if x not in head]


def template_lines(rec: dict) -> list[str]:
    f = rec["facts"]
    lines = []
    if rec["decision"] == UNDETERMINED:
        missing = f.get("required_missing") or f["missing_inputs"]
        lines.append(f"**Recommandation : {UNDETERMINED}.** Données exigées pour décider absentes : {labels(missing)}.")
        lines.append(P("Prix demandé : {0} ; prix maximum non calculable tant que ces données manquent.",
                       S(f["asking_price"], "meur", "deal.asking_price_eur")))
        risks = rec["top_risks"]
        if risks:
            lines.append(join("", ["Risques caractérisés par les données (impact non chiffrable à ce stade) : ",
                                   join(" ; ", [P(r["title"] + " (probabilité {0})", S(r["probability"], "prob", f"risks.{r['id']}.probability"))
                                                for r in risks]), "."]))
        else:
            lines.append("Aucun risque n'est encore caractérisé par des données.")
        lines.append("Prochaine étape : collecter " + labels(missing)
                     + " (sources publiques) ou les fournir avec leur source.")
        lines.append("À demander au vendeur : " + ", ".join(rec["seller_requests"]) + ".")
        return lines

    c, mp = f["central"], f["max_price"]
    lines.append(join("", [f"**Recommandation : {rec['decision']}.** ", cap(rec["triggers"][0]), "."]))
    lines.append(P("Prix maximum : {0} (TRI fonds propres ≥ {1} et marge ≥ {2}), contre {3} demandés ({4}).",
                   S(mp["value"], "meur", "financials.max_price.central"),
                   S(f["targets"]["levered_irr"], "pct0", "targets.min_levered_irr_pct"),
                   S(f["targets"]["margin_on_cost"], "pct0", "targets.min_margin_on_cost_pct"),
                   S(f["asking_price"], "meur", "deal.asking_price_eur"), S(mp["gap_pct"], "pct", "financials.max_price_gap_pct")))
    m = (rec.get("criteria") or {}).get("M") or {}
    if m.get("available"):
        from decision.rules import market_price_phrase
        lines.append(join("", ["Prix au m² : ", market_price_phrase(m), "."]))
    else:
        lines.append("Prix au m² : comparaison au marché impossible, aucune référence de prix au m² disponible.")
    lines.append(P("Scénario central : yield on cost {0}, spread {1}, marge {2}, TRI fonds propres {3}, multiple {4}.",
                   S(c["yield_on_cost"], "pct2", "financials.yoc_pct"), S(c["spread_bps"], "bps", "financials.spread_bps"),
                   S(c["margin_on_cost"], "pct", "financials.margin_pct"), S(c["levered_irr"], "pct", "financials.irr_equity"),
                   S(c["equity_multiple"], "mult", "financials.equity_multiple")))
    sc = f.get("scenarios", {})
    if "defavorable" in sc:
        d = sc["defavorable"]
        lines.append(P("Robustesse : en scénario défavorable, marge {0} et TRI fonds propres {1} (prix maximum {2}).",
                       S(d["margin_on_cost"], "pct", "scenarios.defavorable.margin_pct"),
                       S(d["levered_irr"], "pct", "scenarios.defavorable.irr_equity"),
                       S(d["max_price"], "meur", "scenarios.defavorable.max_price")))
    gated = (rec.get("criteria") or {}).get("gates", {}).get("risks", True)
    head = "Risques principaux : " if gated else "Risques principaux (déclarés, sans effet sur la décision) : "
    lines.append(join("", [head, join(" ; ", [
        P(f"{i}) {r['title']} (criticité {{0}})", S(r["criticality"], "crit", f"risks.{r['id']}.criticality")) if r["criticality"]
        else f"{i}) {r['title']} (incertain)" for i, r in enumerate(rec["top_risks"], 1)]), "."]))
    if rec["conditions"]:
        head = "Pour un réexamen : " if rec["decision"] == NO_GO else "Conditions : "
        conds = rec.get("conditions_phr") or rec["conditions"]
        lines.append(join("", [head, join(" ; ", list(conds[:4])), "."]))
    lines.append("À demander au vendeur : " + ", ".join(rec["seller_requests"]) + ".")
    reserves = [_fallback_text(fb) for fb in f.get("fallbacks", [])]
    if f.get("overrides"):
        reserves.append(f"valeurs imposées par l'utilisateur ({param_labels(f['overrides'])})")
    others = [a for a in f.get("assumptions", []) if a not in {fb["name"] for fb in f.get("fallbacks", [])}]
    if others:
        reserves.append(P("{0} " + ("autres " if f.get("fallbacks") else "") + "hypothèses de modélisation", S(len(others), "int")))
    if f["data_missing"]:
        reserves.append(f"données complémentaires non trouvées : {labels(f['data_missing'])}")
    if any(s == "assumption" for s in f["targets"]["status"].values()):
        reserves.append("objectifs du fonds à confirmer")
    if reserves:
        lines.append(join("", ["Réserves : ", join(" ; ", reserves), " (détail dans le dashboard)."]))
    return lines[:10]


def render_markdown(rec: dict, lines: list[str]) -> str:
    head = f"# Note go/no-go : {rec['facts']['asset']}\n\n"
    body = "\n".join(f"{line}  " for line in lines)
    if rec.get("answers"):
        body += "\n\n## Réponses aux questions du comité\n"
        for qid, q in rec["questions"].items():
            body += f"\n**{q}**  \n{rec['answers'].get(qid, '')}\n"
    if rec.get("synthesis"):
        body += f"\n## Synthèse\n\n{rec['synthesis']}\n"
    if rec.get("node_drafting"):
        body += "\n_Rédaction des analyses : " + " ; ".join(
            f"{n} : {'LLM ' + (d['model'] or '') if d['mode'] == 'llm' else 'automatique'}" for n, d in rec["node_drafting"].items()) + "._"
    d = rec["drafting"]
    foot = (f"\n\n_Rédaction : {'LLM ' + d['model'] if d['mode'] == 'llm' else 'automatique, sans LLM'}"
            + (f" ; désaccord du LLM affiché : {d['disagreement']}" if d.get("disagreement") else "")
            + ". Décision calculée par règles codées ; chaque chiffre provient du moteur de calcul._\n")
    return head + body + foot
