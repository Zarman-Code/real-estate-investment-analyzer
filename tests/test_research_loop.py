"""Phase 3 : boucle de recherche, avec de FAUX connecteurs (aucun réseau).

Livrable du plan : « boucle testée avec un cas où une source est volontairement indisponible
(le système doit boucler une fois, puis continuer avec hypothèse signalée) ».
"""
from __future__ import annotations

import copy
import json
from datetime import date

import pytest

from agent.graph import NODES, build_graph, recursion_limit
from agent.nodes.identify_data_needs import identify_data_needs
from agent.nodes.validate_research import parse_data_date
from agent.state import build_initial_state
from config.schema import (PROJECT_ROOT, AssetConfig, SourcesCatalog, check_needs_against_catalog, load_config,
                           load_defaults, load_investor_targets, load_sources_catalog)
from tools.connectors import ConnectorRegistry
from tools.http import SourceUnavailable
from tools.registry import DataPoint, DataRegistry

ANALYSIS = date(2026, 11, 8)

# Valeurs factices, plausibles, utilisées UNIQUEMENT par les tests (jamais par l'agent)
GOOD = {
    "location.reference_point": ({"lat": 48.87, "lon": 2.34, "citycode": "75109", "score": 0.97}, None, None),
    "market.dvf_price_per_sqm": ({"median": 12000, "q1": 10000, "q3": 14000, "n": 12}, "€/m²", "2026-06"),
    "market.prime_rent": (1000, "€/m²/an", "2026-09"),
    "market.average_rent": (750, "€/m²/an", "2026-09"),
    "market.prime_yield": (0.0425, "fraction", "2026-Q3"),
    "market.vacancy": (0.03, "fraction", "2026-Q3"),
    "rates.euribor_3m": (0.02, "fraction", "2026-10"),
    "market.incentives": (0.18, "fraction", "2026-Q3"),
    "market.future_supply": (150000, "m²", "2026-Q3"),
    "market.take_up": (400000, "m²", "2026-Q3"),
    "index.ilat": (135.2, "indice", "2026-Q2"),
    "energy.dpe_comparables": ({"B": 3, "D": 10}, None, "2026-10"),
    "urbanism.plu_constraints": ({"max_height_m": 25}, None, "2025-01"),
    "urbanism.protected_buildings": (12, "bâtiments", "2025-01"),
    "risk.natural_risks": (["inondation"], None, "2026-01"),
    "risk.catnat": (3, "arrêtés", "2026-01"),
    "index.icc": (2100, "indice", "2026-Q2"),
    "index.bt01": (139.4, "indice", "2026-08"),
    "market.immediate_supply": (680000, "m²", "2026-Q3"),
    "market.investment_price_per_sqm": (13800, "€/m²", "2026-Q3"),
}


def point(key, source, overrides=None):
    value, unit, ddate = GOOD[key]
    if overrides and key in overrides:
        value = overrides[key]
    return DataPoint(key=key, value=value, unit=unit, source_name=source, data_date=ddate or "2026-11-01",
                     method="api", status="ok", confidence="high")


class Fakes:
    """Construit un registre de faux connecteurs et compte les appels."""

    def __init__(self, catalog: SourcesCatalog):
        self.catalog = catalog
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.registry = ConnectorRegistry()

    def add(self, source_id, behaviour="ok", values=None, needs_location=False):
        """behaviour : 'ok' | 'down' | callable(pass_number) -> 'ok'|'down'."""
        def fn(ctx, needs):
            keys = tuple(n.key for n in needs)
            self.calls.append((source_id, keys))
            b = behaviour(len([c for c in self.calls if c[0] == source_id])) if callable(behaviour) else behaviour
            if b == "down":
                raise SourceUnavailable(source_id, "https://fake.test", "HTTP 503 (volontaire)")
            if needs_location:
                assert ctx.registry.found("location.reference_point"), "vague 2 avant le géocodage"
            return [point(k, self.catalog.by_id(source_id).name, values) for k in keys]
        self.registry.register(source_id, fn, needs_location)
        return self

    def add_all(self, **behaviours):
        location = {"dvf", "aucadastre_dvf", "paris_plu_emprises", "paris_plu_protections",
                    "ademe_dpe_tertiaire", "georisques"}
        for s in self.catalog.sources:
            self.add(s.id, behaviours.get(s.id, "ok"), needs_location=s.id in location)
        return self


