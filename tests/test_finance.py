"""Tests du moteur financier (plan, section 5.7) : cas d'école calculés à la main, TRI de référence,
propriétés de monotonie, conservation de la dette, test « zéro dette », prix maximum, provenance des entrées."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

from config.schema import PROJECT_ROOT, load_config, load_defaults
from finance import profitability as P
from finance.cashflow import annualise, equity_multiple, irr, npv
from finance.engine import run_model
from finance.inputs import DealInputs, Lease, quarter_end, resolve_inputs
from finance.max_price import max_price, solve_max_price
from finance.timeline import build_timeline, lease_month_fraction
from tools.registry import DataPoint, DataRegistry, register_config_inputs


def simple(**kw) -> DealInputs:
    """Cas d'école sans frictions : chaque résultat se recalcule à la main."""
    base = dict(price=10_000_000, acq_fees_pct=0.07, surface_before=1000, surface_after=1000,
                capex_per_sqm=1000, fees_contingency_pct=0.10,
                acquisition_date=date(2030, 1, 1), works_start=date(2030, 1, 1), works_months=12,
                exit_date=date(2031, 12, 31), current_leases=(), relet_rent=500, letting_void_months=0,
                rent_free_months=0, stabilised_occupancy=1.0, non_recoverable_pct=0.0, vacant_holding_cost=0.0,
                marketing_fees_pct=0.0, exit_yield=0.05, sale_costs_pct=0.0, ltv=0.0, ltv_basis="total_cost",
                euribor=0.02, margin_bps=200, euribor_floor=0.0, arrangement_fee_pct=0.0)
    base.update(kw)
    return DealInputs(**base)


# ------------------------------------------------------------------ TRI et outils

def test_irr_reference_cases():
    assert irr([-100, 110]) == pytest.approx(0.10, abs=1e-9)
    assert irr([-100, 0, 121]) == pytest.approx(0.10, abs=1e-9)
    assert irr([-100, 50, 50]) == pytest.approx(0.0, abs=1e-9)
    assert irr([-100, -10]) is None and irr([100, 10]) is None


def test_irr_matches_numpy_financial():
    npf = pytest.importorskip("numpy_financial")
    flows = [-10.7e6, -275e3, -275e3, -275e3, -275e3, 125e3, 125e3, 125e3, 10.125e6]
    assert irr(flows) == pytest.approx(float(npf.irr(flows)), abs=1e-9)


def test_npv_at_irr_is_zero():
    flows = [-1000, 200, 300, 400, 500]
    assert npv(irr(flows), flows) == pytest.approx(0, abs=1e-6)


def test_annualise_and_multiple():
    assert annualise(0.025) == pytest.approx(1.025 ** 4 - 1)
    assert annualise(None) is None
    assert equity_multiple([-100, -50, 60, 240]) == pytest.approx(2.0)


# ------------------------------------------------------------------ formules du module 4

def test_module4_formulas():
    assert P.gross_exit_value(500_000, 0.05) == pytest.approx(10_000_000)
    assert P.yield_on_cost(600_000, 10_000_000) == pytest.approx(0.06)
    assert P.margin(11_000_000, 10_000_000) == (pytest.approx(1_000_000), pytest.approx(0.10))
    assert P.spread_bps(0.06, 0.05) == pytest.approx(100)
    with pytest.raises(ValueError):
        P.gross_exit_value(1, 0)


# ------------------------------------------------------------------ cas d'école complet

def test_hand_calculated_case():
    r = run_model(simple())
    # coût total = 10 M + 7 % de frais + 1 000 €/m² x 1 000 m² + 10 % d'aléas = 11,8 M€ (sans dette)
    assert r["cost"]["total_cost"] == pytest.approx(11_800_000)
    assert r["revenue"]["noi_stabilised"] == pytest.approx(500_000)          # 500 €/m² x 1 000 m²
    assert r["exit"]["gross_value"] == pytest.approx(10_000_000)             # 500 k€ / 5 %
    assert r["profitability"]["margin"] == pytest.approx(-1_800_000)
    assert r["profitability"]["yield_on_cost"] == pytest.approx(500_000 / 11_800_000)
    # flux : T0 -10,7 M ; 4 trimestres de travaux de 275 k ; 4 trimestres de loyers de 125 k ; + 10 M
    expected = [-10_700_000] + [-275_000] * 4 + [125_000] * 3 + [10_125_000]
    assert [p["unlevered"] for p in r["cashflows"]] == pytest.approx(expected)
    assert r["returns"]["unlevered_irr"] == pytest.approx(annualise(irr(expected)))


