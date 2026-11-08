"""Tests des sensibilités et scénarios (phase 6) : variations, tornado, seuils de bascule exacts,
heatmaps, ordre des scénarios, prix maximum par scénario, nœuds (complet / incomplet), graphe complet."""
from __future__ import annotations

import json
from datetime import date

import pytest

from agent.graph import build_graph, recursion_limit
from agent.nodes.financial_analysis import financial_analysis
from agent.nodes.scenario_analysis import scenario_analysis
from agent.nodes.sensitivity_analysis import sensitivity_analysis
from agent.state import build_initial_state
from config.schema import (PROJECT_ROOT, ParamChange, ShockChange, load_config, load_defaults,
                           load_investor_targets, load_sources_catalog)
from finance.engine import run_model
from finance.inputs import resolve_inputs
from finance.sensitivity import all_breakevens, breakeven, heatmap, metrics, scenario_inputs, scenarios, tornado
from finance.variations import apply_changes, month_end_after
from tools.connectors import ConnectorRegistry
from tools.registry import DataRegistry, register_config_inputs

D = load_defaults()
SC = D.sensitivity
TARGETS = {"levered_irr": 0.15, "margin_on_cost": 0.15}
ILLUSTRATIVE = {"relet_rent_eur_sqm_year": 940, "exit_yield_pct": 0.045, "euribor_3m_pct": 0.0263,
                "acquisition_fees_pct": 0.075}   # valeurs de test, non sourcées


def cfg_with(tmp_path, **ov):
    data = json.loads((PROJECT_ROOT / "asset_config.json").read_text(encoding="utf-8"))
    data["overrides"] = ov
    p = tmp_path / "c.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return load_config(p)


@pytest.fixture
def inp(tmp_path):
    cfg = cfg_with(tmp_path, **ILLUSTRATIVE)
    reg = DataRegistry()
    register_config_inputs(cfg, reg)
    return resolve_inputs(cfg, reg, D).inputs


# ------------------------------------------------------------------ variations

def test_apply_changes_ops(inp):
    out = apply_changes(inp, [ShockChange(field="relet_rent", op="mult", value=1.1),
                              ShockChange(field="letting_void_months", op="add", value=6),
                              ShockChange(field="stabilised_occupancy", op="set", value=0.85),
                              ShockChange(field="exit_date", op="add_months", value=6)])
    assert out.relet_rent == pytest.approx(inp.relet_rent * 1.1)
    assert out.letting_void_months == inp.letting_void_months + 6 and isinstance(out.letting_void_months, int)
    assert out.stabilised_occupancy == 0.85 and out.exit_date == date(2030, 6, 30)
    assert apply_changes(inp, [ParamChange(field="exit_yield", op="add")], value=0.01).exit_yield == pytest.approx(0.055)
    with pytest.raises(ValueError):
        apply_changes(inp, [ParamChange(field="exit_yield", op="add")])          # valeur manquante


def test_month_end_after():
    assert month_end_after(date(2029, 12, 31), 6) == date(2030, 6, 30)
    assert month_end_after(date(2029, 12, 31), 2) == date(2030, 2, 28)


# ------------------------------------------------------------------ tornado

def test_tornado_directions_and_ranking(inp):
    t = tornado(inp, SC)
    assert t["central"] == metrics(run_model(inp))
    for r in t["rows"]:
        assert r["delta_irr_pts"]["optimistic"] >= 0 >= r["delta_irr_pts"]["pessimistic"], r["variable"]
    ranges = [r["irr_range_pts"] for r in t["rows"]]
    assert ranges == sorted(ranges, reverse=True) and [r["rank"] for r in t["rows"]] == list(range(1, len(ranges) + 1))
    assert {r["variable"] for r in t["rows"]} == set(SC.variables)


def test_tornado_rows_match_direct_runs(inp):
    """Recalcul indépendant d'une ligne : loyer -10 % rejoué directement."""
    t = {r["variable"]: r for r in tornado(inp, SC)["rows"]}
    direct = run_model(apply_changes(inp, SC.variables["rent"].pessimistic))["returns"]["levered_irr"]
    assert t["rent"]["pessimistic"]["levered_irr"] == pytest.approx(direct)


# ------------------------------------------------------------------ seuils de bascule

def test_breakeven_values_are_exact(inp):
    for name, b in all_breakevens(inp, SC, 0.15).items():
        spec = SC.breakeven[name]
        if b["margin_zero_at"] is not None:
            m = metrics(run_model(apply_changes(inp, spec.changes, value=b["margin_zero_at"])))
            if spec.integer:     # premier mois où la marge change de signe
                before = metrics(run_model(apply_changes(inp, spec.changes, value=b["margin_zero_at"] - 1)))
                assert m["margin_on_cost"] <= 0 < before["margin_on_cost"], name
            else:
                assert m["margin_on_cost"] == pytest.approx(0, abs=1e-6), name
        if b["irr_target_at"] is not None and not spec.integer:
            m = metrics(run_model(apply_changes(inp, spec.changes, value=b["irr_target_at"])))
            assert m["levered_irr"] == pytest.approx(0.15, abs=1e-6), name


