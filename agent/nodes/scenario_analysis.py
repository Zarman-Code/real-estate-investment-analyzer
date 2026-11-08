"""Nœud scenario_analysis : variations simultanées (plan, section 7.2), sans LLM.

Scénarios définis dans defaults.yaml (favorable, central, défavorable, stress) à partir des variables
de sensibilité. Pour chacun : YoC, spread, marge, TRI projet et fonds propres, multiple, prix maximum,
et respect des objectifs du fonds. Si le modèle central est incomplet, aucun chiffre n'est produit.
"""
from __future__ import annotations

from agent.state import AgentState
from report.figures import scenario_figures
from report.node_report import make_report
from finance.inputs import resolve_inputs
from finance.sensitivity import scenarios


def make_scenario_node(llm=None):
    def node(state: AgentState) -> dict:
        out = scenario_analysis_core(state)
        figs, summary, answers = scenario_figures(out["scenarios"])
        out["scenarios"]["report"] = make_report("scenario_analysis", figs, summary, answers, state["defaults"], llm)
        d = out["scenarios"]["report"]["drafting"]
        out["logs"].append(f"  rapport scenario_analysis : {d['mode']} ({d['status']})")
        return out
    node.__name__ = "scenario_analysis"
    return node


def scenario_analysis_core(state: AgentState) -> dict:
    fin = state.get("financials") or {}
    if fin.get("status") != "complete":
        return {"scenarios": {"status": "incomplete", "missing_inputs": fin.get("missing_inputs", [])},
                "logs": ["scenario_analysis : modèle central incomplet, aucun scénario calculé"]}
    cfg, defaults = state["config"], state["defaults"]
    inp = resolve_inputs(cfg, state["registry"], defaults).inputs
    res = scenarios(inp, defaults.scenarios, defaults.sensitivity, fin["targets"])
    logs = ["scenario_analysis : " + " | ".join(
        f"{v['label']} TRI FP {_pct(v['metrics']['levered_irr'])}, marge {_pct(v['metrics']['margin_on_cost'])}, "
        f"prix max {_eur(v['max_price'])}" for v in res.values())]
    # cohérence : le scénario central doit reproduire exactement financial_analysis
    central_irr = res["central"]["metrics"]["levered_irr"]
    if central_irr != fin["results"]["returns"]["levered_irr"]:
        logs.append("  ALERTE : le scénario central diffère du résultat de financial_analysis")
    return {"scenarios": {"status": "complete", "scenarios": res, "targets": fin["targets"]}, "logs": logs}


def _pct(v):
    return "n.d." if v is None else f"{v:.1%}"


def _eur(v):
    return "n.d." if v is None else f"{v / 1e6:.2f} M€"


scenario_analysis = make_scenario_node(None)
