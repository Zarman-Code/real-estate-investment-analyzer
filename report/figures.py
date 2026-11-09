"""Chiffres clés et analyses de référence de chaque nœud (rédigés SANS LLM, à partir des seuls résultats calculés).
Chaque phrase s'appuie sur un chiffre de la liste `figures` ; une donnée absente donne « Information non disponible »."""
from __future__ import annotations

from decision import fmt
from report.labels import labels
from report.node_report import NOT_AVAILABLE, figure

CATEGORY_LABELS = {"marché_locatif": "marché locatif", "travaux": "travaux", "financier": "taux et financement",
                   "réglementaire": "réglementation", "environnemental": "environnement"}


def _primary(registry, key):
    f = registry.found(key)
    return f[0] if f else None


def _v(dp, field):
    """Valeur d'un DataPoint : champ d'un dictionnaire (connecteurs) ou valeur simple."""
    if isinstance(dp.value, dict):
        return dp.value.get(field)
    return dp.value if field in ("last", "median") else None


# ------------------------------------------------------------------ financial_analysis (q1 à q4)

def _price_verdict(gap: float, band: float, works: bool) -> str:
    """Conclusion de q1 par règle : écart à la médiane DVF comparé à la bande `price_gap_band` (defaults.yaml)."""
    redev = " pour un immeuble à restructurer, qui devrait se négocier avec une décote" if works else ""
    if gap > band:
        return f"Le prix demandé est donc élevé : il dépasse les références de transaction{redev}."
    if gap < -band:
        return ("Le prix demandé est inférieur aux références de transaction"
                + (", ce qui est cohérent avec la décote attendue pour un immeuble à restructurer." if works else "."))
    return ("Le prix demandé est en ligne avec les références de transaction"
            + (" : il n'intègre pas la décote attendue pour un immeuble à restructurer." if works else "."))