def test_zero_debt_levered_equals_unlevered():
    r = run_model(simple(ltv=0.0))
    assert r["returns"]["levered_irr"] == pytest.approx(r["returns"]["unlevered_irr"], abs=1e-12)
    assert r["cost"]["interest"] == 0


def test_debt_conservation_and_profit_identity():
    r = run_model(simple(ltv=0.6))
    flows = r["cashflows"]
    draws, interest = sum(p["debt_draw"] for p in flows), sum(p["interest"] for p in flows)
    assert r["returns"]["debt_repaid_at_exit"] == pytest.approx(draws + interest)     # tout est remboursé
    assert draws == pytest.approx(0.6 * sum(-p["unlevered"] for p in flows[:-1] if p["unlevered"] < 0))
    rate = 0.02 + 0.02
    recomputed = sum(flows[k - 1]["debt_balance"] * rate / 4 for k in range(1, len(flows)))
    assert interest == pytest.approx(recomputed)                                       # intérêts recalculés
    assert all(r["checks"][k] for k in ("profit_identity", "debt_balance_identity"))


def test_ltv_on_price_basis():
    r = run_model(simple(ltv=0.6, ltv_basis="price"))
    draws = [p["debt_draw"] for p in r["cashflows"]]
    assert draws[0] == pytest.approx(6_000_000) and sum(draws[1:]) == 0


def test_euribor_floor():
    a = run_model(simple(ltv=0.6, euribor=-0.005))
    b = run_model(simple(ltv=0.6, euribor=0.0))
    assert a["cost"]["interest"] == pytest.approx(b["cost"]["interest"])


def test_arrangement_fee_paid_by_equity_at_t0():
    r = run_model(simple(ltv=0.6, arrangement_fee_pct=0.01))
    assert r["cost"]["arrangement_fee"] == pytest.approx(0.01 * sum(p["debt_draw"] for p in r["cashflows"]))
    assert r["checks"]["profit_identity"]


# ------------------------------------------------------------------ monotonie (plan 5.7)

def metric(inp, path):
    r = run_model(inp)
    a, b = path.split(".")
    return r[a][b]


@pytest.mark.parametrize("field,lo,hi,path,direction", [
    ("relet_rent", 500, 600, "profitability.yield_on_cost", +1),
    ("relet_rent", 500, 600, "profitability.margin", +1),
    ("relet_rent", 500, 600, "returns.levered_irr", +1),
    ("exit_yield", 0.05, 0.06, "exit.gross_value", -1),
    ("exit_yield", 0.05, 0.06, "returns.unlevered_irr", -1),
    ("capex_per_sqm", 1000, 1200, "returns.unlevered_irr", -1),
    ("capex_per_sqm", 1000, 1200, "cost.total_cost", +1),
    ("stabilised_occupancy", 0.85, 1.0, "revenue.noi_stabilised", +1),
    ("letting_void_months", 0, 6, "returns.unlevered_irr", -1),
    ("rent_free_months", 0, 6, "returns.unlevered_irr", -1),
    ("price", 9_000_000, 10_000_000, "returns.levered_irr", -1),
])
def test_monotonicity(field, lo, hi, path, direction):
    kw = {"ltv": 0.6}
    a, b = metric(simple(**kw, **{field: lo}), path), metric(simple(**kw, **{field: hi}), path)
    assert (b - a) * direction > 0, f"{field} {lo}->{hi} : {path} {a} -> {b}"


def test_euribor_moves_levered_not_unlevered():
    a, b = run_model(simple(ltv=0.6, euribor=0.02)), run_model(simple(ltv=0.6, euribor=0.03))
    assert b["returns"]["levered_irr"] < a["returns"]["levered_irr"]
    assert b["returns"]["unlevered_irr"] == pytest.approx(a["returns"]["unlevered_irr"])


# ------------------------------------------------------------------ calendrier, baux, sortie

