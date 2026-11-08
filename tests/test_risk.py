"""Tests de l'analyse des risques (phase 5) : matrice, garde-fous du modèle, règles de probabilité,
échelle d'impact, chocs, nœud complet (incomplet / complet), déterminisme."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest
from pydantic import ValidationError

from agent.nodes.risk_analysis import risk_analysis
from agent.state import build_initial_state
from config.schema import PROJECT_ROOT, load_config, load_defaults, load_investor_targets, load_sources_catalog
from finance.engine import run_model
from finance.inputs import resolve_inputs
from risk import catalog as K
from risk.matrix import EvidenceRef, Risk, finalise, impact_from_effect, level_for, matrix
from tools.registry import DataPoint, DataRegistry, register_config_inputs

D = load_defaults()
LEVELS = D.risk_scoring.criticality_levels


def cfg_with(tmp_path, asset=None, **ov):
    data = json.loads((PROJECT_ROOT / "asset_config.json").read_text(encoding="utf-8"))
    data["overrides"] = ov
    data["asset"].update(asset or {})
    p = tmp_path / "c.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return load_config(p)


def reg(cfg, **values) -> DataRegistry:
    r = DataRegistry()
    register_config_inputs(cfg, r)
    for key, v in values.items():
        r.set(DataPoint(key=key, value=v, source_name="test", data_date="2026-09", method="api",
                        status="ok", confidence="medium"))
    return r


def ctx(cfg, registry):
    return K.rule_context(cfg, registry, D)


# ------------------------------------------------------------------ matrice et niveaux

@pytest.mark.parametrize("c,level", [(1, "faible"), (4, "faible"), (5, "modéré"), (9, "modéré"),
                                     (10, "élevé"), (16, "élevé"), (17, "critique"), (25, "critique")])
def test_levels(c, level):
    assert level_for(c, LEVELS) == level


def base_risk(**kw):
    d = dict(id="X", category="financier", title="t", description="d", mitigations=["m"],
             missing_information=["x"], uncertain=True)
    d.update(kw)
    return Risk(**d)


def test_finalise_and_matrix():
    ev = [EvidenceRef(key="k", label="l", value=1)]
    r = finalise(base_risk(probability=4, probability_rationale="règle", evidence=ev, impact=3,
                           impact_rationale="règle", financial_effect={"x": 1}, uncertain=False), LEVELS)
    assert (r.criticality, r.level, r.uncertain) == (12, "élevé", False)
    u = finalise(base_risk(probability=4, probability_rationale="règle", evidence=ev), LEVELS)
    assert u.level == "incertain" and u.criticality is None
    grid = matrix([r, u])
    assert grid[2][3] == ["X"] and sum(len(c) for row in grid for c in row) == 1


def test_risk_guardrails():
    with pytest.raises(ValidationError, match="sans preuve"):
        base_risk(probability=3, probability_rationale="règle")                       # pas de preuve
    with pytest.raises(ValidationError, match="sans preuve"):
        base_risk(probability=3, evidence=[EvidenceRef(key="k", label="l", value=1)])  # pas de règle
    with pytest.raises(ValidationError, match="effet financier"):
        base_risk(impact=3, impact_rationale="règle")
    with pytest.raises(ValidationError, match="informations manquantes"):
        base_risk(missing_information=[])
    with pytest.raises(ValidationError, match="mesure de réduction"):
        base_risk(mitigations=[])


# ------------------------------------------------------------------ échelle d'impact

B = D.risk_scoring.impact_irr_drop_pts


@pytest.mark.parametrize("drop,expected", [(0.5, 1), (1.0, 2), (2.9, 2), (3.0, 3), (5.0, 4), (7.9, 4), (8.0, 5)])
def test_impact_bands_on_irr(drop, expected):
    impact, why = impact_from_effect(0.15, 0.15 - drop / 100, 0.10, 0.10, B)
    assert impact == expected and "pt" in why


@pytest.mark.parametrize("drop,expected", [(0.5, 1), (3.0, 3), (8.0, 5)])
def test_impact_bands_on_margin(drop, expected):
    impact, _ = impact_from_effect(0.15, 0.15, 0.20, 0.20 - drop / 100, B)
    assert impact == expected


def test_margin_becoming_negative_is_impact_5():
    assert impact_from_effect(0.15, 0.149, 0.02, -0.01, B)[0] == 5


def test_already_negative_margin_is_not_automatically_5():
    """Plan : impact 5 si la marge DEVIENT négative. Si elle l'est déjà, on mesure la dégradation."""
    assert impact_from_effect(-0.30, -0.31, -0.20, -0.21, B)[0] == 2


def test_irr_artefact_on_loss_making_deal_uses_margin():
    """Opération déficitaire : un retard rend le TRI MOINS négatif, mais la perte s'aggrave : la marge fait foi."""
    impact, why = impact_from_effect(-0.38, -0.37, -0.25, -0.30, B)
    assert impact == 4 and "marge" in why


# ------------------------------------------------------------------ chocs

