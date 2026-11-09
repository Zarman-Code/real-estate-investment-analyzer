"""Nœud financial_analysis : rentabilité de référence (scénario central), sans LLM.

1. résout les entrées avec leur provenance (finance/inputs.py) ;
2. si une donnée P1 manque : statut « incomplete », liste des manques, AUCUN chiffre produit ;
3. sinon : moteur (finance/engine.py), prix maximum (finance/max_price.py), comparaison du prix (module 1).
Le nœud n'écrit pas dans le registre (risk_analysis tourne en parallèle) : la provenance des entrées
et les hypothèses utilisées sont dans `financials["inputs"]`.
"""
from __future__ import annotations

from dataclasses import asdict

from agent.state import AgentState
from finance.acquisition import compare_price
from finance.engine import run_model
from finance.export import inputs_to_json
from finance.inputs import resolve_inputs
from finance.max_price import max_price
from report.figures import financial_figures
from report.node_report import make_report


def _provenance(reg, defaults, keys=None) -> dict:
    """Pour chaque donnée de marché du modèle : sources trouvées (nom, date, confiance, statut) et nombre demandé.
    Sert au rapporteur final à dire quelles hypothèses reposent sur une source unique ou fragile."""
    specs = {n.key: n for n in defaults.data_needs}
    keys = keys or [k for k in specs if k.startswith(("market.", "rates.", "deal."))]
    out = {}
    for k in keys:
        pts = reg.get_all(k)
        if not pts:
            continue
        out[k] = {"sources": [{"source": p.source_name, "date": p.data_date, "confidence": p.confidence, "status": p.status,
                               "method": p.method} for p in pts if p.status not in ("missing", "rejected")],
                  "min_sources": specs[k].min_sources}
    return out


def _context(state: AgentState) -> dict:
    """Contexte de marché et qualité des données, transmis aux nœuds suivants via ce nœud uniquement."""
    reg, rules = state["registry"], state["defaults"].decision_rules
    market = {}
    for key, field, name in (("market.immediate_supply", "yoy", "supply_yoy"),
                             ("market.investment_price_per_sqm", "yoy", "investment_price_yoy"),
                             ("market.investment_price_per_sqm", "last", "investment_price_per_sqm"),
                             ("market.average_rent", "last", "average_rent")):
        found = reg.found(key)
        if found and isinstance(found[0].value, dict) and found[0].value.get(field) is not None:
            market[name] = {"value": found[0].value[field], "source": found[0].source_name, "date": found[0].data_date}
    required = {k: bool(reg.found(k)) for k in (rules.required_data if rules else [])}
    t = state["targets"].targets
    return {"market_context": market, "required_data": required, "input_provenance": _provenance(reg, state["defaults"]),
            "data_quality": {"missing": [d.key for d in reg.missing()], "rejected": [d.key for d in reg.rejected()],
                             "assumptions": [d.key for d in reg.assumptions()]},
            "targets_full": {"levered_irr": t["levered_irr_min"].value, "margin_on_cost": t["margin_on_cost_min"].value,
                             "spread_bps": t["yoc_spread_min_bps"].value, "equity_multiple": t["equity_multiple_min"].value,
                             "status": {k: v.status for k, v in t.items()}},
            "asking_price": state["config"].operation.asking_price_eur, "asset": state["config"].asset.name}


def make_financial_node(llm=None):
    def node(state: AgentState) -> dict:
        out = financial_analysis_core(state)
        fin = out["financials"]
        figs, summary, answers = financial_figures(fin, state["registry"], state["config"], state["defaults"])
        fin["report"] = make_report("financial_analysis", figs, summary, answers, state["defaults"], llm)
        out["logs"].append(f"  rapport financier : {fin['report']['drafting']['mode']} ({fin['report']['drafting']['status']})")
        return out
    node.__name__ = "financial_analysis"
    return node


def financial_analysis_core(state: AgentState) -> dict:
    cfg, defaults, registry = state["config"], state["defaults"], state["registry"]
    ri = resolve_inputs(cfg, registry, defaults)
    params = [asdict(p) for p in ri.params]
    ctx = _context(state)
    if not ri.complete:
        return {"financials": {"status": "incomplete", "missing_inputs": ri.missing, "inputs": params} | ctx,
                "logs": [f"financial_analysis : INCOMPLET, données manquantes {ri.missing} : aucun chiffre produit"]}

    results = run_model(ri.inputs)
    t = state["targets"].targets
    targets = {"levered_irr": t["levered_irr_min"].value, "margin_on_cost": t["margin_on_cost_min"].value}
    mp = max_price(ri.inputs, targets)
    comparison = compare_price(ri.inputs.price, ri.inputs.surface_before, ri.inputs.surface_after, registry, defaults,
                               cfg.asset.condition, cfg.asset.dpe_current)
    checks = results["checks"]
    pr, rt = results["profitability"], results["returns"]
    logs = [f"financial_analysis : YoC {pr['yield_on_cost']:.2%}, spread {pr['spread_bps']:.0f} bps, "
            f"marge {pr['margin_on_cost']:.1%}, TRI projet {_pct(rt['unlevered_irr'])}, TRI FP {_pct(rt['levered_irr'])}, "
            f"multiple {_x(rt['equity_multiple'])}",
            f"  prix maximum {_eur(mp['max_price'])} (objectif contraignant : {mp['binding_target']}) "
            f"pour un prix demandé de {_eur(ri.inputs.price)}",
            f"  {len(ri.assumptions())} hypothèses de modélisation utilisées ; contrôles : {checks}"]
    if not (checks["profit_identity"] and checks["debt_balance_identity"]):
        logs.append("  ALERTE : un contrôle d'identité comptable a échoué")
    return {"financials": {"status": "complete", "missing_inputs": [], "inputs": params, "targets": targets,
                           "model_inputs": inputs_to_json(ri.inputs),   # entrées du moteur JS du dashboard
                           "results": results, "max_price": mp, "price_comparison": comparison} | ctx,
            "logs": logs}


financial_analysis = make_financial_node(None)


def _pct(v):
    return "n.d." if v is None else f"{v:.1%}"


def _x(v):
    return "n.d." if v is None else f"{v:.2f}x"


def _eur(v):
    return "n.d." if v is None else f"{v:,.0f} €".replace(",", " ")