@pytest.fixture
def base():
    return load_config(), load_defaults(), load_investor_targets(), load_sources_catalog()


def with_overrides(tmp_path, **ov) -> AssetConfig:
    data = json.loads((PROJECT_ROOT / "asset_config.json").read_text(encoding="utf-8"))
    data["overrides"] = ov
    p = tmp_path / "c.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return load_config(p)


def run(cfg, defaults, targets, catalog, fakes):
    state = build_initial_state(cfg, defaults, targets, catalog, analysis_date=ANALYSIS)
    graph = build_graph(fakes.registry)
    return graph.invoke(state, config={"recursion_limit": recursion_limit(defaults.research.max_research_loops)})


def log_index(final, prefix):
    return next(i for i, line in enumerate(final["logs"]) if line.startswith(prefix))


# ------------------------------------------------------------------ chemin nominal

def test_all_sources_ok_single_pass(tmp_path, base):
    _, defaults, targets, catalog = base
    cfg = with_overrides(tmp_path, acquisition_fees_pct=0.07)   # seul P1 sans source : fourni par l'utilisateur
    final = run(cfg, defaults, targets, catalog, Fakes(catalog).add_all())
    assert final["validation_report"].verdict == "sufficient"
    assert final["research_iteration"] == 1
    # frais imposés par override : besoin non bloquant (P2), aucune hypothèse de repli appliquée
    fees = next(n for n in final["data_needs"] if n.key == "deal.acquisition_fees_pct")
    assert fees.priority == "P2" and "override" in fees.reason
    assert final["registry"].assumptions() == []
    assert [d.key for d in final["registry"].missing()] == ["deal.acquisition_fees_pct"]   # aucune source : normal


def test_downstream_order_and_single_recommendation(tmp_path, base):
    _, defaults, targets, catalog = base
    final = run(with_overrides(tmp_path, acquisition_fees_pct=0.07), defaults, targets, catalog,
                Fakes(catalog).add_all())
    i_fin, i_risk = log_index(final, "financial_analysis"), log_index(final, "risk_analysis")
    i_sens, i_scen = log_index(final, "sensitivity_analysis"), log_index(final, "scenario_analysis")
    i_rec = log_index(final, "investment_recommendation")
    assert max(i_fin, i_risk) < i_sens < i_scen < i_rec
    assert sum(line.startswith("investment_recommendation") for line in final["logs"]) == 1
    assert sum(line.startswith("sensitivity_analysis") for line in final["logs"]) == 1


def test_location_connectors_run_after_geocoding(base):
    cfg, defaults, targets, catalog = base
    fakes = Fakes(catalog).add_all()
    run(cfg, defaults, targets, catalog, fakes)
    order = [s for s, _ in fakes.calls]
    assert order.index("ign_geocodage") < order.index("dvf")


# ------------------------------------------------------------------ livrable du plan : source indisponible

def test_unavailable_source_loops_once_then_uses_alternative(tmp_path, base):
    """ImmoStat volontairement en panne : 1er passage -> lacune P1 sur le loyer moyen ;
    retour unique vers research_market_data, qui essaie les rapports de marché ; analyse ensuite."""
    _, defaults, targets, catalog = base
    cfg = with_overrides(tmp_path, acquisition_fees_pct=0.07)
    fakes = Fakes(catalog).add_all(immostat="down")
    final = run(cfg, defaults, targets, catalog, fakes)

    assert final["research_iteration"] == 2                         # boucle une fois
    # le prix d'investissement (P1) n'a qu'ImmoStat pour source : il reste manquant, l'analyse continue
    assert final["validation_report"].verdict == "proceed_with_assumptions"
    assert final["validation_report"].gaps == ["market.investment_price_per_sqm"]
    pass2 = [keys for s, keys in fakes.calls if s == "market_reports"]
    assert set(pass2[-1]) == {"market.average_rent"}                  # recherche limitée à la lacune P1
    assert final["registry"].get("market.average_rent").source_name.startswith("Rapports de marché")
    assert final["research_attempts"]["market.average_rent"] == ["immostat", "market_reports"]
    assert final["registry"].get("market.investment_price_per_sqm").status == "missing"
    assert any("retour 1/2" in line for line in final["logs"])