def test_timeline_and_partial_month_lease():
    tl = build_timeline(simple(works_start=date(2030, 7, 1), works_months=6, exit_date=date(2031, 6, 30)))
    assert (tl.works_start, tl.works_end, tl.n_months, tl.n_quarters) == (6, 12, 18, 6)
    assert lease_month_fraction(date(2030, 1, 1), 5, date(2030, 6, 15)) == pytest.approx(15 / 30)
    assert lease_month_fraction(date(2030, 1, 1), 5, date(2030, 6, 30)) == 1.0
    assert lease_month_fraction(date(2030, 1, 1), 6, date(2030, 6, 30)) == 0.0


def test_current_lease_income_stops_at_lease_end():
    inp = simple(works_start=date(2030, 7, 1), works_months=6, current_leases=(Lease(400, 300, date(2030, 3, 31)),))
    assert run_model(inp)["revenue"]["current_leases_income"] == pytest.approx(400 * 300 / 4)    # 3 mois


def test_exit_before_letting_deducts_void_and_rent_free():
    inp = simple(letting_void_months=30, rent_free_months=6, marketing_fees_pct=0.1, vacant_holding_cost=60)
    r = run_model(inp)
    d = r["exit"]["deductions"]
    assert r["timeline"]["let_at_exit"] is False
    assert d["remaining_void_rent"] == pytest.approx(18 * 500_000 / 12)       # livraison 2031-01 + 30 mois
    assert d["remaining_rent_free"] == pytest.approx(6 * 500_000 / 12)
    assert d["unspent_marketing"] == pytest.approx(50_000)
    assert d["remaining_void_holding"] == pytest.approx(18 * 1000 * 60 / 12)


def test_rent_free_remaining_at_exit():
    r = run_model(simple(rent_free_months=18))        # bail au 2031-01, franchise jusqu'au 2032-07, sortie fin 2031
    assert r["exit"]["deductions"]["remaining_rent_free"] == pytest.approx(6 * 500_000 / 12)
    assert r["revenue"]["new_lease_income_before_exit"] == 0


def test_holding_costs_follow_vacancy():
    inp = simple(vacant_holding_cost=120, stabilised_occupancy=0.9)
    # travaux 12 mois immeuble vide (1 000 m²), puis 10 % vacants pendant 12 mois
    assert run_model(inp)["cost"]["holding_costs"] == pytest.approx(1000 * 120 + 100 * 120)


# ------------------------------------------------------------------ prix maximum

def test_max_price_hits_targets():
    inp = simple(ltv=0.6, relet_rent=700)
    res = max_price(inp, {"levered_irr": 0.15, "margin_on_cost": 0.15})
    for m, t in (("levered_irr", 0.15), ("margin_on_cost", 0.15)):
        p = res["by_target"][m]["price"]
        r = run_model(replace(inp, price=p))
        got = r["returns"]["levered_irr"] if m == "levered_irr" else r["profitability"]["margin_on_cost"]
        assert got == pytest.approx(t, abs=2e-4)
    assert res["max_price"] == min(r["price"] for r in res["by_target"].values())


def test_max_price_unattainable():
    res = solve_max_price(simple(relet_rent=10), "margin_on_cost", 0.15)
    assert res["price"] is None and "inatteignable" in res["status"]


# ------------------------------------------------------------------ résolution des entrées (provenance)

def cfg_with(tmp_path, **ov):
    data = json.loads((PROJECT_ROOT / "asset_config.json").read_text(encoding="utf-8"))
    data["overrides"] = ov
    p = tmp_path / "c.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return load_config(p)


def reg_for(cfg, **values):
    reg = DataRegistry()
    register_config_inputs(cfg, reg)
    for key, (v, status) in values.items():
        reg.set(DataPoint(key=key, value=v, source_name="test", data_date="2026-09", method="api" if status == "ok" else "assumption",
                          status=status, confidence="medium", notes=None if status == "ok" else "hypothèse de test"))
    return reg


def test_missing_p1_market_data_makes_model_incomplete(tmp_path):
    cfg = cfg_with(tmp_path)
    ri = resolve_inputs(cfg, reg_for(cfg), load_defaults())
    assert not ri.complete and ri.inputs is None
    # données exigées : loyer ImmoStat et prix d'investissement (taux de sortie implicite) ; loyer et taux prime facultatifs
    assert ri.missing == sorted(["deal.acquisition_fees_pct", "market.average_rent",
                                 "market.investment_price_per_sqm", "rates.euribor_3m"])