def financial_figures(fin: dict, registry, config, defaults=None) -> tuple[list[dict], str, dict]:
    figs, ans = [], {}
    a, op = config.asset, config.operation
    band = defaults.decision_rules.price_gap_band if defaults and defaults.decision_rules else None
    works = op.capex_eur_per_sqm_after > 0
    price = op.asking_price_eur
    ppsqm = price / a.surface_before_sqm
    figs.append(figure("asking_price", "prix demandé", fmt.meur(price), price, "case"))
    figs.append(figure("asking_price_sqm", "prix demandé au m² (surface actuelle)", fmt.eur_sqm(ppsqm), ppsqm, "calcul",
                       cite=("q1",)))

    # q1 : prix vs transactions
    dvf_pts = registry.found("market.dvf_price_per_sqm")
    if dvf_pts:
        main = dvf_pts[0]
        v = main.value if isinstance(main.value, dict) else {"median": main.value}
        figs.append(figure("dvf_median", "médiane DVF des locaux d'activité du secteur", fmt.eur_sqm(v["median"]), v["median"],
                           f"{main.source_name}, {main.data_date}", cite=("q1",)))
        figs.append(figure("dvf_n", "transactions retenues", str(v.get("n")), v.get("n"), main.source_name))
        gap = ppsqm / v["median"] - 1
        figs.append(figure("gap_dvf", "écart du prix demandé à la médiane DVF", fmt.pct(gap), gap, "calcul", cite=("q1",)))
        txt = (f"Le prix demandé ressort à {fmt.eur_sqm(ppsqm)} sur la surface actuelle, soit {fmt.pct(abs(gap))} "
               f"{'au-dessus' if gap > 0 else 'en dessous'} de la médiane des transactions de locaux d'activité du secteur ({fmt.eur_sqm(v['median'])}, "
               f"{v.get('n')} transactions, {main.source_name}, {main.data_date}).")
        large = v.get("large_lots")
        if large:
            figs.append(figure("dvf_large", "médiane DVF des grandes surfaces", fmt.eur_sqm(large["median"]), large["median"], main.source_name))
            figs.append(figure("dvf_large_n", "transactions de grandes surfaces", str(large["n"]), large["n"], main.source_name))
            txt += f" Sur les grandes surfaces, la médiane est de {fmt.eur_sqm(large['median'])} ({large['n']} transactions)."
        if len(dvf_pts) > 1:
            alt = dvf_pts[1]
            am = _v(alt, "median")
            figs.append(figure("firmio_median", "médiane Firmio (recoupement, confiance faible)",
                               fmt.eur_sqm(am), am, f"{alt.source_name}, {alt.data_date}"))
            txt += f" Le recoupement Firmio, de faible confiance, donne {fmt.eur_sqm(am)}."
        if band is not None:
            txt += " " + _price_verdict(gap, band, works)
        ans["q1"] = txt
    else:
        ans["q1"] = f"{NOT_AVAILABLE} : transactions comparables (DVF) non collectées."

    # q2 : loyer
    avg = _primary(registry, "market.average_rent")
    prime = _primary(registry, "market.prime_rent")
    inputs = {p["name"]: p for p in fin.get("inputs", [])}
    parts = []
    if "relet_rent" in inputs:
        r = inputs["relet_rent"]
        figs.append(figure("relet_rent", "loyer de relocation retenu", fmt.eur_sqm(r["value"]) + "/an", r["value"],
                           r["reference"], cite=("q2",)))
        origin = {"override": "valeur imposée par l'utilisateur", "computed": "entre loyer moyen et loyer prime",
                  "registry": "loyer moyen ImmoStat, retenu faute de loyer prime sourcé"}.get(r["origin"], r["origin"])
        parts.append(f"Le loyer de relocation retenu est de {fmt.eur_sqm(r['value'])}/an ({origin}).")
    if avg:
        v = avg.value if isinstance(avg.value, dict) else {"last": avg.value}
        figs.append(figure("immostat_rent", "loyer moyen de seconde main du sous-marché", fmt.eur_sqm(v["last"]) + "/an",
                           v["last"], f"{avg.source_name}, {avg.data_date}", cite=("q2",)))
        parts.append(f"ImmoStat publie un loyer moyen de seconde main de {fmt.eur_sqm(v['last'])}/an ({avg.data_date})"
                     + (f", {fmt.pct(v['yoy'])} sur un an" if v.get("yoy") is not None else "") + ".")
        if v.get("yoy") is not None:
            figs.append(figure("immostat_rent_yoy", "variation annuelle du loyer moyen", fmt.pct(v["yoy"]), v["yoy"], avg.source_name))
    if prime:
        figs.append(figure("prime_rent", "loyer prime", fmt.eur_sqm(prime.value) + "/an", prime.value, prime.source_name))
        parts.append(f"Le loyer prime est de {fmt.eur_sqm(prime.value)}/an ({prime.source_name}).")
    else:
        parts.append(f"{NOT_AVAILABLE} : loyer prime du sous-marché (rapports de marché).")
    ans["q2"] = " ".join(parts) if parts else f"{NOT_AVAILABLE} : loyer de marché."

    complete = fin.get("status") == "complete"
    inv = _primary(registry, "market.investment_price_per_sqm")
    inv_v = _v(inv, "last") if inv else None
    if inv:
        figs.append(figure("investment_price_sqm", "prix moyen d'investissement bureaux du sous-marché",
                           fmt.eur_sqm(inv_v), inv_v, f"{inv.source_name}, {inv.data_date}", cite=("q3",) if complete else ()))
    if not complete:
        missing = labels(fin.get("missing_inputs", []))
        ans["q3"] = f"{NOT_AVAILABLE} : coût total non calculable (données manquantes : {missing})."
        ans["q4"] = f"{NOT_AVAILABLE} : yield on cost et taux de sortie non calculables (données manquantes : {missing})."
        summary = (f"Modèle financier incomplet : {missing}. " + ans["q1"])
        return figs, summary, ans

    r = fin["results"]
    c, p, rt, ex = r["cost"], r["profitability"], r["returns"], r["exit"]
    figs += [figure("total_cost", "coût total", fmt.meur(c["total_cost"]), c["total_cost"], "moteur"),
             figure("total_cost_sqm", "coût total au m² après travaux", fmt.eur_sqm(c["total_cost_per_sqm_after"]),
                    c["total_cost_per_sqm_after"], "moteur", cite=("q3",)),
             figure("exit_value_sqm", "valeur de sortie brute au m²", fmt.eur_sqm(ex["gross_value_per_sqm"]),
                    ex["gross_value_per_sqm"], "moteur")]
    # q3
    txt = f"Le coût total atteint {fmt.meur(c['total_cost'])}, soit {fmt.eur_sqm(c['total_cost_per_sqm_after'])} après travaux."
    if inv:
        gap = c["total_cost_per_sqm_after"] / inv_v - 1
        figs.append(figure("gap_cost_value", "écart du coût au m² au prix d'investissement du secteur", fmt.pct(gap), gap, "calcul"))
        txt += (f" Le prix moyen d'investissement des bureaux du secteur est de {fmt.eur_sqm(inv_v)} "
                f"({inv.source_name}, {inv.data_date}) : le coût ressort {fmt.pct(abs(gap))} "
                f"{'au-dessus' if gap > 0 else 'en dessous'} de cette référence.")
    txt += f" La valeur de sortie modélisée est de {fmt.eur_sqm(ex['gross_value_per_sqm'])}."
    ans["q3"] = txt

    # q4
    ey = inputs.get("exit_yield", {})
    method = {"override": "valeur imposée", "computed": "calculé"}.get(ey.get("origin"), "")
    rate = rt["interest_rate"]
    gap_debt = (p["yield_on_cost"] - rate) * 10_000
    figs += [figure("yoc", "yield on cost", fmt.pct(p["yield_on_cost"], 2), p["yield_on_cost"], "moteur", cite=("q4", "summary")),
             figure("exit_yield", "taux de sortie", fmt.pct(p["exit_yield"], 2), p["exit_yield"], ey.get("note") or ey.get("reference"),
                    cite=("q4",)),
             figure("spread", "spread yield on cost / taux de sortie", fmt.bps(p["spread_bps"]), p["spread_bps"], "moteur",
                    cite=("q4",)),
             figure("spread_target", "spread minimal visé par le fonds", fmt.bps(fin["targets_full"]["spread_bps"]),
                    fin["targets_full"]["spread_bps"], "config/investor_targets.yaml"),
             figure("debt_rate", "coût de la dette (Euribor + marge)", fmt.pct(rate, 2), rate, "BCE + case", cite=("q4",)),
             figure("yoc_vs_debt", "écart yield on cost / coût de la dette", fmt.bps(gap_debt), gap_debt, "calcul"),
             figure("margin", "marge sur coût", fmt.pct(p["margin_on_cost"]), p["margin_on_cost"], "moteur", cite=("summary",)),
             figure("levered_irr", "TRI fonds propres", fmt.pct(rt["levered_irr"]), rt["levered_irr"], "moteur",
                    cite=("summary",)),
             figure("unlevered_irr", "TRI projet", fmt.pct(rt["unlevered_irr"]), rt["unlevered_irr"], "moteur"),
             figure("multiple", "multiple de fonds propres", fmt.mult(rt["equity_multiple"]), rt["equity_multiple"], "moteur"),
             figure("max_price", "prix maximum (objectifs du fonds)", fmt.meur(fin["max_price"]["max_price"]),
                    fin["max_price"]["max_price"], "moteur", cite=("summary",))]
    ok = p["spread_bps"] >= fin["targets_full"]["spread_bps"]
    ans["q4"] = (f"Le yield on cost est de {fmt.pct(p['yield_on_cost'], 2)} pour un taux de sortie de {fmt.pct(p['exit_yield'], 2)}"
                 f"{' (' + method + ')' if method else ''}, soit un spread de {fmt.bps(p['spread_bps'])}, "
                 f"{'au-dessus' if ok else 'en dessous'} du minimum de {fmt.bps(fin['targets_full']['spread_bps'])} visé. "
                 f"Face à un coût de la dette de {fmt.pct(rate, 2)}, l'écart est de {fmt.bps(gap_debt)}. "
                 f"La marge sur coût est de {fmt.pct(p['margin_on_cost'])} et le TRI fonds propres de {fmt.pct(rt['levered_irr'])}.")
    lev, unlev = rt["levered_irr"], rt["unlevered_irr"]
    if gap_debt < 0:
        ans["q4"] += (" L'effet de levier est donc négatif : la dette coûte plus que le rendement de l'opération"
                      + (f", ce qui explique un TRI fonds propres ({fmt.pct(lev)}) inférieur au TRI projet ({fmt.pct(unlev)})."
                         if lev is not None and unlev is not None and lev < unlev else "."))
    else:
        ans["q4"] += " L'effet de levier est positif : le rendement de l'opération dépasse le coût de la dette."
    summary = (f"Au prix demandé de {fmt.meur(price)}, le scénario central donne un yield on cost de {fmt.pct(p['yield_on_cost'], 2)}, "
               f"une marge de {fmt.pct(p['margin_on_cost'])} et un TRI fonds propres de {fmt.pct(rt['levered_irr'])}. "
               f"Le prix maximum compatible avec les objectifs du fonds est de {fmt.meur(fin['max_price']['max_price'])}.")
    return figs, summary, ans