def test_everything_down_continues_with_flagged_assumption(base):
    """Toutes les sources en panne : l'agent ne plante pas, ne boucle pas à l'infini,
    applique l'hypothèse de repli SIGNALÉE pour les frais et laisse le reste 'missing'."""
    cfg, defaults, targets, catalog = base
    fakes = Fakes(catalog).add_all(**{s.id: "down" for s in catalog.sources})
    final = run(cfg, defaults, targets, catalog, fakes)

    report, reg = final["validation_report"], final["registry"]
    assert report.verdict == "proceed_with_assumptions"
    assert final["research_iteration"] <= 1 + defaults.research.max_research_loops
    fees = reg.get("deal.acquisition_fees_pct")
    assert fees.status == "assumption" and fees.method == "assumption" and "Fourchette" in fees.notes
    assert report.assumptions_applied == ["deal.acquisition_fees_pct"]
    assert reg.get("market.prime_rent").status == "missing"          # jamais d'hypothèse pour le marché
    assert reg.get("market.prime_rent").value is None
    assert any(line.startswith("financial_analysis") for line in final["logs"])   # l'analyse a bien lieu


def test_loop_is_capped_by_max_research_loops(base):
    """Avec 4 sources candidates toutes en panne et max 2 retours : exactement 3 passes, la 4e source jamais appelée."""
    cfg, defaults, targets, _ = base
    raw = load_sources_catalog().model_dump()
    for i in (1, 2, 3):   # loyer moyen (P1) : 1 source demandée par passage (min_sources = 1)
        raw["sources"].append({"id": f"extra_reports_{i}", "name": f"Rapports supplémentaires {i}", "type": "report",
                               "indicators": ["average_rent"], "priority": "low"})
    catalog = SourcesCatalog.model_validate(raw)
    fakes = Fakes(catalog).add_all(**{s.id: "down" for s in catalog.sources})
    final = run(cfg, defaults, targets, catalog, fakes)

    assert final["research_iteration"] == 3
    tried = final["research_attempts"]["market.average_rent"]
    assert tried == ["immostat", "market_reports", "extra_reports_1"]
    assert "extra_reports_2" not in [s for s, _ in fakes.calls]
    assert any("nombre maximal de retours atteint" in line for line in final["logs"])


def test_no_loop_when_max_loops_is_zero(base):
    cfg, defaults, targets, catalog = base
    d0 = defaults.model_copy(update={"research": defaults.research.model_copy(update={"max_research_loops": 0})})
    final = run(cfg, d0, targets, catalog, Fakes(catalog).add_all(immostat="down"))
    assert final["research_iteration"] == 1
    assert final["registry"].get("market.average_rent").status == "missing"


def test_no_pointless_loop_without_alternative_source(base):
    """Une lacune sans autre source ne déclenche pas de retour (inutile et coûteux)."""
    cfg, defaults, targets, catalog = base
    final = run(cfg, defaults, targets, catalog, Fakes(catalog).add_all(ecb_euribor="down"))
    assert final["research_iteration"] == 1
    assert final["registry"].get("rates.euribor_3m").status == "missing"


def test_missing_connectors_are_reported_not_invented(base):
    """Sans aucun connecteur (état actuel avant la phase 2) : tout est 'missing', raison explicite."""
    cfg, defaults, targets, catalog = base
    final = run(cfg, defaults, targets, catalog, Fakes(catalog))
    dp = final["registry"].get("rates.euribor_3m")
    assert dp.status == "missing" and dp.method == "none" and "connecteur non implémenté" in dp.notes
    assert final["research_iteration"] == 1


# ------------------------------------------------------------------ contrôles de validate_research

def issues_for(final, key):
    return [(i.check, i.severity) for i in final["validation_report"].issues if i.key == key]


