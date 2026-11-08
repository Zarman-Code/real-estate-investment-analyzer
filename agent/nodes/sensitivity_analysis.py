"""Nœud sensitivity_analysis : effet de chaque paramètre pris isolément (plan, section 7.1), sans LLM.

Reçoit financial_analysis et risk_analysis (jointure du graphe). Produit : tornado (classement des
variables déterminantes), seuils de bascule (marge nulle, TRI fonds propres = objectif du fonds), heatmaps.
Si le modèle central est incomplet, aucun chiffre n'est produit.
"""
from __future__ import annotations

from agent.state import AgentState
from report.figures import sensitivity_figures
from report.node_report import make_report
from finance.inputs import resolve_inputs
from finance.sensitivity import all_breakevens, heatmap, tornado


def make_sensitivity_node(llm=None):
    def node(state: AgentState) -> dict:
        out = sensitivity_analysis_core(state)
        figs, summary, answers = sensitivity_figures(out["sensitivity"])
        out["sensitivity"]["report"] = make_report("sensitivity_analysis", figs, summary, answers, state["defaults"], llm)
        d = out["sensitivity"]["report"]["drafting"]
        out["logs"].append(f"  rapport sensitivity_analysis : {d['mode']} ({d['status']})")
        return out
    node.__name__ = "sensitivity_analysis"
    return node


def sensitivity_analysis_core(state: AgentState) -> dict:
    fin = state.get("financials") or {}
    if fin.get("status") != "complete":
        return {"sensitivity": {"status": "incomplete", "missing_inputs": fin.get("missing_inputs", [])},
                "logs": ["sensitivity_analysis : modèle central incomplet, aucune sensibilité calculée"]}
    cfg, defaults = state["config"], state["defaults"]
    inp = resolve_inputs(cfg, state["registry"], defaults).inputs
    sc = defaults.sensitivity
    target = fin["targets"]["levered_irr"]
    tor = tornado(inp, sc)
    be = all_breakevens(inp, sc, target)
    maps = {name: heatmap(inp, spec) for name, spec in sc.heatmaps.items()}
    top = ", ".join(f"{r['label']} ({r['irr_range_pts']:.1f} pt)" for r in tor["rows"][:3])
    logs = [f"sensitivity_analysis : {len(tor['rows'])} variables, plus déterminantes : {top}"]
    for name, b in be.items():
        logs.append(f"  seuil {name} : marge nulle à {b.get('margin_zero_value', b['margin_zero_at'])}, "
                    f"TRI = objectif à {b.get('irr_target_value', b['irr_target_at'])}")
    return {"sensitivity": {"status": "complete", "tornado": tor, "breakeven": be, "heatmaps": maps},
            "logs": logs}


sensitivity_analysis = make_sensitivity_node(None)