def helios_inputs(tmp_path):
    cfg = cfg_with(tmp_path, relet_rent_eur_sqm_year=940, exit_yield_pct=0.045, euribor_3m_pct=0.02,
                   acquisition_fees_pct=0.075)
    return resolve_inputs(cfg, reg(cfg), D).inputs


def test_apply_shocks(tmp_path):
    inp = helios_inputs(tmp_path)
    s = D.risk_scoring.shocks
    assert K.apply_shock(inp, s["rent_level"]).relet_rent == pytest.approx(inp.relet_rent * 0.9)
    wd = K.apply_shock(inp, s["works_delay"])
    assert wd.works_months == inp.works_months + 6 and isinstance(wd.works_months, int)
    assert wd.exit_date == date(2030, 6, 30)                        # fin de trimestre, décalée de 6 mois
    assert K.apply_shock(inp, s["no_extension"]).surface_after == inp.surface_before
    assert K.apply_shock(inp, s["rates_up"]).euribor == pytest.approx(inp.euribor + 0.01)
    assert inp.relet_rent == 940 and inp.works_months == 18          # l'original n'est pas modifié
    for name, shock in s.items():                                    # chaque choc produit un modèle valide
        r = run_model(K.apply_shock(inp, shock))
        assert r["checks"]["profit_identity"], name


def test_every_shock_lowers_returns(tmp_path):
    inp = helios_inputs(tmp_path)
    base = run_model(inp)["returns"]["levered_irr"]
    for name, shock in D.risk_scoring.shocks.items():
        assert run_model(K.apply_shock(inp, shock))["returns"]["levered_irr"] < base, name


# ------------------------------------------------------------------ règles de probabilité

def test_rule_letting_delay(tmp_path):
    cfg = cfg_with(tmp_path)
    r0 = K.rule_letting_delay(ctx(cfg, reg(cfg)))
    assert r0.probability is None and r0.missing
    supply, take = {"last": 680000}, {"rolling_12m": 408000}                     # 20 mois d'offre
    r1 = K.rule_letting_delay(ctx(cfg, reg(cfg, **{"market.immediate_supply": supply, "market.take_up": take})))
    assert r1.probability == 3 and "20 mois" in r1.rationale and "taux de vacance" in r1.missing[0]
    r2 = K.rule_letting_delay(ctx(cfg, reg(cfg, **{"market.immediate_supply": supply, "market.take_up": take,
                                                    "market.vacancy": 0.10})))
    assert r2.probability == 4 and not r2.missing


def test_rule_rent_level(tmp_path):
    cfg = cfg_with(tmp_path)
    assert K.rule_rent_level(ctx(cfg, reg(cfg))).probability is None
    r = K.rule_rent_level(ctx(cfg, reg(cfg, **{"market.immediate_supply": {"yoy": 0.33},
                                                "market.average_rent": {"yoy": -0.05}})))
    assert r.probability == 4
    r = K.rule_rent_level(ctx(cfg, reg(cfg, **{"market.immediate_supply": {"yoy": 0.05},
                                                "market.average_rent": {"yoy": 0.02}})))
    assert r.probability == 2


def test_rule_capex_counts_structural_factors(tmp_path):
    cfg = cfg_with(tmp_path)                                         # Hélios : extension, F->B, années 1970
    r = K.rule_capex_overrun(ctx(cfg, reg(cfg)))
    assert r.probability == 4 and "BT01" in " ".join(r.missing)
    r = K.rule_capex_overrun(ctx(cfg, reg(cfg, **{"index.bt01": {"yoy": 0.03}})))
    assert r.probability == 5
    light = cfg_with(tmp_path, asset={"surface_after_sqm": 3000, "dpe_target": "D", "year_built_decade": 2000})
    assert K.rule_capex_overrun(ctx(light, reg(light, **{"index.bt01": {"yoy": 0.01}}))).probability == 1


def test_rule_works_delay_flags_tight_lease_release(tmp_path):
    cfg = cfg_with(tmp_path)                                         # bail au 30/06/2027, travaux au 01/07/2027
    r = K.rule_works_delay(ctx(cfg, reg(cfg, **{"urbanism.protected_buildings": {"count": 10}})))
    assert "1 jour(s)" in r.rationale and r.probability == 4


@pytest.mark.parametrize("last,avg,expected", [(0.0263, 0.0227, 4), (0.020, 0.025, 3)])
def test_rule_rates(tmp_path, last, avg, expected):
    cfg = cfg_with(tmp_path)
    r = K.rule_rates_up(ctx(cfg, reg(cfg, **{"rates.euribor_3m": {"last": last, "avg_12m": avg}})))
    assert r.probability == expected


@pytest.mark.parametrize("yoy,expected", [(-0.097, 4), (-0.02, 3), (0.01, 2)])
def test_rule_exit_yield(tmp_path, yoy, expected):
    cfg = cfg_with(tmp_path)
    r = K.rule_exit_yield_up(ctx(cfg, reg(cfg, **{"market.investment_price_per_sqm": {"yoy": yoy}})))
    assert r.probability == expected