def test_implausible_value_replaced_by_plausible_alternative(tmp_path, base):
    """ImmoStat renvoie 95 000 €/m²/an (erreur d'unité) : lacune, retour, rapports à 750 -> retenu."""
    _, defaults, targets, catalog = base
    cfg = with_overrides(tmp_path, acquisition_fees_pct=0.07)
    fakes = Fakes(catalog).add_all()
    fakes.registry.specs.pop("immostat")
    fakes.add("immostat", values={"market.average_rent": 95000})
    final = run(cfg, defaults, targets, catalog, fakes)
    assert final["research_iteration"] == 2
    assert final["registry"].get("market.average_rent").value == 750
    assert ("plausibility", "warning") in issues_for(final, "market.average_rent")
    assert final["validation_report"].verdict == "sufficient"


def test_quality_warnings(tmp_path, base):
    _, defaults, targets, catalog = base
    cfg = with_overrides(tmp_path, acquisition_fees_pct=0.07)
    old = dict(GOOD)
    try:
        GOOD["market.prime_rent"] = (700, "€/m²/an", "2024-06")            # périmé ET < loyer moyen
        GOOD["market.dvf_price_per_sqm"] = ({"median": 12000, "n": 3}, "€/m²", "2026-06")   # échantillon faible
        final = run(cfg, defaults, targets, catalog, Fakes(catalog).add_all())
    finally:
        GOOD.clear(); GOOD.update(old)
    rent = issues_for(final, "market.prime_rent")
    assert ("freshness", "warning") in rent
    assert ("coherence", "warning") in rent
    assert ("convergence", "warning") in rent                              # 1 source pour 2 demandées
    assert ("sample_size", "warning") in issues_for(final, "market.dvf_price_per_sqm")
    assert final["validation_report"].verdict == "sufficient"              # avertissements non bloquants


def test_divergence_between_sources(base):
    from agent.nodes.validate_research import check_need
    _, defaults, _, _ = base
    reg = DataRegistry()
    reg.add(point("market.prime_yield", "A"))
    reg.add(point("market.prime_yield", "B", {"market.prime_yield": 0.0525}))
    issues, gap = check_need(defaults.need("market.prime_yield"), reg, ANALYSIS, 0.10)
    assert not gap
    assert any(i.check == "convergence" and "écart" in i.message for i in issues)


@pytest.mark.parametrize("s,expected", [
    ("2026", date(2026, 12, 31)), ("2026-09", date(2026, 9, 30)), ("2026-Q3", date(2026, 9, 30)),
    ("2026-T1", date(2026, 3, 31)), ("2026-02-15", date(2026, 2, 15)), ("2026-09-15T10:00:00", date(2026, 9, 15)),
    ("sept. 2026", None), (None, None),
])
def test_parse_data_date(s, expected):
    assert parse_data_date(s) == expected


# ------------------------------------------------------------------ identify_data_needs

def test_identify_needs_order_scope_and_overrides(tmp_path, base):
    _, defaults, targets, catalog = base
    cfg = with_overrides(tmp_path, exit_yield_pct=0.045)
    out = identify_data_needs(build_initial_state(cfg, defaults, targets, catalog, analysis_date=ANALYSIS))
    needs = {n.key: n for n in out["data_needs"]}
    # override -> toujours recherché pour comparaison, mais plus bloquant
    assert needs["market.prime_yield"].priority == "P2" and "0.045" in needs["market.prime_yield"].reason
    assert [n.priority for n in out["data_needs"]] == sorted(n.priority for n in out["data_needs"])
    assert needs["market.average_rent"].target_sources == ["immostat", "market_reports"]  # fichier avant rapport
    assert needs["market.prime_rent"].target_sources == ["market_reports"]   # ImmoStat ne publie pas de loyer prime
    # DVF officiel d'abord, Firmio en recoupement ; aucadastre (prix d'appartements) écarté
    assert needs["market.dvf_price_per_sqm"].target_sources == ["dvf", "firmio_dvf_entreprises"]
    assert cfg.asset.reference_point.label in needs["market.dvf_price_per_sqm"].scope
    assert needs["market.prime_rent"].scope == cfg.asset.submarket


