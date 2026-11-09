"""Arguments favorables et défavorables, CHIFFRÉS et SOURCÉS (plan, section 8.2), construits par règles.
Chaque argument porte sa force (pour le classement) et ses références (indicateur, source, date).
Aucun argument n'est créé sans le fait qui le fonde : s'il y a moins de 3 faits, il y a moins de 3 arguments."""
from __future__ import annotations

from decision import fmt
from decision.phrase import P, S


def _arg(polarity: str, strength: float, text, refs: list[str]) -> dict:
    out = {"polarity": polarity, "strength": strength, "text": str(text), "refs": refs}
    if hasattr(text, "template"):                      # phrase traçable : gabarit + chiffres pour le dashboard
        out["template"], out["slots"] = text.template, text.slots
    return out


def build_arguments(facts: dict, max_args: int) -> dict[str, list[dict]]:
    if not facts["complete"]:
        return {"for": [], "against": []}
    c, t = facts["central"], facts["targets"]
    out = []

    # rentabilité vs objectifs du fonds
    for key, label, f, unit_gap, k_val, k_tg in (
            ("levered_irr", "TRI fonds propres", "pct", 100, "financials.irr_equity", "targets.min_levered_irr_pct"),
            ("margin_on_cost", "marge sur coût", "pct", 100, "financials.margin_pct", "targets.min_margin_on_cost_pct"),
            ("spread_bps", "spread yield on cost / taux de sortie", "bps", 0.01, "financials.spread_bps", "targets.min_spread_vs_exit_bps")):
        v, tg = c[key], t[key]
        if v is None:
            continue
        gap = (v - tg) * unit_gap
        word = "au-dessus" if gap >= 0 else "en dessous"
        out.append(_arg("for" if gap >= 0 else "against", abs(gap) + 1,
                        P(label + " de {0} au scénario central, " + word + " de l'objectif de {1}", S(v, f, k_val), S(tg, f, k_tg)),
                        [f"finance.{key}", "config/investor_targets.yaml"]))

    # prix maximum vs prix demandé
    mp = facts["max_price"]["value"]
    if mp is not None:
        gap = mp / facts["asking_price"] - 1
        out.append(_arg("for" if gap >= 0 else "against", abs(gap) * 100 + 2,
                        P("prix maximum de {0} pour respecter les objectifs, contre {1} demandés ({2})",
                          S(mp, "meur", "financials.max_price.central"), S(facts["asking_price"], "meur", "deal.asking_price_eur"),
                          S(gap, "pct", "financials.max_price_gap_pct")), ["finance.max_price"]))

    # prix demandé vs références de marché
    for ref in facts.get("price_references", []):
        gap = ref["gap_pct"]
        before = ref["basis"] == "surface_before"
        tail = " soit {1} par rapport à la référence « " + ref["label"] + " » ({2}"
        out.append(_arg("for" if gap < 0 else "against", abs(gap) * 50,
                        P("prix demandé de {0}" + tail + ", " + ref["source"] + ", " + str(ref["data_date"]) + ")",
                          S(c["price_per_sqm_before"] if before else c["price_per_sqm_after"], "sqm",
                            "financials.price_per_sqm_asked" if before else "financials.price_per_sqm_asked_after"),
                          S(gap, "pct"), S(ref["value_per_sqm"], "sqm", ref["key"], "median" if before else "last"),
                          display="prix demandé de {0}" + tail + ")"), [ref["key"]]))

    # coût total vs valeur vénale des bureaux du secteur
    inv = facts["market"].get("investment_price_per_sqm")
    if inv:
        gap = c["total_cost_per_sqm"] / inv["value"] - 1
        head = "coût total de {0} après travaux, {1} par rapport au prix moyen d'investissement bureaux du secteur ({2}"
        out.append(_arg("for" if gap < 0 else "against", abs(gap) * 50,
                        P(head + ", " + inv["source"] + ", " + str(inv["date"]) + ")",
                          S(c["total_cost_per_sqm"], "sqm", "financials.total_cost.per_sqm_after"), S(gap, "pct"),
                          S(inv["value"], "sqm", "market.investment_price_per_sqm", "last"), display=head + ")"),
                        ["finance.total_cost_per_sqm", "market.investment_price_per_sqm"]))

    # dynamique de marché
    sup = facts["market"].get("supply_yoy")
    if sup and sup["value"] > 0.10:
        out.append(_arg("against", sup["value"] * 20,
                        P("offre immédiate en hausse de {0} sur un an (" + sup["source"] + ", " + str(sup["date"]) + ") : "
                          "concurrence accrue à la relocation", S(sup["value"], "pct0", "market.immediate_supply", "yoy"),
                          display="offre immédiate en hausse de {0} sur un an : concurrence accrue à la relocation"),
                        ["market.immediate_supply"]))
    ip = facts["market"].get("investment_price_yoy")
    if ip and ip["value"] < 0:
        out.append(_arg("against", abs(ip["value"]) * 30,
                        P("prix d'investissement bureaux en baisse de {0} sur un an (" + ip["source"] + ", " + str(ip["date"])
                          + ") : pression sur la valeur de sortie", S(abs(ip["value"]), "pct"),
                          display="prix d'investissement bureaux en baisse de {0} sur un an : pression sur la valeur de sortie"),
                        ["market.investment_price_per_sqm"]))

    # coussins de sécurité (seuils de bascule)
    be = facts.get("breakeven", {})
    rent = be.get("rent")
    if rent and rent.get("margin_zero_value") and rent.get("central_value"):
        cushion = 1 - rent["margin_zero_value"] / rent["central_value"]
        out.append(_arg("for" if cushion >= 0.10 else "against", abs(cushion) * 30,
                        P("la marge ne s'annule qu'à un loyer de {0} ({1} par rapport au loyer central de {2})",
                          S(rent["margin_zero_value"], "sqm", "sensitivity.breakeven.rent.breakeven"), S(-cushion, "pct"),
                          S(rent["central_value"], "sqm", "sensitivity.breakeven.rent.base")),
                        ["sensitivity.breakeven.rent"]))
    ey = be.get("exit_yield")
    if ey and ey.get("margin_zero_value") and ey.get("central_value"):
        room = (ey["margin_zero_value"] - ey["central_value"]) * 10_000
        out.append(_arg("for" if room >= 50 else "against", abs(room) / 10,
                        P("la marge ne s'annule qu'à un taux de sortie de {0} ({1} par rapport au taux central de {2})",
                          S(ey["margin_zero_value"], "pct2", "sensitivity.breakeven.exit_yield.breakeven"), S(room, "bps_signed"),
                          S(ey["central_value"], "pct2", "sensitivity.breakeven.exit_yield.base")),
                        ["sensitivity.breakeven.exit_yield"]))

    # robustesse
    sc = facts.get("scenarios", {})
    if "defavorable" in sc:
        m = sc["defavorable"]["margin_on_cost"]
        irr = sc["defavorable"]["levered_irr"]
        out.append(_arg("for" if m >= 0 else "against", abs(m) * 50 + 1,
                        P("scénario défavorable : marge de {0} et TRI fonds propres de {1}",
                          S(m, "pct", "scenarios.defavorable.margin_pct"), S(irr, "pct", "scenarios.defavorable.irr_equity"))
                        if irr is not None else
                        P("scénario défavorable : marge de {0} et TRI fonds propres de n.d.", S(m, "pct", "scenarios.defavorable.margin_pct")),
                        ["scenarios.defavorable"]))

    # risques majeurs
    for r in facts["risks"]:
        if r["level"] in ("critique", "élevé"):
            fe = r.get("financial_effect") or {}
            d = fe.get("delta_margin_pts")
            head = "risque " + r["level"] + " « " + r["title"] + " » (criticité {0}"
            slots = [S(r["criticality"], "crit", f"risks.{r['id']}.criticality")]
            if d is not None:
                head += ", {1} de marge si le risque survient"
                slots.append(S(d, "pts", f"risks.{r['id']}.delta_margin_pts"))
            out.append(_arg("against", (r["criticality"] or 0) / 2, P(head + ")", *slots), [f"risks.{r['id']}"]))

    pos = sorted((a for a in out if a["polarity"] == "for"), key=lambda a: -a["strength"])[:max_args]
    neg = sorted((a for a in out if a["polarity"] == "against"), key=lambda a: -a["strength"])[:max_args]
    return {"for": pos, "against": neg}