def test_rule_planning_not_applicable_without_extension(tmp_path):
    cfg = cfg_with(tmp_path, asset={"surface_after_sqm": 3000})
    assert not K.rule_planning(ctx(cfg, reg(cfg))).applicable
    cfg = cfg_with(tmp_path)
    r = K.rule_planning(ctx(cfg, reg(cfg, **{"urbanism.protected_buildings": {"count": 150},
                                              "urbanism.plu_constraints": {"max_height": 25}})))
    assert r.probability == 4


def test_rule_dpe(tmp_path):
    cfg = cfg_with(tmp_path)
    assert K.rule_dpe(ctx(cfg, reg(cfg))).probability == 3                 # F -> B sans données ADEME
    r = K.rule_dpe(ctx(cfg, reg(cfg, **{"energy.dpe_comparables": {"share_b_or_better": 0.10}})))
    assert r.probability == 4


def test_rule_natural_never_invents(tmp_path):
    cfg = cfg_with(tmp_path)
    r = K.rule_natural(ctx(cfg, reg(cfg)))
    assert r.probability is None and any("Géorisques" in m for m in r.missing)


# ------------------------------------------------------------------ nœud

def node(cfg, registry):
    st = build_initial_state(cfg, D, load_investor_targets(), load_sources_catalog(), analysis_date=date(2026, 11, 8))
    st["registry"] = registry
    return risk_analysis(st)["risks"]


def test_node_without_financial_inputs_is_all_uncertain(tmp_path):
    cfg = cfg_with(tmp_path)
    out = node(cfg, reg(cfg))
    assert not out["financial_inputs_complete"]
    assert all(r["level"] == "incertain" and r["impact"] is None for r in out["risks"])
    assert all(r["probability"] is not None for r in out["risks"])              # seuls les risques caractérisés
    assert {"R1", "R2", "R5", "R6", "R7", "R9"} <= {e["id"] for e in out["excluded"]}   # sans donnée : non communiqués
    assert any("impact non chiffrable" in m for m in out["missing_information"])
    assert "diagnostic amiante avant travaux" in out["seller_requests"]


MARKET = {"market.immediate_supply": {"last": 680000, "yoy": 0.33}, "market.take_up": {"rolling_12m": 408000},
          "market.average_rent": {"last": 779, "yoy": -0.04}, "market.investment_price_per_sqm": {"yoy": -0.097},
          "rates.euribor_3m": {"last": 0.0263, "avg_12m": 0.0227}, "index.bt01": {"yoy": 0.025},
          "urbanism.protected_buildings": {"count": 120}, "urbanism.plu_constraints": {"max_height": 25},
          "energy.dpe_comparables": {"share_b_or_better": 0.2}}


def test_node_complete_scores_with_financial_effect(tmp_path):
    cfg = cfg_with(tmp_path, relet_rent_eur_sqm_year=940, exit_yield_pct=0.045, acquisition_fees_pct=0.075)
    out = node(cfg, reg(cfg, **MARKET))
    risks = {r["id"]: r for r in out["risks"]}
    assert out["financial_inputs_complete"]
    for rid in ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"):
        r = risks[rid]
        assert r["level"] != "incertain", rid
        assert r["criticality"] == r["probability"] * r["impact"]
        fe = r["financial_effect"]
        assert fe["shocked_levered_irr"] < fe["base_levered_irr"]                 # le choc dégrade toujours
        assert r["evidence"] and r["probability_rationale"] and r["impact_rationale"]
    assert "R9" not in risks and "R9" in {e["id"] for e in out["excluded"]}   # non caractérisé : non communiqué
    crit = [r["criticality"] or 0 for r in out["risks"] if r["level"] != "incertain"]
    assert crit == sorted(crit, reverse=True)                                     # tri par criticité
    placed = sorted(x for row in out["matrix"] for cell in row for x in cell)
    assert placed == sorted(r["id"] for r in out["risks"] if r["level"] != "incertain")
    assert all(r["probability"] is not None for r in out["risks"])


def test_node_is_deterministic(tmp_path):
    cfg = cfg_with(tmp_path, relet_rent_eur_sqm_year=940, exit_yield_pct=0.045, acquisition_fees_pct=0.075)
    a = node(cfg, reg(cfg, **MARKET))
    b = node(cfg, reg(cfg, **MARKET))
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def test_no_deal_figures_in_risk_texts():
    """Règle 4 : les textes du catalogue ne contiennent aucun chiffre propre au deal."""
    text = " ".join(f"{s.title} {s.description} {' '.join(s.mitigations)} {' '.join(s.seller_requests)}"
                    for s in K.CATALOG)
    cfg = load_config()
    for v in (cfg.operation.capex_eur_per_sqm_after, cfg.asset.surface_after_sqm, cfg.operation.asking_price_eur):
        assert f"{v:,.0f}".replace(",", " ") not in text and str(int(v)) not in text