def test_catalog_cross_check(base):
    _, defaults, _, catalog = base
    warnings = check_needs_against_catalog(defaults, catalog)
    assert warnings == ["deal.acquisition_fees_pct : aucune source au catalogue pour ['acquisition_fees'] "
                        "(hypothèse de repli prévue)"]


# ------------------------------------------------------------------ registre : fusion

def test_registry_merge_never_degrades():
    reg = DataRegistry()
    miss = DataPoint.missing("market.vacancy", "S1", "panne")
    ok1, ok2 = point("market.vacancy", "S2"), point("market.vacancy", "S3")
    assert reg.merge(miss) == "set"
    assert reg.merge(ok1) == "replaced_missing"
    assert reg.merge(DataPoint.missing("market.vacancy", "S4", "panne")) == "ignored_missing"
    assert reg.merge(ok2) == "alternative"
    assert [p.source_name for p in reg.get_all("market.vacancy")] == ["S2", "S3"]
    reg.promote(ok2)
    assert reg.get("market.vacancy").source_name == "S3"


# ------------------------------------------------------------------ topologie du graphe (règle 1)

def test_graph_has_exactly_the_eight_nodes_and_edges():
    g = build_graph(ConnectorRegistry()).get_graph()
    nodes = {n for n in g.nodes if not n.startswith("__")}
    assert nodes == set(NODES) and len(nodes) == 8
    edges = {(e.source, e.target) for e in g.edges}
    expected = {
        ("__start__", "identify_data_needs"),
        ("identify_data_needs", "research_market_data"),
        ("research_market_data", "validate_research"),
        ("validate_research", "research_market_data"),
        ("validate_research", "financial_analysis"),
        ("validate_research", "risk_analysis"),
        ("financial_analysis", "sensitivity_analysis"),
        ("risk_analysis", "sensitivity_analysis"),
        ("sensitivity_analysis", "scenario_analysis"),
        ("financial_analysis", "investment_recommendation"),
        ("risk_analysis", "investment_recommendation"),
        ("scenario_analysis", "investment_recommendation"),
        ("investment_recommendation", "__end__"),
    }
    assert edges == expected


# ------------------------------------------------------------------ corrections après revue de la phase 3

def test_implausible_value_without_alternative_never_reaches_analysis(base):
    """Euribor à 2.0 au lieu de 0.02 (erreur d'unité), aucune autre source : la valeur est REJETÉE,
    conservée pour la traçabilité, et inutilisable par les calculs."""
    cfg, defaults, targets, catalog = base
    fakes = Fakes(catalog).add_all()
    fakes.registry.specs.pop("ecb_euribor")
    fakes.add("ecb_euribor", values={"rates.euribor_3m": 2.0})
    final = run(cfg, defaults, targets, catalog, fakes)
    reg = final["registry"]
    with pytest.raises(KeyError, match="REJETÉE"):
        reg.value("rates.euribor_3m")
    dp = reg.get("rates.euribor_3m")
    assert dp.status == "rejected" and dp.value == 2.0 and dp.confidence == "low"   # trace conservée
    assert reg.found("rates.euribor_3m") == []
    assert [p.key for p in reg.rejected()] == ["rates.euribor_3m"]
    assert final["validation_report"].verdict == "proceed_with_assumptions"


def test_implausible_p2_value_is_rejected_too(tmp_path, base):
    _, defaults, targets, catalog = base
    fakes = Fakes(catalog).add_all()
    fakes.registry.specs.pop("immostat")
    fakes.add("immostat")
    fakes.registry.specs.pop("market_reports")
    fakes.add("market_reports", values={"market.incentives": 3.0})      # 300 % au lieu de 30 % : donnée P2 aberrante
    final = run(with_overrides(tmp_path, acquisition_fees_pct=0.07), defaults, targets, catalog, fakes)
    assert final["registry"].get("market.incentives").status == "rejected"
    assert final["validation_report"].verdict == "sufficient"