def test_breakeven_rent_value_in_euros(inp):
    b = breakeven(inp, SC.breakeven["rent"], 0.15)
    assert b["margin_zero_value"] == pytest.approx(inp.relet_rent * b["margin_zero_at"])
    assert b["margin_zero_value"] < inp.relet_rent              # marge positive au central : il faut baisser le loyer


def test_breakeven_not_reached_is_none(inp):
    b = breakeven(inp, SC.breakeven["works"], 0.15)
    assert b["central_below_target"] is (run_model(inp)["returns"]["levered_irr"] < 0.15)
    assert b["irr_target_at"] is None                            # TRI déjà sous l'objectif et toujours décroissant


# ------------------------------------------------------------------ heatmaps

def test_heatmap_shape_centre_and_monotonicity(inp):
    spec = SC.heatmaps["exit_yield_x_rent"]
    h = heatmap(inp, spec)
    assert len(h["levered_irr"]) == len(spec.y.values) and len(h["levered_irr"][0]) == len(spec.x.values)
    cy, cx = spec.y.values.index(0.0), spec.x.values.index(1.0)
    assert h["levered_irr"][cy][cx] == pytest.approx(run_model(inp)["returns"]["levered_irr"])
    for row in h["margin_on_cost"]:                              # loyer croissant -> marge croissante
        assert row == sorted(row)
    for j in range(len(spec.x.values)):                          # taux de sortie croissant -> marge STRICTEMENT décroissante
        col = [h["margin_on_cost"][i][j] for i in range(len(spec.y.values))]
        assert all(a > b for a, b in zip(col, col[1:]))
    # recalcul direct d'une case en coin (les deux axes appliqués)
    corner = apply_changes(apply_changes(inp, spec.y.changes, value=spec.y.values[-1]), spec.x.changes, value=spec.x.values[0])
    assert h["margin_on_cost"][-1][0] == pytest.approx(run_model(corner)["profitability"]["margin_on_cost"])


# ------------------------------------------------------------------ scénarios

def test_scenarios_order_and_central_identity(inp):
    s = scenarios(inp, D.scenarios, SC, TARGETS)
    irr = {k: (v["metrics"]["levered_irr"] if v["metrics"]["levered_irr"] is not None else -1) for k, v in s.items()}
    assert irr["favorable"] > irr["central"] > irr["defavorable"] > irr["stress"]
    mp = {k: v["max_price"] for k, v in s.items()}
    assert mp["favorable"] > mp["central"] > mp["defavorable"] > mp["stress"]
    assert s["central"]["metrics"] == metrics(run_model(inp))
    assert all(v["checks"]["profit_identity"] for v in s.values())


def test_scenario_delays_shift_exit(inp):
    d = scenario_inputs(inp, D.scenarios["defavorable"], SC)
    assert d.exit_date == date(2030, 12, 31)                      # +6 travaux +6 relocation
    assert d.works_months == inp.works_months + 6 and d.capex_per_sqm == pytest.approx(inp.capex_per_sqm * 1.15)


def test_meets_targets_flag_is_consistent(inp):
    for v in scenarios(inp, D.scenarios, SC, TARGETS).values():
        m = v["metrics"]
        ok = (m["levered_irr"] or -1) >= 0.15 and m["margin_on_cost"] >= 0.15
        assert v["meets_targets"] is ok


# ------------------------------------------------------------------ nœuds

def state_for(cfg):
    st = build_initial_state(cfg, D, load_investor_targets(), load_sources_catalog(), analysis_date=date(2026, 11, 8))
    st.update(financial_analysis(st))
    return st


def test_nodes_incomplete_produce_no_numbers(tmp_path):
    st = state_for(cfg_with(tmp_path))
    s, c = sensitivity_analysis(st)["sensitivity"], scenario_analysis(st)["scenarios"]
    assert s["status"] == c["status"] == "incomplete"
    assert "tornado" not in s and "scenarios" not in c and "market.average_rent" in s["missing_inputs"]


def test_nodes_complete_and_serialisable(tmp_path):
    st = state_for(cfg_with(tmp_path, **ILLUSTRATIVE))
    s = sensitivity_analysis(st)
    c = scenario_analysis(st)
    assert s["sensitivity"]["status"] == c["scenarios"]["status"] == "complete"
    assert set(s["sensitivity"]["heatmaps"]) == set(SC.heatmaps)
    assert c["scenarios"]["scenarios"]["central"]["metrics"]["levered_irr"] == \
        st["financials"]["results"]["returns"]["levered_irr"]
    assert not any("ALERTE" in line for line in c["logs"])
    json.dumps(s, default=str)
    json.dumps(c, default=str)


def test_full_graph_runs_all_eight_nodes(tmp_path):
    """Graphe complet, sans connecteur mais avec les overrides : les 8 nœuds produisent leur sortie."""
    cfg = cfg_with(tmp_path, **ILLUSTRATIVE)
    st = build_initial_state(cfg, D, load_investor_targets(), load_sources_catalog(), analysis_date=date(2026, 11, 8))
    final = build_graph(ConnectorRegistry()).invoke(st, config={"recursion_limit": recursion_limit(2)})
    assert final["financials"]["status"] == "complete"
    assert final["sensitivity"]["status"] == "complete" and final["scenarios"]["status"] == "complete"
    assert final["risks"]["financial_inputs_complete"]