def test_without_prime_data_rent_and_exit_yield_come_from_immostat(tmp_path):
    cfg, d = cfg_with(tmp_path), load_defaults()
    reg = reg_for(cfg, **{"market.average_rent": ({"last": 779.0}, "ok"),
                          "market.investment_price_per_sqm": ({"last": 13812.0}, "ok"),
                          "rates.euribor_3m": ({"last": 0.0263}, "ok"), "deal.acquisition_fees_pct": (0.075, "assumption")})
    ri = resolve_inputs(cfg, reg, d)
    assert ri.complete
    assert ri.inputs.relet_rent == 779.0
    assert ri.inputs.exit_yield == pytest.approx(779 / 13812)
    by = {p.name: p for p in ri.params}
    assert by["relet_rent"].origin == "registry" and "ImmoStat" in by["relet_rent"].note
    assert by["exit_yield"].reference == "market.average_rent / market.investment_price_per_sqm"


def test_inputs_from_registry_with_provenance(tmp_path):
    cfg, d = cfg_with(tmp_path), load_defaults()
    reg = reg_for(cfg, **{"market.prime_rent": (1100, "ok"), "market.average_rent": ({"last": 780.0}, "ok"),
                          "market.prime_yield": (0.0425, "ok"), "rates.euribor_3m": (0.021, "ok"),
                          "deal.acquisition_fees_pct": (0.075, "assumption")})
    ri = resolve_inputs(cfg, reg, d)
    assert ri.complete
    pos = d.financial_assumptions["relet_rent_position"].value
    assert ri.inputs.relet_rent == pytest.approx(780 + pos * (1100 - 780))
    assert ri.inputs.exit_yield == pytest.approx(0.0425 + d.financial_assumptions["exit_yield_premium_bps"].value / 1e4)
    inc = d.financial_assumptions["incentives_if_missing"].value
    assert ri.inputs.rent_free_months == pytest.approx(inc * d.financial_assumptions["firm_lease_term_months"].value)
    by = {p.name: p for p in ri.params}
    assert by["price"].origin == "config" and by["acq_fees_pct"].origin == "assumption"   # repli de la recherche
    assert by["relet_rent"].origin == "computed" and by["stabilised_occupancy"].origin == "assumption"
    assert all(p.note for p in ri.assumptions())                                         # chaque hypothèse justifiée
    assert ri.inputs.exit_date == quarter_end(cfg.operation.timeline.exit_year, 4)


def test_overrides_take_precedence(tmp_path):
    cfg = cfg_with(tmp_path, relet_rent_eur_sqm_year=900, exit_yield_pct=0.05, euribor_3m_pct=0.03,
                   acquisition_fees_pct=0.07, stabilised_occupancy_pct=0.9, rent_free_months=10)
    ri = resolve_inputs(cfg, reg_for(cfg), load_defaults())
    assert ri.complete
    i = ri.inputs
    assert (i.relet_rent, i.exit_yield, i.euribor, i.acq_fees_pct, i.stabilised_occupancy, i.rent_free_months) == \
           (900, 0.05, 0.03, 0.07, 0.9, 10)
    assert {p.name: p.origin for p in ri.params}["stabilised_occupancy"] == "override"


def test_incentives_from_market_data_are_used(tmp_path):
    cfg = cfg_with(tmp_path, relet_rent_eur_sqm_year=900, exit_yield_pct=0.05, euribor_3m_pct=0.03, acquisition_fees_pct=0.07)
    ri = resolve_inputs(cfg, reg_for(cfg, **{"market.incentives": (0.25, "ok")}), load_defaults())
    assert ri.inputs.rent_free_months == pytest.approx(0.25 * 72)


def test_rejected_value_is_not_used(tmp_path):
    cfg = cfg_with(tmp_path, relet_rent_eur_sqm_year=900, exit_yield_pct=0.05, acquisition_fees_pct=0.07)
    reg = reg_for(cfg, **{"rates.euribor_3m": (2.0, "ok")})
    reg.reject(reg.get("rates.euribor_3m"), "hors plage")
    ri = resolve_inputs(cfg, reg, load_defaults())
    assert "rates.euribor_3m" in ri.missing


# ------------------------------------------------------------------ Hélios : recalcul indépendant