def test_rejected_value_can_be_superseded_by_new_source():
    reg = DataRegistry()
    bad = point("market.vacancy", "S1", {"market.vacancy": 3.0})
    reg.set(bad)
    reg.reject(bad, "hors plage")
    assert reg.merge(point("market.vacancy", "S2")) == "replaced_missing"
    assert reg.value("market.vacancy") == 0.03
    assert [p.status for p in reg.get_all("market.vacancy")] == ["ok", "rejected"]


def test_rejected_requires_reason():
    with pytest.raises(Exception, match="REJETÉE"):
        DataPoint(key="market.vacancy", value=3.0, source_name="S", data_date="2026", method="api",
                  status="rejected", confidence="low", notes="hors plage")


def test_override_keeps_market_reference_researched(tmp_path, base):
    """Loyer imposé par l'utilisateur : les loyers de marché restent recherchés, pour comparaison."""
    _, defaults, targets, catalog = base
    cfg = with_overrides(tmp_path, relet_rent_eur_sqm_year=900, acquisition_fees_pct=0.07)
    final = run(cfg, defaults, targets, catalog, Fakes(catalog).add_all())
    needs = {n.key: n for n in final["data_needs"]}
    assert needs["market.prime_rent"].priority == "P2" and needs["market.average_rent"].priority == "P2"
    assert final["registry"].value("market.prime_rent") == 1000                   # référence disponible
    assert final["registry"].value("override.relet_rent_eur_sqm_year") == 900     # valeur imposée tracée


def test_override_need_failure_is_not_blocking(tmp_path, base):
    _, defaults, targets, catalog = base
    cfg = with_overrides(tmp_path, euribor_3m_pct=0.021, acquisition_fees_pct=0.07)
    final = run(cfg, defaults, targets, catalog, Fakes(catalog).add_all(ecb_euribor="down"))
    assert final["validation_report"].verdict == "sufficient"                     # override couvre le besoin
    assert final["registry"].get("rates.euribor_3m").status == "missing"



def test_cross_check_sources_are_all_queried_in_first_pass(tmp_path, base):
    """min_sources = 2 : DVF ET Firmio interrogés dès le 1er passage ; DVF (priorité) reste la valeur retenue."""
    _, defaults, targets, catalog = base
    fakes = Fakes(catalog).add_all()
    final = run(with_overrides(tmp_path, acquisition_fees_pct=0.07), defaults, targets, catalog, fakes)
    called = {s for s, keys in fakes.calls if "market.dvf_price_per_sqm" in keys}     # ordre d'appel libre
    assert called == {"dvf", "firmio_dvf_entreprises"} and final["research_iteration"] == 1
    srcs = [p.source_name for p in final["registry"].found("market.dvf_price_per_sqm")]
    assert srcs[0] == catalog.by_id("dvf").name and srcs[1] == catalog.by_id("firmio_dvf_entreprises").name


def test_primary_follows_source_priority_not_alphabet(base):
    """« aaa_low » est alphabétiquement premier mais moins prioritaire : il ne doit pas devenir la valeur retenue."""
    cfg, defaults, targets, _ = base
    raw = load_sources_catalog().model_dump()
    raw["sources"].append({"id": "aaa_low", "name": "Source peu fiable", "type": "report",
                           "indicators": ["price_per_sqm"], "priority": "low"})
    catalog = SourcesCatalog.model_validate(raw)
    d = defaults.model_copy(update={"data_needs": [n.model_copy(update={"min_sources": 3})
                                                   if n.key == "market.dvf_price_per_sqm" else n
                                                   for n in defaults.data_needs]})
    final = run(cfg, d, targets, catalog, Fakes(catalog).add_all(aaa_low="ok"))
    assert final["registry"].get("market.dvf_price_per_sqm").source_name == catalog.by_id("dvf").name



def test_registry_rank_sources():
    reg = DataRegistry()
    low, high = point("market.vacancy", "Basse"), point("market.vacancy", "Haute")
    reg.add(low); reg.add(high); reg.add(DataPoint.missing("market.vacancy", "Panne", "x"))
    reg.rank_sources("market.vacancy", ["Haute", "Basse"])
    assert [p.source_name for p in reg.get_all("market.vacancy")] == ["Haute", "Basse", "Panne"]