# ------------------------------------------------------------------ risk_analysis (q6)

def risk_figures(risks_out: dict) -> tuple[list[dict], str, dict]:
    figs, by_cat = [], {}
    for r in risks_out["risks"]:
        fe = r.get("financial_effect") or {}
        val = (f"P {r['probability']}/5 x I {r['impact']}/5 = {r['criticality']}/25, {r['level']}" if r["criticality"]
               else f"probabilité {r['probability']}/5, impact non chiffré")
        key = r["level"] in ("critique", "élevé")
        figs.append(figure(r["id"], r["title"], val, r["criticality"], "règles de notation",
                           cite=("q6", "summary") if key else (), cite_label=True))
        if fe.get("delta_margin_pts") is not None:
            figs.append(figure(f"{r['id']}_margin", f"effet sur la marge : {r['title']}", fmt.pts(fe["delta_margin_pts"]),
                               fe["delta_margin_pts"], "moteur"))
        if fe.get("delta_irr_pts") is not None:
            figs.append(figure(f"{r['id']}_irr", f"effet sur le TRI fonds propres : {r['title']}", fmt.pts(fe["delta_irr_pts"]),
                               fe["delta_irr_pts"], "moteur"))
        by_cat.setdefault(r["category"], []).append(r)
    if not risks_out["risks"]:
        return figs, f"{NOT_AVAILABLE} : aucun risque caractérisé par des données.", {"q6": f"{NOT_AVAILABLE}."}
    parts = []
    for cat, rs in by_cat.items():
        items = []
        for r in rs:
            fe = r.get("financial_effect") or {}
            s = f"{r['title']} ({r['level']}" + (f", criticité {r['criticality']}/25" if r["criticality"] else "")
            if fe.get("delta_margin_pts") is not None:
                s += f", marge {fmt.pts(fe['delta_margin_pts'])}"
            s += ")"
            items.append(s)
        parts.append(f"{CATEGORY_LABELS[cat].capitalize()} : " + " ; ".join(items) + ".")
    crit = [r["title"] for r in risks_out["risks"] if r["level"] == "critique"]
    high = [r["title"] for r in risks_out["risks"] if r["level"] == "élevé"]
    summary = f"{len(risks_out['risks'])} risques sont caractérisés par des données."
    if crit:
        summary += f" Risques critiques : {', '.join(crit)}."
    if high:
        summary += f" Risques élevés : {', '.join(high)}."
    return figs, summary, {"q6": " ".join(parts)}


