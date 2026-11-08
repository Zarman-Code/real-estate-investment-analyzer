"""Note go/no-go de 5 à 10 lignes (plan, section 8.3), rédigée SANS LLM à partir de la recommandation.
Contenu imposé par le case : recommandation, prix maximum, 3 risques principaux, demandes au vendeur.
Sert de version de référence ; la version rédigée par le LLM (si elle passe le contrôle) la remplace."""
from __future__ import annotations

from decision import fmt
from decision.rules import UNDETERMINED
from report.labels import labels, param_labels


def template_lines(rec: dict) -> list[str]:
    f = rec["facts"]
    lines = []
    if rec["decision"] == UNDETERMINED:
        missing = f.get("required_missing") or f["missing_inputs"]
        lines.append(f"**Recommandation : {UNDETERMINED}.** Données exigées pour décider absentes : {labels(missing)}.")
        lines.append(f"Prix demandé : {fmt.meur(f['asking_price'])} ; prix maximum non calculable tant que ces données manquent.")
        risks = rec["top_risks"]
        if risks:
            lines.append("Risques caractérisés par les données (impact non chiffrable à ce stade) : "
                         + " ; ".join(f"{r['title']} (probabilité {r['probability']}/5)" for r in risks) + ".")
        else:
            lines.append("Aucun risque n'est encore caractérisé par des données.")
        lines.append("Prochaine étape : collecter " + labels(missing)
                     + " (sources publiques) ou les fournir avec leur source.")
        lines.append("À demander au vendeur : " + ", ".join(rec["seller_requests"]) + ".")
        return lines

    c, mp = f["central"], f["max_price"]
    lines.append(f"**Recommandation : {rec['decision']}.** " + rec["triggers"][0][0].upper() + rec["triggers"][0][1:] + ".")
    lines.append(f"Prix maximum : {fmt.meur(mp['value'])} (TRI fonds propres ≥ {fmt.pct(f['targets']['levered_irr'], 0)} "
                 f"et marge ≥ {fmt.pct(f['targets']['margin_on_cost'], 0)}), contre {fmt.meur(f['asking_price'])} demandés "
                 f"({fmt.pct(mp['gap_pct'])}).")
    lines.append(f"Scénario central : yield on cost {fmt.pct(c['yield_on_cost'], 2)}, spread {fmt.bps(c['spread_bps'])}, "
                 f"marge {fmt.pct(c['margin_on_cost'])}, TRI fonds propres {fmt.pct(c['levered_irr'])}, "
                 f"multiple {fmt.mult(c['equity_multiple'])}.")
    sc = f.get("scenarios", {})
    if "defavorable" in sc:
        d = sc["defavorable"]
        lines.append(f"Robustesse : en scénario défavorable, marge {fmt.pct(d['margin_on_cost'])} et TRI fonds propres "
                     f"{fmt.pct(d['levered_irr'])} (prix maximum {fmt.meur(d['max_price'])}).")
    lines.append("Risques principaux : " + " ; ".join(
        f"{i}) {r['title']}" + (f" (criticité {r['criticality']}/25)" if r["criticality"] else " (incertain)")
        for i, r in enumerate(rec["top_risks"], 1)) + ".")
    if rec["conditions"]:
        lines.append("Conditions : " + " ; ".join(rec["conditions"][:4]) + ".")
    lines.append("À demander au vendeur : " + ", ".join(rec["seller_requests"]) + ".")
    reserves = []
    if f.get("overrides"):
        reserves.append(f"valeurs imposées par l'utilisateur ({param_labels(f['overrides'])})")
    if f.get("assumptions"):
        reserves.append(f"{len(f['assumptions'])} hypothèses de modélisation")
    if f["data_missing"]:
        reserves.append(f"données complémentaires non trouvées : {labels(f['data_missing'])}")
    if any(s == "assumption" for s in f["targets"]["status"].values()):
        reserves.append("objectifs du fonds à confirmer")
    if reserves:
        lines.append("Réserves : " + ", ".join(reserves) + " (détail dans le dashboard).")
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
