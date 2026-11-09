"""Dashboard HTML autonome (specification_dashboard_helios.md, section 9.2).

Étapes :
  1. charger analysis_results.json, data_registry.json, asset_config.json (+ config du fonds) ;
  2. construire l'objet DATA au format de la section 3 (`build_data`), puis le VALIDER (report/dashboard_schema.py) :
     refus d'écrire si un bloc obligatoire manque ;
  3. injecter DATA en JSON dans le gabarit (échappement de « </ ») ;
  4. inliner CSS et JS (tout est dans le gabarit) ;
  5. écrire output/dashboard.html.

Aucune donnée du deal ici : tout vient des sorties de l'agent et de asset_config.json. Les champs du contrat que
l'agent ne produit pas restent à null (affichés « non disponible ») et sont listés dans CONTRACT_GAPS.

    python -m report.dashboard                                  # à partir de output/ (sorties de run.py)
    python -m report.dashboard --data fixtures/data_sample.json # à partir d'un DATA déjà au format du contrat
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from report.dashboard_schema import validate_data

TEMPLATES = Path(__file__).parent / "templates"
TEMPLATE_NAME = "dashboard.html.j2"
ENGINE_PATH = Path(__file__).parent / "js" / "engine.js"   # moteur testé contre Python (tests/test_js_engine.py, golden_cases)

DECISIONS = {"GO": "GO", "GO AVEC CONTRAINTES": "GO_AVEC_CONTRAINTES", "GO SOUS CONDITIONS": "GO_SOUS_CONDITIONS",
             "NO-GO": "NO_GO", "NON DÉTERMINÉ": "NON_DETERMINE"}
ENGINE = "Moteur financier de l'agent"

# champs du contrat (section 3.1) que l'agent ne produit pas aujourd'hui : laissés à null / vides, jamais inventés
CONTRACT_GAPS = {
    "market.transactions": "le connecteur DVF ne conserve que des statistiques agrégées (médiane, quartiles, par année), "
                           "pas la liste des mutations ni leurs coordonnées",
    "market.rents.prime": "loyer prime : connecteur des rapports de marché non implémenté",
    "market.yields.prime": "taux de rendement prime : connecteur des rapports de marché non implémenté",
    "market.supply_demand.vacancy_pct": "taux de vacance : connecteur des rapports de marché non implémenté",
    "market.supply_demand.future_supply_sqm": "offre future : connecteur des rapports de marché non implémenté",
    "market.plu.zone": "le connecteur PLU ne renvoie pas de libellé de zonage",
    "market.plu.heritage_constraints": "pas de texte produit par l'agent (seulement un décompte des bâtiments protégés)",
    "market.plu.rooftop_extension_notes": "aucune analyse de faisabilité du rooftop produite par l'agent",
    "financials.cashflows[].noi / capex": "le moteur ne ventile pas les flux trimestriels entre revenus et travaux "
                                         "(il fournit le flux net, les tirages et les intérêts)",
    "sensitivity.tornado[].low_case / high_case": "le moteur stocke les écarts de résultat, pas la valeur testée de la variable",
}


# ------------------------------------------------------------------ outils

def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _primary(registry: dict, key: str) -> dict | None:
    pts = [p for p in registry.get(key, []) if p["status"] in ("ok", "partial", "assumption")]
    return pts[0] if pts else None


def _field(dp: dict | None, name: str = "last"):
    if dp is None:
        return None
    v = dp["value"]
    if isinstance(v, dict):
        return v.get(name)
    return v if name in ("last", "median", "value") else None


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# formules des grandeurs calculées (texte générique, affiché au survol de la pastille « ƒ »)
FORMULAS = {
    "price_per_sqm_asked": "prix demandé / surface avant travaux",
    "price_vs_median_pct": "prix demandé au m² / médiane des transactions − 1",
    "value_range.low": "1er quartile des transactions × surface avant travaux",
    "value_range.high": "3e quartile des transactions × surface avant travaux",
    "total_cost.acquisition": "prix d'acquisition",
    "total_cost.fees": "prix × taux de frais d'acquisition",
    "total_cost.works": "coût des travaux au m² × surface après travaux",
    "total_cost.contingency": "travaux × taux d'honoraires et aléas",
    "total_cost.financing_costs": "intérêts capitalisés + commission d'arrangement",
    "total_cost.other": "portage des surfaces vides + honoraires de commercialisation",
    "total_cost.total": "somme des postes de coût",
    "total_cost.per_sqm_after": "coût total / surface après travaux",
    "noi_stabilized": "loyer × surface × occupation × (1 − charges non récupérables) − portage des surfaces vides",
    "yoc_pct": "revenu net stabilisé / coût total",
    "exit_value": "revenu net stabilisé / taux de sortie − déductions à la cession − frais de cession",
    "margin_eur": "valeur nette de sortie − coût total",
    "margin_pct": "marge / coût total",
    "spread_bps": "(yield on cost − taux de sortie) × 10 000",
    "irr_project": "TRI des flux trimestriels du projet sans dette, annualisé",
    "irr_equity": "TRI des flux trimestriels des fonds propres, annualisé",
    "equity_multiple": "somme des distributions / somme des apports des fonds propres",
    "peak_equity": "apport cumulé maximal des fonds propres",
    "max_price.central": "prix d'acquisition le plus élevé respectant les objectifs de TRI fonds propres et de marge (dichotomie)",
    "max_price.pessimistic": "prix maximum du scénario défavorable",
}
UNITS = {"price_per_sqm_asked": "€/m²", "price_vs_median_pct": "fraction", "total_cost.per_sqm_after": "€/m²",
         "yoc_pct": "fraction", "margin_pct": "fraction", "spread_bps": "bps", "irr_project": "fraction",
         "irr_equity": "fraction", "equity_multiple": "x"}


# ------------------------------------------------------------------ construction de DATA à partir des sorties de l'agent

def build_data(analysis: dict, registry: dict, config, defaults, generated_at: str | None = None) -> dict[str, Any]:
    fin, rec, risks_out = analysis.get("financials", {}), analysis.get("recommendation", {}), analysis.get("risks", {})
    complete = fin.get("status") == "complete"
    r = fin.get("results") or {}
    inputs = {p["name"]: p for p in fin.get("inputs", [])}
    adate = str(analysis.get("analysis_date"))[:10]
    fallbacks = [p["name"] for p in fin.get("inputs", []) if p.get("fallback")]
    dps: dict[str, dict] = {}

    # --- registre : la première donnée de chaque clé ; les sources concurrentes en clé@1, clé@2...
    for key, pts in registry.items():
        for i, p in enumerate(pts):
            dps[key if i == 0 else f"{key}@{i}"] = dict(p)

    from report.labels import PARAM_LABELS
    def calc(key, value, unit, notes, depends=True):
        """depends=False : entrée du modèle (taux de sortie, loyer…) qui ne dépend pas des valeurs de repli des autres entrées."""
        dep = f" Dépend de valeurs de repli : {', '.join(PARAM_LABELS.get(f, f) for f in fallbacks)}." if fallbacks and depends else ""
        dps[key] = {"key": key, "value": value, "unit": unit, "source_name": ENGINE, "source_url": None, "data_date": adate,
                    "retrieved_at": None, "method": "computed", "status": "ok" if value is not None else "missing",
                    "confidence": "medium" if fallbacks and depends else "high", "notes": notes + dep}

    def conf(key, value, unit, src, notes, assumption=False):
        dps[key] = {"key": key, "value": value, "unit": unit, "source_name": src, "source_url": None, "data_date": None,
                    "retrieved_at": None, "method": "assumption" if assumption else "user_input",
                    "status": "assumption" if assumption else "ok", "confidence": "medium" if assumption else "high", "notes": notes}

    # --- market
    dvf = _primary(registry, "market.dvf_price_per_sqm")
    by_year = (_field(dvf, "by_year") or {}) if dvf else {}
    ademe = _primary(registry, "energy.dpe_comparables")
    by_class = _field(ademe, "by_class") if ademe else None
    plu = _primary(registry, "urbanism.plu_constraints")
    euribor, ilat = _primary(registry, "rates.euribor_3m"), _primary(registry, "index.ilat")
    relet, rf, firm = (inputs.get(k, {}).get("value") for k in ("relet_rent", "rent_free_months", "firm_lease_term_months"))
    effective = relet * (1 - rf / firm) if all(_num(x) is not None for x in (relet, rf, firm)) and firm else None
    market = {
        "transactions": [],
        "transactions_stats": {"median": _num(_field(dvf, "median")), "q1": _num(_field(dvf, "q1")), "q3": _num(_field(dvf, "q3")),
                               "count": _field(dvf, "n"),
                               "by_year": [{"year": int(y), "median": v.get("median"), "count": v.get("n")} for y, v in sorted(by_year.items())]},
        "rents": {"prime": _num(_field(_primary(registry, "market.prime_rent"))), "average": _num(_field(_primary(registry, "market.average_rent"))),
                  "relet_assumed": _num(relet), "incentives_months": _num(rf), "effective": effective},
        "yields": {"prime": _num(_field(_primary(registry, "market.prime_yield"))), "exit_assumed": _num(inputs.get("exit_yield", {}).get("value"))},
        "supply_demand": {"vacancy_pct": _num(_field(_primary(registry, "market.vacancy"))),
                          "future_supply_sqm": _num(_field(_primary(registry, "market.future_supply"))),
                          "take_up_sqm": _num(_field(_primary(registry, "market.take_up")))},
        "rates": {"euribor3m_series": [{"date": p["period"], "value": p["value"]} for p in (_field(euribor, "series") or [])],
                  "euribor3m_last": _num(_field(euribor)),
                  "ilat_series": [{"date": p["period"], "value": p["value"]} for p in (_field(ilat, "series") or [])],
                  "debt_rate_all_in": (r.get("returns") or {}).get("interest_rate")},
        "dpe_comparables": {"distribution": {c: (by_class.get(c, 0) if isinstance(by_class, dict) else None) for c in "ABCDEFG"},
                            "count": _field(ademe, "n_with_label") if ademe else None},
        "plu": {"zone": None, "max_height_m": _num(_field(plu, "max_height")), "heritage_constraints": None, "rooftop_extension_notes": None},
    }
    calc("market.rents.effective", effective, "€/m²/an", "loyer de relocation × (1 − franchise / durée ferme du bail)")
    for k, src in (("relet_rent", "market.rents.relet_assumed"), ("rent_free_months", "market.rents.incentives_months"),
                   ("exit_yield", "market.yields.exit_assumed")):
        p = inputs.get(k)
        if p:
            calc(src, p["value"], p.get("unit"), p.get("note") or p.get("reference") or "", depends=False)
            dps[src]["confidence"] = "medium"   # donnée de marché combinée à une hypothèse (prime de risque, position…)
            if p.get("origin") == "assumption" or p.get("fallback"):
                dps[src].update(method="assumption", status="assumption", confidence="low")
    calc("market.rates.debt_rate_all_in", market["rates"]["debt_rate_all_in"], "fraction", "Euribor 3 mois (dernière valeur) + marge bancaire")

    # --- financials
    ref = next((x for x in (fin.get("price_comparison") or {}).get("references", []) if x["key"] == "market.dvf_price_per_sqm"), {})
    cost, prof, ret, ext, rev = (r.get(k, {}) for k in ("cost", "profitability", "returns", "exit", "revenue"))
    mp = fin.get("max_price") or {}
    sc = (analysis.get("scenarios") or {}).get("scenarios") or {}
    rng = ref.get("value_range") or [None, None]
    financials = {
        "price_per_sqm_asked": (r.get("acquisition") or {}).get("price_per_sqm_before"),
        "price_vs_median_pct": ref.get("gap_pct"),
        "value_range": {"low": rng[0], "high": rng[1]},
        "total_cost": {"acquisition": cost.get("acquisition_price"), "fees": cost.get("acquisition_fees"), "works": cost.get("capex"),
                       "contingency": cost.get("fees_contingency"),
                       "financing_costs": (cost["interest"] + cost["arrangement_fee"]) if complete else None,
                       "other": (cost["holding_costs"] + cost["marketing_fees"]) if complete else None,
                       "total": cost.get("total_cost"), "per_sqm_after": cost.get("total_cost_per_sqm_after")},
        "noi_stabilized": rev.get("noi_stabilised"), "yoc_pct": prof.get("yield_on_cost"), "exit_value": ext.get("net_value"),
        "margin_eur": prof.get("margin"), "margin_pct": prof.get("margin_on_cost"), "spread_bps": prof.get("spread_bps"),
        "irr_project": ret.get("unlevered_irr"), "irr_equity": ret.get("levered_irr"), "equity_multiple": ret.get("equity_multiple"),
        "peak_equity": ret.get("peak_equity"),
        "max_price": {"central": mp.get("max_price"), "pessimistic": (sc.get("defavorable") or {}).get("max_price"),
                      "target_used": mp.get("binding_target")},
        "cashflows": [{"period": c["period"], "noi": None, "capex": None, "debt_draw": c["debt_draw"], "interest": c["interest"],
                       "net": c["unlevered"]} for c in (r.get("cashflows") or [])],
    }

    def walk(prefix, obj, path=""):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if isinstance(v, dict):
                walk(prefix, v, p)
            elif (isinstance(v, (int, float)) and not isinstance(v, bool)) or v is None:
                if p in FORMULAS:
                    unit = UNITS.get(p, "€")
                    calc(f"{prefix}.{p}", v, unit, FORMULAS[p])
    walk("financials", {k: v for k, v in financials.items() if k != "cashflows"})
    calc("financials.max_price_gap_pct", mp.get("gap_to_asking_pct"), "fraction", "prix maximum / prix demandé − 1")
    calc("financials.price_per_sqm_asked_after", (r.get("acquisition") or {}).get("price_per_sqm_after"), "€/m²",
         "prix demandé / surface après travaux")

    # --- sensitivity
    se = analysis.get("sensitivity") or {}
    unit_of = {"relet_rent": "€/m²/an", "exit_yield": "fraction", "capex_per_sqm": "€/m²", "euribor": "fraction"}
    tornado, breakeven, heatmaps = [], [], []
    if se.get("status") == "complete":
        for row in se["tornado"]["rows"]:
            for metric, key in (("margin_pct", "delta_margin_pts"), ("irr_equity", "delta_irr_pts")):
                tornado.append({"variable": row["variable"], "label": row["label"], "low_case": None, "high_case": None, "metric": metric,
                                "delta_low": row[key]["pessimistic"], "delta_high": row[key]["optimistic"]})
        for name, b in se["breakeven"].items():
            if b.get("integer"):
                breakeven.append({"variable": name, "label": b["label"], "base": 0, "breakeven": b.get("margin_zero_at"), "unit": "mois"})
            else:
                breakeven.append({"variable": name, "label": b["label"], "base": b.get("central_value"),
                                  "breakeven": b.get("margin_zero_value"), "unit": unit_of.get(b.get("field"))})
        for hid, h in se["heatmaps"].items():
            for metric, key in (("margin_pct", "margin_on_cost"), ("irr_equity", "levered_irr")):
                heatmaps.append({"id": f"{hid}.{metric}", "x_label": h["x"]["label"], "y_label": h["y"]["label"],
                                 "x_values": h["x"]["values"], "y_values": h["y"]["values"], "z": h[key], "metric": metric})
    for b in breakeven:
        calc(f"sensitivity.breakeven.{b['variable']}.base", b["base"], b["unit"], f"{b['label']} : valeur du scénario central")
        calc(f"sensitivity.breakeven.{b['variable']}.breakeven", b["breakeven"], b["unit"],
             f"{b['label']} : valeur pour laquelle la marge s'annule, les autres hypothèses restant au central (dichotomie)")
    if se.get("status") == "complete":
        for name, b in se["breakeven"].items():
            unit = "mois" if b.get("integer") else unit_of.get(b.get("field"))
            calc(f"sensitivity.breakeven.{name}.irr_target", b.get("irr_target_at") if b.get("integer") else b.get("irr_target_value"),
                 unit, f"{b['label']} : valeur pour laquelle le TRI fonds propres atteint l'objectif du fonds")
    for metric in ("margin_pct", "irr_equity"):
        calc(f"sensitivity.tornado.{metric}", [r for r in tornado if r["metric"] == metric] or None, "pts",
             "écart en points par rapport au central quand une seule hypothèse passe à sa valeur optimiste ou pessimiste")
    for h in heatmaps:
        calc(f"sensitivity.heatmaps.{h['id']}", h["z"], "fraction", f"{h['y_label']} × {h['x_label']} : moteur rejoué pour chaque case")
    dash = defaults.dashboard
    mi = fin.get("model_inputs") or {}
    if dash:
        conf("dashboard.sliders", [x.model_dump() for x in dash.sliders], None, "Configuration du dashboard (config/defaults.yaml)",
             "plages des curseurs du simulateur (spécification du dashboard, section 7.2), en écart au scénario central")
        for sl in dash.sliders:
            calc(f"engine.inputs.{sl.field}", mi.get(sl.field), {"relet_rent": "€/m²/an", "capex_per_sqm": "€/m²", "price": "€",
                                                                  "works_months": "mois", "letting_void_months": "mois"}.get(sl.field, "fraction"),
                 f"valeur centrale de « {sl.label} » utilisée par le moteur")
    sensitivity = {"tornado": tornado, "breakeven": breakeven, "heatmaps": heatmaps,
                   "engine_params": {"inputs": fin.get("model_inputs"), "price_targets": fin.get("targets"),
                                     "sliders": [x.model_dump() for x in dash.sliders] if dash else [],
                                     "variables": {k: {"label": v.label, "optimistic": [c.model_dump() for c in v.optimistic],
                                                       "pessimistic": [c.model_dump() for c in v.pessimistic]}
                                                   for k, v in (defaults.sensitivity.variables.items() if defaults.sensitivity else [])},
                                     "central": {"noi_stabilized": rev.get("noi_stabilised"), "yoc_pct": prof.get("yield_on_cost"),
                                                 "spread_bps": prof.get("spread_bps"), "exit_value": ext.get("net_value"),
                                                 "margin_eur": prof.get("margin"), "margin_pct": prof.get("margin_on_cost"),
                                                 "irr_project": ret.get("unlevered_irr"), "irr_equity": ret.get("levered_irr"),
                                                 "equity_multiple": ret.get("equity_multiple")}}}

    # --- scenarios
    scenarios = [{"name": k, "assumptions": v.get("variables", {}), "yoc_pct": v["metrics"]["yield_on_cost"],
                  "spread_bps": v["metrics"]["spread_bps"], "margin_pct": v["metrics"]["margin_on_cost"],
                  "irr_equity": v["metrics"]["levered_irr"], "equity_multiple": v["metrics"]["equity_multiple"],
                  "max_price": v.get("max_price")} for k, v in sc.items()]
    sc_formula = {"yoc_pct": ("fraction", "yield on cost"), "spread_bps": ("bps", "spread yield on cost − taux de sortie"),
                  "margin_pct": ("fraction", "marge sur coût"), "irr_equity": ("fraction", "TRI fonds propres"),
                  "equity_multiple": ("x", "multiple des fonds propres"), "max_price": ("€", "prix maximum")}
    for s in scenarios:
        label = (sc.get(s["name"]) or {}).get("label", s["name"])
        rat = (sc.get(s["name"]) or {}).get("rationale", "")
        for m, (unit, what) in sc_formula.items():
            calc(f"scenarios.{s['name']}.{m}", s[m], unit, f"{what}, scénario {label.lower()} ({rat}) : moteur rejoué avec les "
                                                             "hypothèses du scénario")
            dps[f"scenarios.{s['name']}.{m}"]["label"] = f"{what}, scénario {label.lower()}"

    # --- risks
    risks = [{k: x.get(k) for k in ("id", "title", "category", "description", "evidence", "probability", "probability_rationale",
                                    "impact", "impact_rationale", "criticality", "level", "uncertain", "mitigations", "financial_link",
                                    "financial_effect", "missing_information")}
             for x in risks_out.get("risks", [])]
    RULES = "Règles de notation des risques de l'agent (risk/catalog.py)"
    deal_src_name = (registry.get("deal.asking_price_eur") or [{}])[0].get("source_name") or "asset_config.json"
    for x in risks:
        x["evidence"] = x["evidence"] or []
        x["impact_rationale"] = x["impact_rationale"] or ""
        x["missing_information"] = x["missing_information"] or []
        fe = x["financial_effect"] or {}
        x["financial_effect"] = {"shock": fe.get("shock_rationale"), "delta_margin_pts": fe.get("delta_margin_pts"),
                                 "delta_irr_pts": fe.get("delta_irr_pts")} if fe else None
        rid = x["id"]
        calc(f"risks.{rid}.criticality", x["criticality"], None, "probabilité × impact")
        calc(f"risks.{rid}.probability", x["probability"], None, "probabilité notée par règles explicites sur les données")
        calc(f"risks.{rid}.impact", x["impact"], None, "impact mesuré en rejouant le moteur avec le choc du risque")
        for k in ("delta_margin_pts", "delta_irr_pts"):
            calc(f"risks.{rid}.{k}", fe.get(k), "pts", f"effet du choc « {fe.get('shock_rationale') or 'n.d.'} » sur la "
                                                         + ("marge sur coût" if "margin" in k else "TRI fonds propres") + ", en points")
        calc(f"risks.{rid}.shock", fe.get("shock_rationale"), None, "choc appliqué au moteur pour mesurer l'impact du risque")
        for k in ("probability_rationale", "impact_rationale"):            # justification citée (contient des chiffres)
            calc(f"risks.{rid}.{k}", x[k] or None, None, "texte de justification produit par les règles de notation")
            dps[f"risks.{rid}.{k}"]["source_name"] = RULES
        for i, ev in enumerate(x["evidence"]):
            evd = ev if isinstance(ev, dict) else {"value": ev}
            src_key = str(evd.get("key") or "")
            # clé du registre éventuellement suivie d'un champ (ex. market.immediate_supply.yoy)
            reg_key = max((k for k in registry if src_key == k or src_key.startswith(k + ".")), key=len, default=None)
            field = src_key[len(reg_key) + 1:] if reg_key and src_key != reg_key else None
            base = (registry.get(reg_key) or [None])[0] if reg_key else None
            from_case = src_key.startswith("asset_config")
            unit = "fraction" if field and (field == "yoy" or field.startswith("share")) else (base or {}).get("unit")
            dps[f"risks.{rid}.evidence.{i}"] = {
                "key": f"risks.{rid}.evidence.{i}", "value": evd.get("value"), "unit": unit,
                "source_name": (base or {}).get("source_name") or (deal_src_name if from_case else evd.get("source") or RULES),
                "source_url": (base or {}).get("source_url"), "data_date": (base or {}).get("data_date") or evd.get("data_date"),
                "retrieved_at": None,
                "method": (base or {}).get("method") or ("user_input" if from_case else "computed"),
                "status": "ok", "confidence": (base or {}).get("confidence") or "medium",
                "notes": f"élément de preuve du risque {rid}" + (f" (donnée {src_key})" if src_key else "")}
    n_crit = sum(x["level"] == "critique" for x in risks)
    calc("risks.critical_count", n_crit, None, "nombre de risques de niveau critique (criticité 17 à 25)")

    # --- targets et règles du fonds
    tf = fin.get("targets_full") or {}
    status = tf.get("status") or {}
    t_assumed = any(v == "assumption" for v in status.values())
    for key, src_key, unit in (("min_levered_irr_pct", "levered_irr", "fraction"), ("min_margin_on_cost_pct", "margin_on_cost", "fraction"),
                               ("min_spread_vs_exit_bps", "spread_bps", "bps"), ("min_equity_multiple", "equity_multiple", "x")):
        conf(f"targets.{key}", tf.get(src_key), unit, "Objectifs du fonds (config/investor_targets.yaml)",
             "Objectif fixé par hypothèse, à confirmer." if t_assumed else "Objectif du fonds.", assumption=t_assumed)
    targets = {"min_spread_vs_exit_bps": tf.get("spread_bps"), "min_levered_irr_pct": tf.get("levered_irr"),
               "min_equity_multiple": tf.get("equity_multiple"), "min_margin_on_cost_pct": tf.get("margin_on_cost"),
               "source": "assumption" if t_assumed else "confirmed"}
    dr = defaults.decision_rules
    conf("rules.max_negotiable_discount", dr.max_negotiable_discount, "fraction", "Règles de décision (config/defaults.yaml)",
         "Décote maximale jugée négociable entre le prix demandé et le prix maximum.")
    conf("rules.max_critical_risks", dr.max_critical_risks_conditional, None, "Règles de décision (config/defaults.yaml)",
         "Nombre de risques critiques au-delà duquel le dossier est refusé.")

    # --- recommendation
    decision = DECISIONS.get(rec.get("decision"), "NON_DETERMINE")
    args = rec.get("arguments") or {}
    recommendation = {
        "decision": decision, "rationale": rationale(decision, rec, dps, dr),
        "pros": [tokenize(a, dps, f"recommendation.pros.{i}", adate) for i, a in enumerate(args.get("for", []))],
        "cons": [tokenize(a, dps, f"recommendation.cons.{i}", adate) for i, a in enumerate(args.get("against", []))],
        "key_indicators": [k for k in ("financials.yoc_pct", "financials.spread_bps", "financials.margin_pct", "financials.irr_equity",
                                       "financials.exit_value", "financials.max_price.central") if k in dps],
        "priority_risks": [x["id"] for x in rec.get("top_risks", []) if any(y["id"] == x["id"] for y in risks)],
        "conditions": [tokenize(c, dps, f"recommendation.conditions.{i}", adate)
                       for i, c in enumerate(rec.get("conditions_struct") or list(rec.get("conditions") or []))],
        "missing_information": sorted(set(risks_out.get("missing_information") or [])),
        "seller_requests": list(rec.get("seller_requests") or []),
        "max_price": mp.get("max_price"),
        "note": [],
    }

    # --- sources, hypothèses, manques
    by_src: dict[str, dict] = {}
    for key, pts in registry.items():
        for p in pts:
            if p["status"] in ("ok", "partial"):
                s = by_src.setdefault(p["source_name"], {"id": _slug(p["source_name"]), "name": p["source_name"], "url": p.get("source_url"),
                                                         "dates": [], "used_for": []})
                if p.get("data_date") and p["data_date"] not in s["dates"]:
                    s["dates"].append(p["data_date"])
                s["used_for"].append(key)
    sources = [{"id": s["id"], "name": s["name"], "url": s["url"], "data_date": " ; ".join(s["dates"]) or None, "used_for": s["used_for"]}
               for s in by_src.values()]
    fa = defaults.financial_assumptions
    deal_src = (registry.get("deal.asking_price_eur") or [{}])[0].get("source_name") or "asset_config.json"
    for p in fin.get("inputs", []):
        spec = fa.get(p["name"])
        method = {"config": "user_input", "override": "user_input", "assumption": "assumption", "computed": "computed"}.get(p["origin"])
        reg_dp = _primary(registry, p["reference"]) if p["origin"] == "registry" else None
        note = (spec.rationale.strip() if spec else None) or p.get("note") or p.get("reference") or ""
        if spec and spec.range:
            note += f" Fourchette usuelle : {_fmt_range(spec.range, spec.unit)}."
        dps[f"params.{p['name']}"] = {
            "key": f"params.{p['name']}", "value": p["value"], "unit": p.get("unit"),
            "source_name": (reg_dp or {}).get("source_name") or (deal_src if p["origin"] == "config" else
                                                                 "Hypothèses de modélisation (config/defaults.yaml)" if p["origin"] == "assumption" else ENGINE),
            "source_url": (reg_dp or {}).get("source_url"), "data_date": (reg_dp or {}).get("data_date"), "retrieved_at": None,
            "method": "assumption" if p.get("fallback") else (method or (reg_dp or {}).get("method") or "computed"),
            "status": "assumption" if (p.get("fallback") or p["origin"] == "assumption") else "ok",
            "confidence": "low" if p.get("fallback") else "medium" if p["origin"] == "assumption" else "high", "notes": note}
    assumptions = []
    for p in fin.get("inputs", []):
        if p["origin"] == "assumption" or p.get("fallback"):
            spec = fa.get(p["name"])
            assumptions.append({"key": f"params.{p['name']}", "value": p["value"], "unit": p.get("unit"),
                                "justification": (spec.rationale.strip() if spec else None) or p.get("note") or p.get("reference") or "",
                                "range": list(spec.range) if spec and spec.range else None})
    prio = {n.key: n.priority for n in defaults.data_needs}
    impact = {"P1": "P1 : nécessaire à la décision", "P2": "P2 : utile, remplacé par une hypothèse ou omis",
              "P3": "P3 : complémentaire"}
    missing = [{"key": k, "reason": pts[0].get("notes") or "non trouvée", "impact": impact.get(prio.get(k), "non classée")}
               for k, pts in registry.items() if pts and pts[0]["status"] == "missing"]

    asset = config.asset.model_dump(mode="json")
    operation = config.operation.model_dump(mode="json")
    # données du dossier sans DataPoint dans le registre : pastille « U », source = dossier de l'opération
    for block, obj in (("asset", asset), ("operation", operation)):
        for k, v in obj.items():
            key = f"deal.{block}.{k}"
            if not isinstance(v, dict):
                dps[key] = {"key": key, "value": v, "unit": None, "source_name": deal_src, "source_url": None, "data_date": None,
                            "retrieved_at": None, "method": "user_input", "status": "ok" if v is not None else "missing",
                            "confidence": "high", "notes": f"asset_config.json : {block}.{k}"}
    tl = dict(r.get("timeline") or {})
    if tl.get("lease_start") and tl.get("rent_free_months") is not None:
        tl["rent_start"] = _add_months(tl["lease_start"], tl["rent_free_months"])
    calc("financials.timeline", tl or None, None, "calendrier modélisé : acquisition, travaux, livraison, prise d'effet du bail, "
                                                 "fin de franchise, cession")
    calc("financials.cashflows", financials["cashflows"] or None, "€", "flux trimestriels : flux net du projet, tirages de dette, "
                                                                       "intérêts capitalisés")
    avg_rent, prime_rent = market["rents"]["average"], market["rents"]["prime"]
    pos = ((relet - avg_rent) / (prime_rent - avg_rent)) if all(_num(x) is not None for x in (relet, avg_rent, prime_rent)) \
        and prime_rent != avg_rent else None
    calc("market.rents.relet_position_pct", pos, "fraction", "(loyer retenu − loyer moyen) / (loyer prime − loyer moyen)")
    if pos is None:
        dps["market.rents.relet_position_pct"]["notes"] += " Non calculable : loyer prime non disponible."
    # sources concurrentes : écart affiché (section 11, « sources divergentes »)
    for key, pts in registry.items():
        for i, p in enumerate(pts[1:], 1):
            v0, v1 = _num(_field(pts[0], "median")), _num(_field(p, "median"))
            if v0 is None:
                v0, v1 = _num(_field(pts[0])), _num(_field(p))
            if v0 and v1 is not None:
                calc(f"{key}@{i}.gap_pct", v1 / v0 - 1, "fraction", f"écart entre « {p['source_name']} » et la source principale "
                                                                     f"« {pts[0]['source_name']} »")
    need = next((n for n in defaults.data_needs if dash and n.key == dash.low_sample_threshold_from), None)
    conf("rules.min_comparables", need.min_sample if need else None, None, "Contrôles de qualité (config/defaults.yaml)",
         "Taille d'échantillon en dessous de laquelle la confiance est jugée faible.")
    # champs du contrat que l'agent ne produit pas : donnée « manquante » explicite, avec la raison
    for path in ("market.plu.zone", "market.plu.heritage_constraints", "market.plu.rooftop_extension_notes"):
        dps[path] = {"key": path, "value": None, "unit": None, "source_name": "Agent - non produit", "source_url": None,
                     "data_date": None, "retrieved_at": None, "method": "none", "status": "missing", "confidence": "low",
                     "notes": CONTRACT_GAPS[path]}
    price_cr = (rec.get("criteria") or {}).get("price") or {}
    calc("rules.negotiable_floor_price", price_cr.get("floor"), "€",
         "prix demandé × (1 − décote maximale jugée négociable) : en dessous, l'écart n'est plus jugé négociable")
    m_cr = (rec.get("criteria") or {}).get("M") or {}
    if m_cr.get("constraint") and _num(m_cr.get("target_price")) is not None:
        mp_v = price_cr.get("max_price")
        calc("rules.market_price_target", min(m_cr["target_price"], mp_v) if _num(mp_v) is not None else m_cr["target_price"], "€",
             "référence de marché au m² × (1 − bande d'écart) × surface actuelle, plafonnée au prix maximum : prix sous lequel "
             "négocier pour que le prix au m² passe sous le marché (critère M)")
    note, dropped = [], []
    for i, line in enumerate(rec.get("note_struct") or []):
        t = tokenize(line, dps, f"recommendation.note.{i}", adate) if line.get("slots") or not re.search(r"\d", line["text"]) \
            else tokenize_free(line["text"], dps)
        if t is None:
            dropped.append(line["text"])
        else:
            note.append(t)
    recommendation["note"] = note
    # texte explicatif final (décision, risques déclarés, hypothèses à valider, seuils de bascule)
    explanation = []
    for si, sec in enumerate(rec.get("explanation") or []):
        paras = []
        for pi, par in enumerate(sec["paragraphs"]):
            t = tokenize(par, dps, f"recommendation.explanation.{si}.{pi}", adate) if par.get("slots") \
                or not re.search(r"\d", par["text"]) else tokenize_free(par["text"], dps)
            if t is None:
                dropped.append(par["text"])
            else:
                paras.append(t)
        explanation.append({"title": sec["title"], "paragraphs": paras})
    recommendation["explanation"] = explanation
    if dropped:
        recommendation["missing_information"].append(
            "lignes de la note rédigées par le LLM non reprises dans le dashboard : un de leurs chiffres n'est rattaché à aucune donnée")
    sweep_effects(fin, defaults, calc)
    label_all(dps)
    return {
        "meta": {"asset_name": asset["name"], "analysis_date": adate,
                 "generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "currency": "EUR", "language": "fr"},
        "deal": {"asset": asset, "operation": operation},
        "datapoints": dps, "market": market, "financials": financials, "sensitivity": sensitivity, "scenarios": scenarios,
        "risks": risks, "targets": targets, "recommendation": recommendation, "sources": sources,
        "assumptions": assumptions, "missing": missing,
    }


COMPUTED_LABELS = {
    "financials.price_per_sqm_asked": "prix demandé au m² (surface avant travaux)",
    "financials.price_per_sqm_asked_after": "prix demandé au m² (surface après travaux)",
    "financials.price_vs_median_pct": "écart du prix demandé à la médiane des transactions",
    "financials.value_range.low": "fourchette de valeur indicative (bas)", "financials.value_range.high": "fourchette de valeur indicative (haut)",
    "financials.total_cost.acquisition": "prix d'acquisition", "financials.total_cost.fees": "frais d'acquisition",
    "financials.total_cost.works": "travaux", "financials.total_cost.contingency": "honoraires et aléas",
    "financials.total_cost.financing_costs": "frais financiers", "financials.total_cost.other": "portage et commercialisation",
    "financials.total_cost.total": "coût total", "financials.total_cost.per_sqm_after": "coût total au m² après travaux",
    "financials.noi_stabilized": "revenu net stabilisé (NOI)", "financials.yoc_pct": "yield on cost",
    "financials.exit_value": "valeur nette de sortie", "financials.margin_eur": "marge (€)", "financials.margin_pct": "marge sur coût",
    "financials.spread_bps": "spread yield on cost − taux de sortie", "financials.irr_project": "TRI projet (sans dette)",
    "financials.irr_equity": "TRI fonds propres", "financials.equity_multiple": "multiple des fonds propres",
    "financials.peak_equity": "besoin maximal en fonds propres", "financials.max_price.central": "prix maximum (scénario central)",
    "financials.max_price.pessimistic": "prix maximum (scénario défavorable)", "financials.max_price_gap_pct": "écart du prix maximum au prix demandé",
    "financials.timeline": "calendrier modélisé", "financials.cashflows": "flux trimestriels",
    "market.rents.effective": "loyer économique (après franchise)", "market.rents.relet_assumed": "loyer de relocation retenu",
    "market.rents.incentives_months": "franchise de loyer", "market.rents.relet_position_pct": "position du loyer retenu entre moyen et prime",
    "market.yields.exit_assumed": "taux de sortie retenu", "market.rates.debt_rate_all_in": "coût de la dette tout compris",
    "market.plu.zone": "zonage PLU", "market.plu.heritage_constraints": "contraintes patrimoniales",
    "market.plu.rooftop_extension_notes": "faisabilité du rooftop et de l'extension",
    "rules.negotiable_floor_price": "prix plancher négociable", "rules.market_price_target": "cible de négociation (prix au m² sous le marché)",
    "sensitivity.sweep.relet_rent": "effet sur la marge : loyer retenu", "sensitivity.sweep.exit_yield": "effet sur la marge : taux de sortie",
    "sensitivity.sweep.rent_free_months": "effet sur la marge : franchise",
    "risks.critical_count": "nombre de risques critiques", "rules.max_critical_risks": "nombre maximal de risques critiques admis",
    "rules.max_negotiable_discount": "décote maximale jugée négociable", "rules.min_comparables": "seuil d'échantillon faible",
    "targets.min_levered_irr_pct": "objectif de TRI fonds propres", "targets.min_margin_on_cost_pct": "objectif de marge sur coût",
    "targets.min_spread_vs_exit_bps": "objectif de spread", "targets.min_equity_multiple": "objectif de multiple",
}


DEAL_LABELS = {
    "asking_price_eur": "prix demandé", "surface_before_sqm": "surface avant travaux", "surface_after_sqm": "surface après travaux",
    "capex_eur_per_sqm_after": "coût des travaux au m²", "fees_and_contingency_pct_of_capex": "honoraires et aléas",
    "occupancy_current_pct": "taux d'occupation actuel", "ltv": "LTV envisagée", "debt_margin_bps": "marge bancaire",
    "dpe_current": "DPE actuel", "dpe_target": "DPE visé", "current_leases": "baux en place", "timeline": "calendrier du dossier",
    "acquisition_fees_pct": "frais d'acquisition", "name": "nom de l'actif", "type": "type d'actif", "district": "arrondissement",
    "sector": "secteur", "submarket": "sous-marché", "condition": "état de l'actif", "year_built_decade": "décennie de construction",
    "floors": "niveaux", "price_basis": "base du prix", "strategy": "stratégie", "project_scope": "programme de travaux",
}


def _fmt_range(r, unit) -> str:
    def f(v):
        if unit == "fraction":
            return f"{v * 100:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " %"
        return f"{v:g}".replace(".", ",") + (f" {unit}" if unit else "")
    return f"{f(r[0])} à {f(r[1])}"


def label_all(dps: dict) -> None:
    """Libellé lisible de chaque donnée (affichage seulement)."""
    from report.labels import LABELS, PARAM_LABELS
    metric = {"yoc_pct": "yield on cost", "spread_bps": "spread", "margin_pct": "marge sur coût", "irr_equity": "TRI fonds propres",
              "equity_multiple": "multiple", "max_price": "prix maximum"}
    for k, dp in dps.items():
        if dp.get("label"):
            continue
        base, _, alt = k.partition("@")
        parts = k.split(".")
        if k in COMPUTED_LABELS:
            lab = COMPUTED_LABELS[k]
        elif base in LABELS:
            lab = LABELS[base] + (f" ({dp['source_name'].split(' - ')[0]})" if alt else "")
            if alt and "." in alt:
                lab = f"écart entre sources : {LABELS[base]}"
        elif k.startswith("params."):
            lab = PARAM_LABELS.get(parts[1], parts[1].replace("_", " "))
        elif k.startswith("deal."):
            lab = DEAL_LABELS.get(parts[-1], "dossier : " + parts[-1].replace("_", " "))
        elif k.startswith("scenarios.") and len(parts) == 3:
            lab = f"{metric.get(parts[2], parts[2])}, scénario {parts[1]}"
        elif k.startswith("risks.") and len(parts) >= 3:
            lab = f"risque {parts[1]} : " + {"criticality": "criticité", "probability": "probabilité", "impact": "impact",
                                             "delta_margin_pts": "effet sur la marge", "delta_irr_pts": "effet sur le TRI",
                                             "probability_rationale": "justification de la probabilité",
                                             "impact_rationale": "justification de l'impact"}.get(parts[2], "élément de preuve")
        elif k.startswith("sensitivity.breakeven."):
            lab = f"seuil de bascule ({parts[2]}) : " + {"base": "valeur centrale", "breakeven": "marge nulle",
                                                          "irr_target": "TRI à l'objectif"}.get(parts[3], parts[3])
        elif k.startswith("sensitivity.tornado."):
            lab = "sensibilités, une hypothèse à la fois (" + ("marge" if parts[2] == "margin_pct" else "TRI fonds propres") + ")"
        elif k.startswith("sensitivity.heatmaps."):
            lab = "grille croisée (" + ("marge" if parts[-1] == "margin_pct" else "TRI fonds propres") + ")"
        elif k.startswith("engine.inputs."):
            lab = "valeur centrale : " + PARAM_LABELS.get(parts[2], parts[2].replace("_", " "))
        elif k.startswith("sensitivity."):
            lab = "sensibilités : " + ".".join(parts[1:])
        elif k.startswith("recommendation."):
            lab = "chiffre cité dans la recommandation"
        else:
            lab = k
        dp["label"] = lab


NUM_RE = re.compile(r"-?\d{1,3}(?:[\u202f\u00a0 ]\d{3})*(?:,\d+)?(?:\s?(?:M€|€/m²/an|€/m²|%|pb|pt|x|mois|/25))?")


def tokenize_free(text: str, dps: dict) -> str | None:
    """Ligne libre (rédigée par le LLM) : chaque chiffre doit égaler EXACTEMENT l'affichage d'une donnée existante ;
    il est alors remplacé par son jeton. Sinon la ligne est refusée (None) : jamais de chiffre sans source."""
    from decision.phrase import FORMATS
    norm = lambda x: re.sub(r"[\u202f\u00a0]", " ", x).strip()
    out, last = [], 0
    for m in NUM_RE.finditer(text):
        if not re.search(r"\d", m.group(0)):
            continue
        found = None
        for key, dp in dps.items():
            v = dp.get("value")
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            for f, fn in FORMATS.items():
                try:
                    if norm(fn(v)) == norm(m.group(0)):
                        found = "{" + key + "|" + f + "}"
                        break
                except (TypeError, ValueError):
                    continue
            if found:
                break
        if not found:
            return None
        out.append(text[last:m.start()].replace("{", "(").replace("}", ")") + found)
        last = m.end()
    return "".join(out) + text[last:].replace("{", "(").replace("}", ")")


def sweep_effects(fin: dict, defaults, calc) -> None:
    """Point 17 : effet sur la marge quand le loyer retenu, le taux de sortie ou la franchise varient dans leur
    fourchette (bornes des sensibilités et des hypothèses de defaults.yaml). Moteur Python inchangé, simplement rejoué."""
    from finance.engine import run_model
    from finance.export import inputs_from_json
    mi = fin.get("model_inputs")
    if not mi or fin.get("status") != "complete" or not defaults.sensitivity:
        return
    base = inputs_from_json(mi)
    from dataclasses import replace
    var = defaults.sensitivity.variables
    rent = [c.value for c in var["rent"].pessimistic + var["rent"].optimistic if c.field == "relet_rent"]
    ey = [c.value for c in var["exit_yield"].optimistic + var["exit_yield"].pessimistic if c.field == "exit_yield"]
    fa = defaults.financial_assumptions
    inc = fa["incentives_if_missing"].range or (fa["incentives_if_missing"].value, fa["incentives_if_missing"].value)
    firm = base.firm_lease_term_months
    lin = lambda a, b, n=7: [a + (b - a) * i / (n - 1) for i in range(n)]
    specs = {
        "relet_rent": ("€/m²/an", [base.relet_rent * m for m in lin(min(rent), max(rent))], lambda v: replace(base, relet_rent=v),
                       f"loyer retenu de ×{min(rent):g} à ×{max(rent):g} (bornes pessimiste et optimiste des sensibilités)"),
        "exit_yield": ("fraction", [base.exit_yield + d for d in lin(min(ey), max(ey))], lambda v: replace(base, exit_yield=v),
                       f"taux de sortie de {min(ey) * 100:+.2f} à {max(ey) * 100:+.2f} point (bornes des sensibilités)"),
        "rent_free_months": ("mois", [i * firm for i in lin(inc[0], inc[1])], lambda v: replace(base, rent_free_months=v),
                             f"franchise pour des mesures d'accompagnement de {inc[0]:.0%} à {inc[1]:.0%} (fourchette de l'hypothèse)"),
    }
    for name, (unit, xs, make, rng) in specs.items():
        pts = [{"x": x, "margin_pct": run_model(make(x))["profitability"]["margin_on_cost"]} for x in xs]
        calc(f"sensitivity.sweep.{name}", pts, unit, f"marge sur coût quand l'hypothèse varie seule dans sa fourchette : {rng}")


def _add_months(iso: str, months: float) -> str:
    """Date ISO + n mois (la fraction de mois est convertie en jours du mois atteint) : fin de franchise sur la frise."""
    import calendar
    from datetime import date
    y, m, d = (int(x) for x in iso[:10].split("-"))
    whole = int(months)
    yy, mm = divmod(m - 1 + whole, 12)
    yy, mm = y + yy, mm + 1
    days = calendar.monthrange(yy, mm)[1]
    return date(yy, mm, max(1, min(days, d + round((months - whole) * days)))).isoformat()


def tokenize(phrase, dps: dict, prefix: str, adate: str) -> str:
    """Phrase traçable (texte + gabarit + chiffres) -> texte à jetons {clé:champ|format} rendu par fmt().
    Un chiffre n'est rattaché à une donnée existante que si elle vaut EXACTEMENT ce chiffre ; sinon il devient une
    donnée calculée propre à la phrase (jamais une valeur prise ailleurs)."""
    if isinstance(phrase, str):
        phrase = {"text": phrase}
    if "template" not in phrase:
        # sortie d'une version de l'agent sans gabarit : la phrase entière est citée comme une seule valeur, avec sa pastille
        k = f"{prefix}.text"
        dps[k] = {"key": k, "value": phrase["text"], "unit": None, "source_name": ENGINE, "source_url": None, "data_date": adate,
                  "retrieved_at": None, "method": "computed", "status": "ok", "confidence": "high",
                  "notes": "phrase produite par les règles de décision (relancer l'agent pour le détail de chaque chiffre)"}
        return "{" + k + "}"
    tokens = []
    for j, slot in enumerate(phrase.get("slots") or []):
        key, field, v = slot.get("key"), slot.get("field"), slot.get("value")
        dp = dps.get(key) if key else None
        cur = None
        if dp:
            cur = dp["value"].get(field) if (field and isinstance(dp["value"], dict)) else dp["value"]
        same_text = isinstance(cur, str) and isinstance(v, str) and cur == v
        if dp and (same_text or (_num(cur) is not None and _num(v) is not None and abs(cur - v) <= 1e-9 * max(1.0, abs(v)))):
            tokens.append("{" + key + (f":{field}" if field else "") + "|" + slot["format"] + "}")
        else:
            k = f"{prefix}.{j}"
            dps[k] = {"key": k, "value": v, "unit": None, "source_name": ENGINE, "source_url": None, "data_date": adate,
                      "retrieved_at": None, "method": "computed", "status": "ok" if v is not None else "missing", "confidence": "high",
                      "notes": "chiffre calculé par les règles de décision pour cette phrase" + (f" (à partir de {key})" if key else "")}
            tokens.append("{" + k + "|" + slot["format"] + "}")
    return phrase["template"].format(*tokens) if tokens else phrase["template"].replace("{{", "{").replace("}}", "}")


def rationale(decision: str, rec: dict, dps: dict, rules) -> str:
    """Phrase de synthèse du bandeau, construite à partir des critères DÉJÀ calculés par les règles de décision
    (aucune décision recalculée). Les chiffres sont des jetons {clé|format}, rendus par fmt() dans le dashboard."""
    from report.labels import labels
    cr = rec.get("criteria") or {}
    if decision == "NON_DETERMINE":
        missing = (rec.get("facts") or {}).get("required_missing") or (rec.get("facts") or {}).get("missing_inputs") or []
        return f"Décision impossible : données exigées absentes ({labels(missing)})." if missing else "Décision impossible : modèle incomplet."
    parts = []
    price = cr.get("price") or {}
    gates = cr.get("gates") or {}
    if decision == "NO_GO":
        if price.get("negotiable") is False:
            parts.append("le prix maximum compatible avec les objectifs ({financials.max_price.central|meur}) est inférieur de "
                         "{financials.max_price_gap_pct|pct_abs} au prix demandé ({deal.asking_price_eur|meur}), au-delà de la décote "
                         "jugée négociable ({rules.max_negotiable_discount|pct})")
        if cr.get("central_profitable") is False:
            parts.append("la marge du scénario central est négative ({financials.margin_pct|pct})")
        if gates.get("risks", True) and (cr.get("B") or {}).get("manageable") is False:
            parts.append("{risks.critical_count|int} risques critiques, pour un maximum admis de {rules.max_critical_risks|int}")
    elif decision == "GO_SOUS_CONDITIONS":
        failed = [k for k, v in ((cr.get("A") or {}).get("checks") or {}).items() if not v.get("ok")]
        names = {"levered_irr": "TRI fonds propres {financials.irr_equity|pct} pour un objectif de {targets.min_levered_irr_pct|pct}",
                 "margin_on_cost": "marge {financials.margin_pct|pct} pour un objectif de {targets.min_margin_on_cost_pct|pct}",
                 "spread_bps": "spread {financials.spread_bps|bps} pour un minimum de {targets.min_spread_vs_exit_bps|bps}",
                 "equity_multiple": "multiple {financials.equity_multiple|mult} pour un objectif de {targets.min_equity_multiple|mult}"}
        if failed:
            parts.append("objectifs non atteints au prix demandé dans le scénario central : " + ", ".join(names[k] for k in failed if k in names))
        if gates.get("risks", True) and (cr.get("B") or {}).get("critical"):
            parts.append("{risks.critical_count|int} risque(s) critique(s) à réduire")
        if gates.get("robustness", True) and (cr.get("C") or {}).get("satisfactory") is False and "scenarios.defavorable.margin_pct" in dps:
            parts.append("marge négative en scénario défavorable ({scenarios.defavorable.margin_pct|pct})")
    else:
        parts.append("objectifs du fonds atteints dans le scénario central au prix demandé"
                     + (", aucun risque critique" if gates.get("risks", True) else "")
                     + (", marge positive en scénario défavorable" if gates.get("robustness", True) else ""))
    m = cr.get("M") or {}
    if decision != "NO_GO" and m.get("available") and {"financials.price_per_sqm_asked", "financials.price_vs_median_pct"} <= dps.keys():
        verdict = ("à négocier" if m.get("constraint") else "cohérent avec le marché")
        parts.append("prix demandé de {financials.price_per_sqm_asked|sqm} face à la médiane des transactions "
                     "({market.dvf_price_per_sqm:median|sqm}, écart {financials.price_vs_median_pct|pct_signed}) : " + verdict)
    if not parts:
        return "Voir les critères détaillés de la recommandation."
    text = " ; ".join(parts)
    return text[0].upper() + text[1:] + "."


# ------------------------------------------------------------------ rendu

def render(data: dict) -> str:
    """Valide DATA (refus si invalide) puis produit le HTML autonome."""
    validate_data(data)
    try:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
    except ImportError as exc:  # dépendance du générateur seulement : le fichier produit n'en a pas besoin
        raise RuntimeError("jinja2 manquant : pip install jinja2") from exc
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["j2", "html"]), undefined=StrictUndefined)
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    from markupsafe import Markup
    engine = ENGINE_PATH.read_text(encoding="utf-8")
    if "</" in engine:
        raise ValueError("le moteur JS ne doit pas contenir « </ » (fermeture de balise)")
    return env.get_template(TEMPLATE_NAME).render(title=f"{data['meta']['asset_name']} – aide à la décision",
                                                  lang=data["meta"]["language"], data_json=Markup(payload), engine_js=Markup(engine))


def write_dashboard(out_dir: Path, config, defaults, generated_at: str | None = None) -> Path:
    analysis = json.loads((out_dir / "analysis_results.json").read_text(encoding="utf-8"))
    registry = json.loads((out_dir / "data_registry.json").read_text(encoding="utf-8"))
    path = out_dir / "dashboard.html"
    path.write_text(render(build_data(analysis, registry, config, defaults, generated_at)), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    from config.schema import PROJECT_ROOT, load_config, load_defaults
    ap = argparse.ArgumentParser(description="Génère le dashboard HTML autonome")
    ap.add_argument("--data", help="objet DATA déjà au format du contrat (ex. fixtures/data_sample.json)")
    ap.add_argument("--config", default=str(PROJECT_ROOT / "asset_config.json"))
    ap.add_argument("--out", default=str(PROJECT_ROOT / "output"))
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.data:
        path = out / "dashboard.html"
        path.write_text(render(json.loads(Path(args.data).read_text(encoding="utf-8"))), encoding="utf-8")
    else:
        path = write_dashboard(out, load_config(args.config), load_defaults())
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