# ------------------------------------------------------------------ sensitivity_analysis (q5)

def sensitivity_figures(sens: dict) -> tuple[list[dict], str, dict]:
    if sens.get("status") != "complete":
        msg = f"{NOT_AVAILABLE} : modèle financier incomplet ({labels(sens.get('missing_inputs', []))})."
        return [], msg, {"q5": msg}
    figs, parts = [], []
    for i, row in enumerate(sens["tornado"]["rows"][:4]):
        figs.append(figure(f"tornado_{row['variable']}", f"amplitude sur le TRI fonds propres : {row['label']}",
                           fmt.pts(row["irr_range_pts"]), row["irr_range_pts"], "moteur",
                           cite=("summary",) if i < 3 else ()))
    top = ", ".join(f"{r['label']} ({fmt.pts(r['irr_range_pts'])})" for r in sens["tornado"]["rows"][:3])
    fmt_value = {"rent": fmt.eur_sqm, "capex": fmt.eur_sqm, "exit_yield": lambda v: fmt.pct(v, 2),
                 "euribor": lambda v: fmt.pct(v, 2)}
    for name, b in sens["breakeven"].items():
        f = fmt_value.get(name)
        if f and b.get("central_value") is not None:
            mz, it = b.get("margin_zero_value"), b.get("irr_target_value")
            figs.append(figure(f"be_{name}_central", f"{b['label']} : valeur centrale", f(b["central_value"]), b["central_value"], "moteur"))
            short = b["label"].split(" (")[0]
            if mz is not None:
                figs.append(figure(f"be_{name}_margin", f"{b['label']} : marge nulle à", f(mz), mz, "moteur", cite=("q5",)))
                parts.append(f"{short} : marge nulle à {f(mz)} (valeur centrale {f(b['central_value'])})")
            else:
                parts.append(f"{short} : la marge reste {'négative' if b.get('central_margin_negative') else 'positive'} "
                             "sur toute la plage testée")
            if it is not None:
                figs.append(figure(f"be_{name}_irr", f"{b['label']} : TRI à l'objectif à", f(it), it, "moteur"))
        elif b.get("integer"):
            mz = b.get("margin_zero_at")
            if mz is not None:
                figs.append(figure(f"be_{name}_margin", f"{b['label']} : marge nulle après", f"+{int(mz)} mois", mz, "moteur"))
                parts.append(f"{b['label'].split(' (')[0]} : marge nulle au-delà de +{int(mz)} mois de retard")
            else:
                parts.append(f"{b['label'].split(' (')[0]} : la marge reste "
                             f"{'négative' if b.get('central_margin_negative') else 'positive'} sur toute la plage testée")
    answer = (f"Les variables les plus déterminantes sont : {top}. " + ("Seuils de bascule : " + " ; ".join(parts) + "." if parts else ""))
    summary = f"Classement des sensibilités : {top}."
    return figs, summary, {"q5": answer}