def test_helios_total_cost_independent_recalculation(tmp_path):
    """Recalcul à la main, hors moteur, des postes du coût total d'Hélios (données du case + hypothèses)."""
    cfg, d = cfg_with(tmp_path, relet_rent_eur_sqm_year=940, exit_yield_pct=0.045, euribor_3m_pct=0.02,
                      acquisition_fees_pct=0.075), load_defaults()
    r = run_model(resolve_inputs(cfg, reg_for(cfg), d).inputs)
    a, op = cfg.asset, cfg.operation
    fa = {k: v.value for k, v in d.financial_assumptions.items()}
    hc = fa["vacant_holding_cost_eur_sqm_year"] / 12
    leased = sum(l.surface_sqm for l in op.current_leases)
    holding = ((a.surface_before_sqm - leased) * 6 + a.surface_before_sqm * 18 + a.surface_after_sqm * 6
               + a.surface_after_sqm * (1 - fa["stabilised_occupancy"]) * 6) * hc
    let_rent = 940 * a.surface_after_sqm * fa["stabilised_occupancy"]
    expected = (op.asking_price_eur * 1.075 + op.capex_eur_per_sqm_after * a.surface_after_sqm * 1.15
                + holding + let_rent * fa["marketing_fees_pct_annual_rent"])
    c = r["cost"]
    assert c["total_cost"] - c["interest"] - c["arrangement_fee"] == pytest.approx(expected)
    assert r["revenue"]["current_leases_income"] == pytest.approx(1200 * 520 / 2)        # bail jusqu'au 30/06/2027
    assert r["timeline"]["delivery"] == "2029-01-01"
    noi = let_rent * (1 - fa["non_recoverable_pct"]) - a.surface_after_sqm * (1 - fa["stabilised_occupancy"]) * hc * 12
    assert r["revenue"]["noi_stabilised"] == pytest.approx(noi)
    assert r["exit"]["gross_value"] == pytest.approx(noi / 0.045)
    assert all(r["checks"][k] for k in ("profit_identity", "debt_balance_identity"))


# ------------------------------------------------------------------ ajoutés après contrôle de mutation

def test_net_exit_value_subtracts_deductions_and_sale_costs():
    r = run_model(simple(rent_free_months=18, sale_costs_pct=0.02))
    e = r["exit"]
    remaining_rent_free = 6 * 500_000 / 12
    assert e["net_value"] == pytest.approx(10_000_000 - remaining_rent_free - 0.02 * 10_000_000)
    assert e["net_value"] == pytest.approx(e["gross_value"] - e["deductions_total"] - e["sale_costs"])
    # la valeur nette est bien celle encaissée au dernier trimestre
    assert r["cashflows"][-1]["unlevered"] == pytest.approx(e["net_value"])      # loyers nuls (franchise)


def test_capex_uses_surface_after_works():
    r = run_model(simple(surface_before=800, surface_after=1000))
    assert r["cost"]["capex"] == pytest.approx(1000 * 1000)
    assert r["acquisition"]["price_per_sqm_before"] == pytest.approx(10_000_000 / 800)


# ------------------------------------------------------------------ nœud financial_analysis

def _state(cfg, reg):
    from agent.state import build_initial_state
    from config.schema import load_investor_targets, load_sources_catalog
    st = build_initial_state(cfg, load_defaults(), load_investor_targets(), load_sources_catalog())
    st["registry"] = reg
    return st


def test_node_incomplete_produces_no_numbers(tmp_path):
    from agent.nodes.financial_analysis import financial_analysis
    cfg = cfg_with(tmp_path)
    out = financial_analysis(_state(cfg, reg_for(cfg)))["financials"]
    assert out["status"] == "incomplete" and "results" not in out and "max_price" not in out
    assert "market.average_rent" in out["missing_inputs"]


def test_node_complete_uses_fund_targets(tmp_path):
    from agent.nodes.financial_analysis import financial_analysis
    cfg = cfg_with(tmp_path, relet_rent_eur_sqm_year=940, exit_yield_pct=0.045, euribor_3m_pct=0.02,
                   acquisition_fees_pct=0.075)
    out = financial_analysis(_state(cfg, reg_for(cfg)))
    f = out["financials"]
    assert f["status"] == "complete" and f["targets"] == {"levered_irr": 0.15, "margin_on_cost": 0.15}
    assert f["max_price"]["max_price"] is not None
    assert any(p["origin"] == "assumption" for p in f["inputs"])
    json.dumps(f, default=str)                               # sérialisable pour analysis_results.json
