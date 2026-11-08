"""Arguments favorables et défavorables, CHIFFRÉS et SOURCÉS (plan, section 8.2), construits par règles.
Chaque argument porte sa force (pour le classement) et ses références (indicateur, source, date).
Aucun argument n'est créé sans le fait qui le fonde : s'il y a moins de 3 faits, il y a moins de 3 arguments."""
from __future__ import annotations

from decision import fmt


def _arg(polarity: str, strength: float, text: str, refs: list[str]) -> dict:
    return {"polarity": polarity, "strength": strength, "text": text, "refs": refs}


def build_arguments(facts: dict, max_args: int) -> dict[str, list[dict]]:
    if not facts["complete"]:
        return {"for": [], "against": []}
    c, t = facts["central"], facts["targets"]
    out = []

    # rentabilité vs objectifs du fonds
    for key, label, f, unit_gap in (("levered_irr", "TRI fonds propres", fmt.pct, 100),
                                    ("margin_on_cost", "marge sur coût", fmt.pct, 100),
                                    ("spread_bps", "spread yield on cost / taux de sortie", fmt.bps, 0.01)):
        v, tg = c[key], t[key]
        if v is None:
            continue
        gap = (v - tg) * unit_gap
        word = "au-dessus" if gap >= 0 else "en dessous"
        out.append(_arg("for" if gap >= 0 else "against", abs(gap) + 1,
                        f"{label} de {f(v)} au scénario central, {word} de l'objectif de {f(tg)}",
                        [f"finance.{key}", "config/investor_targets.yaml"]))

    # prix maximum vs prix demandé
    mp = facts["max_price"]["value"]
    if mp is not None:
        gap = mp / facts["asking_price"] - 1
        out.append(_arg("for" if gap >= 0 else "against", abs(gap) * 100 + 2,
                        f"prix maximum de {fmt.meur(mp)} pour respecter les objectifs, contre {fmt.meur(facts['asking_price'])} "
                        f"demandés ({fmt.pct(gap)})", ["finance.max_price"]))

    # prix demandé vs références de marché
    for ref in facts.get("price_references", []):
        gap = ref["gap_pct"]
        out.append(_arg("for" if gap < 0 else "against", abs(gap) * 50,
                        f"prix demandé de {fmt.eur_sqm(c['price_per_sqm_before'] if ref['basis'] == 'surface_before' else c['price_per_sqm_after'])} "
                        f"soit {fmt.pct(gap)} par rapport à la référence « {ref['label']} » ({fmt.eur_sqm(ref['value_per_sqm'])}, "
                        f"{ref['source']}, {ref['data_date']})", [ref["key"]]))

    # coût total vs valeur vénale des bureaux du secteur
    inv = facts["market"].get("investment_price_per_sqm")
    if inv:
        gap = c["total_cost_per_sqm"] / inv["value"] - 1
        out.append(_arg("for" if gap < 0 else "against", abs(gap) * 50,
                        f"coût total de {fmt.eur_sqm(c['total_cost_per_sqm'])} après travaux, {fmt.pct(gap)} par rapport au prix "
                        f"moyen d'investissement bureaux du secteur ({fmt.eur_sqm(inv['value'])}, {inv['source']}, {inv['date']})",
                        ["finance.total_cost_per_sqm", "market.investment_price_per_sqm"]))

    # dynamique de marché
    sup = facts["market"].get("supply_yoy")
    if sup and sup["value"] > 0.10:
        out.append(_arg("against", sup["value"] * 20,
                        f"offre immédiate en hausse de {fmt.pct(sup['value'], 0)} sur un an ({sup['source']}, {sup['date']}) : "
                        "concurrence accrue à la relocation", ["market.immediate_supply"]))
    ip = facts["market"].get("investment_price_yoy")
    if ip and ip["value"] < 0:
        out.append(_arg("against", abs(ip["value"]) * 30,
                        f"prix d'investissement bureaux en baisse de {fmt.pct(abs(ip['value']))} sur un an ({ip['source']}, "
                        f"{ip['date']}) : pression sur la valeur de sortie", ["market.investment_price_per_sqm"]))

    # coussins de sécurité (seuils de bascule)
    be = facts.get("breakeven", {})
    rent = be.get("rent")
    if rent and rent.get("margin_zero_value") and rent.get("central_value"):
        cushion = 1 - rent["margin_zero_value"] / rent["central_value"]
        out.append(_arg("for" if cushion >= 0.10 else "against", abs(cushion) * 30,
                        f"la marge ne s'annule qu'à un loyer de {fmt.eur_sqm(rent['margin_zero_value'])} "
                        f"({fmt.pct(-cushion)} par rapport au loyer central de {fmt.eur_sqm(rent['central_value'])})",
                        ["sensitivity.breakeven.rent"]))
    ey = be.get("exit_yield")
    if ey and ey.get("margin_zero_value") and ey.get("central_value"):
        room = (ey["margin_zero_value"] - ey["central_value"]) * 10_000
        out.append(_arg("for" if room >= 50 else "against", abs(room) / 10,
                        f"la marge ne s'annule qu'à un taux de sortie de {fmt.pct(ey['margin_zero_value'], 2)} "
                        f"({'+' if room >= 0 else ''}{fmt.bps(room)} par rapport au taux central de {fmt.pct(ey['central_value'], 2)})",
                        ["sensitivity.breakeven.exit_yield"]))

    # robustesse
    sc = facts.get("scenarios", {})
    if "defavorable" in sc:
        m = sc["defavorable"]["margin_on_cost"]
        out.append(_arg("for" if m >= 0 else "against", abs(m) * 50 + 1,
                        f"scénario défavorable : marge de {fmt.pct(m)} et TRI fonds propres de {fmt.pct(sc['defavorable']['levered_irr'])}",
                        ["scenarios.defavorable"]))

    # risques majeurs
    for r in facts["risks"]:
        if r["level"] in ("critique", "élevé"):
            fe = r.get("financial_effect") or {}
            d = fe.get("delta_margin_pts")
            out.append(_arg("against", (r["criticality"] or 0) / 2,
                            f"risque {r['level']} « {r['title']} » (criticité {r['criticality']}/25"
                            + (f", {fmt.pts(d)} de marge si le risque survient" if d is not None else "") + ")",
                            [f"risks.{r['id']}"]))

    pos = sorted((a for a in out if a["polarity"] == "for"), key=lambda a: -a["strength"])[:max_args]
    neg = sorted((a for a in out if a["polarity"] == "against"), key=lambda a: -a["strength"])[:max_args]
    return {"for": pos, "against": neg}