# ------------------------------------------------------------------ scenario_analysis (q7)

def scenario_figures(sc: dict) -> tuple[list[dict], str, dict]:
    if sc.get("status") != "complete":
        msg = f"{NOT_AVAILABLE} : modèle financier incomplet ({labels(sc.get('missing_inputs', []))})."
        return [], msg, {"q7": msg}
    figs, parts = [], []
    for name, v in sc["scenarios"].items():
        m = v["metrics"]
        irr_txt = fmt.pct(m["levered_irr"]) if m["levered_irr"] is not None else "non calculable (perte en capital)"
        figs += [figure(f"{name}_irr", f"TRI fonds propres, scénario {v['label']}", irr_txt, m["levered_irr"], "moteur"),
                 figure(f"{name}_margin", f"marge, scénario {v['label']}", fmt.pct(m["margin_on_cost"]), m["margin_on_cost"], "moteur",
                        cite=("q7", "summary")),
                 figure(f"{name}_max_price", f"prix maximum, scénario {v['label']}", fmt.meur(v["max_price"]), v["max_price"], "moteur")]
        parts.append(f"{v['label']} : TRI fonds propres {irr_txt}, marge {fmt.pct(m['margin_on_cost'])}, "
                     f"prix maximum {fmt.meur(v['max_price'])}{', objectifs atteints' if v['meets_targets'] else ''}")
    meets = [v["label"] for v in sc["scenarios"].values() if v["meets_targets"]]
    answer = " ; ".join(parts) + "." + (f" Les objectifs du fonds ne sont atteints que dans : {', '.join(meets)}." if meets
                                         else " Les objectifs du fonds ne sont atteints dans aucun scénario.")
    return figs, "Scénarios : " + " ; ".join(parts) + ".", {"q7": answer}
