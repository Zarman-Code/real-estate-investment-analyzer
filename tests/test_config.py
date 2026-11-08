import copy
import json
import re
from datetime import date

import pytest
import yaml

from config.schema import (
    ConfigError, PROJECT_ROOT, add_months, load_config, load_defaults,
    load_investor_targets, load_sources_catalog,
)

BASE = json.loads((PROJECT_ROOT / "asset_config.json").read_text(encoding="utf-8"))


def write_cfg(tmp_path, data, name="cfg.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def mutate(**changes):
    """changes: 'operation.timeline.works_start' -> value"""
    data = copy.deepcopy(BASE)
    for path, value in changes.items():
        node = data
        *parents, leaf = path.split("__")
        for p in parents:
            node = node[p]
        node[leaf] = value
    return data


def assert_config_error(tmp_path, data, fragment):
    with pytest.raises(ConfigError) as exc:
        load_config(write_cfg(tmp_path, data))
    assert fragment in str(exc.value), str(exc.value)


# ------------------------------------------------------------------ config réelle

def test_real_config_matches_case():
    cfg = load_config()
    assert cfg.asset.name == "Hélios"
    assert cfg.asset.surface_before_sqm == 3000
    assert cfg.asset.surface_after_sqm == 3200
    assert cfg.asset.dpe_current == "F" and cfg.asset.dpe_target == "B"
    assert cfg.asset.reference_point.address == "6 rue Drouot, 75009 Paris"
    op = cfg.operation
    assert op.asking_price_eur == 28_000_000
    assert op.capex_eur_per_sqm_after == 4000
    assert op.fees_and_contingency_pct_of_capex == 0.15
    assert op.current_leases[0].surface_sqm == 1200
    assert op.current_leases[0].rent_eur_sqm_year == 520
    assert op.financing.ltv == 0.60 and op.financing.margin_bps == 300
    assert op.timeline.works_duration_months == 18
    assert cfg.works_end == date(2029, 1, 1)
    assert cfg.overrides == {}


def test_real_config_has_no_warnings():
    assert load_config().consistency_warnings() == []


def test_real_config_has_no_null_fields():
    assert load_config().null_fields() == []


def test_null_fields_are_reported_not_rejected(tmp_path):
    cfg = load_config(write_cfg(tmp_path, mutate(asset__dpe_current=None)))
    assert cfg.null_fields() == ["asset.dpe_current"]


def test_asset_address_not_accepted(tmp_path):
    """Seul le secteur compte : l'adresse de l'actif n'est pas un champ de la config."""
    assert_config_error(tmp_path, mutate(asset__address="1 rue X"), "asset.address")


def test_reference_point_required(tmp_path):
    data = copy.deepcopy(BASE)
    del data["asset"]["reference_point"]["address"]
    assert_config_error(tmp_path, data, "reference_point.address")


# ------------------------------------------------------------------ erreurs de lecture

def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="introuvable"):
        load_config(tmp_path / "nope.json")


def test_invalid_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="illisible"):
        load_config(p)


# ------------------------------------------------------------------ champs obligatoires / types

@pytest.mark.parametrize("section,field", [
    ("asset", "surface_before_sqm"), ("asset", "surface_after_sqm"), ("asset", "reference_point"),
    ("operation", "asking_price_eur"), ("operation", "capex_eur_per_sqm_after"),
    ("operation", "financing"), ("operation", "timeline"),
])
def test_required_fields(tmp_path, section, field):
    data = copy.deepcopy(BASE)
    del data[section][field]
    assert_config_error(tmp_path, data, f"{section}.{field}")


def test_unknown_field_rejected(tmp_path):
    data = mutate(asset__surface_befor_sqm=3000)  # faute de frappe
    assert_config_error(tmp_path, data, "surface_befor_sqm")


@pytest.mark.parametrize("path,value,fragment", [
    ("asset__surface_before_sqm", 0, "surface_before_sqm"),
    ("operation__asking_price_eur", -1, "asking_price_eur"),
    ("operation__fees_and_contingency_pct_of_capex", 15, "fees_and_contingency_pct_of_capex"),
    ("operation__occupancy_current_pct", 1.4, "occupancy_current_pct"),
    ("asset__dpe_current", "H", "dpe_current"),
    ("asset__year_built_decade", 1974, "décennie"),
])
def test_field_bounds(tmp_path, path, value, fragment):
    assert_config_error(tmp_path, mutate(**{path: value}), fragment)


@pytest.mark.parametrize("ltv", [-0.1, 1.2])
def test_ltv_bounds(tmp_path, ltv):
    assert_config_error(tmp_path, mutate(operation__financing__ltv=ltv), "ltv")


def test_bad_date_format(tmp_path):
    assert_config_error(tmp_path, mutate(operation__timeline__acquisition_date="01/01/2027"), "acquisition_date")


# ------------------------------------------------------------------ cohérence

def test_surface_after_lower_than_before(tmp_path):
    assert_config_error(tmp_path, mutate(asset__surface_after_sqm=2500), "surface_after_sqm")


def test_works_before_acquisition(tmp_path):
    assert_config_error(tmp_path, mutate(operation__timeline__works_start="2026-06-01"), "works_start")


def test_exit_before_letting(tmp_path):
    assert_config_error(tmp_path, mutate(operation__timeline__exit_year=2028), "exit_year")


def test_works_ending_after_letting_year(tmp_path):
    assert_config_error(tmp_path, mutate(operation__timeline__works_duration_months=36), "fin des travaux")


def test_leased_surface_exceeds_building(tmp_path):
    data = copy.deepcopy(BASE)
    data["operation"]["current_leases"][0]["surface_sqm"] = 3500
    assert_config_error(tmp_path, data, "surface louée")


def test_multiple_errors_reported_together(tmp_path):
    data = mutate(asset__surface_after_sqm=2500, operation__timeline__exit_year=2028)
    with pytest.raises(ConfigError) as exc:
        load_config(write_cfg(tmp_path, data))
    assert "surface_after_sqm" in str(exc.value) and "exit_year" in str(exc.value)


# ------------------------------------------------------------------ avertissements non bloquants

def test_occupancy_mismatch_is_warning(tmp_path):
    cfg = load_config(write_cfg(tmp_path, mutate(operation__occupancy_current_pct=0.6)))
    assert any("occupancy_current_pct" in w for w in cfg.consistency_warnings())


def test_lease_after_works_start_is_warning(tmp_path):
    data = copy.deepcopy(BASE)
    data["operation"]["current_leases"][0]["end_date"] = "2028-01-01"
    cfg = load_config(write_cfg(tmp_path, data))
    assert any("après le début des travaux" in w for w in cfg.consistency_warnings())


# ------------------------------------------------------------------ overrides

def test_valid_override(tmp_path):
    cfg = load_config(write_cfg(tmp_path, mutate(overrides={"exit_yield_pct": 0.045})))
    assert cfg.overrides["exit_yield_pct"] == 0.045


@pytest.mark.parametrize("ov,fragment", [
    ({"exit_yeild_pct": 0.045}, "clé inconnue"),
    ({"exit_yield_pct": 4.5}, "fraction"),       # erreur classique : pourcentage au lieu de fraction
    ({"relet_rent_eur_sqm_year": 0}, "> 0"),
    ({"rent_free_months": -1}, ">= 0"),
])
def test_invalid_overrides(tmp_path, ov, fragment):
    assert_config_error(tmp_path, mutate(overrides=ov), fragment)


# ------------------------------------------------------------------ helpers

@pytest.mark.parametrize("start,months,expected", [
    (date(2027, 7, 1), 18, date(2029, 1, 1)),
    (date(2027, 1, 31), 1, date(2027, 2, 28)),
    (date(2027, 11, 15), 2, date(2028, 1, 15)),
])
def test_add_months(start, months, expected):
    assert add_months(start, months) == expected


# ------------------------------------------------------------------ YAML de paramétrage

def test_defaults_load():
    d = load_defaults()
    assert d.research.max_research_loops == 2
    assert d.research.radius_m == [500, 1000, 1500]


def test_defaults_criticality_must_cover_1_25(tmp_path):
    raw = yaml.safe_load((PROJECT_ROOT / "config/defaults.yaml").read_text(encoding="utf-8"))
    raw["risk_scoring"]["criticality_levels"]["critique"] = [18, 25]  # trou à 17
    p = tmp_path / "d.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match="couvrir"):
        load_defaults(p)


def test_investor_targets_are_flagged_assumptions():
    t = load_investor_targets()
    assert {"levered_irr_min", "equity_multiple_min", "margin_on_cost_min", "yoc_spread_min_bps"} <= t.targets.keys()
    assert all(x.status == "assumption" and x.rationale for x in t.targets.values())


def test_sources_catalog():
    cat = load_sources_catalog()
    assert cat.by_id("ecb_euribor").type == "api"
    assert cat.for_indicator("price_per_sqm")[0].priority == "very_high"
    # une source n'est « vérifiée » que si une vraie réponse datée a servi à écrire son parsing
    assert all(s.verified_on for s in cat.sources if s.verified)
    assert not cat.by_id("market_reports").verified


def test_sources_catalog_duplicate_ids(tmp_path):
    raw = yaml.safe_load((PROJECT_ROOT / "config/sources_catalog.yaml").read_text(encoding="utf-8"))
    raw["sources"].append(raw["sources"][0])
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match="dupliqués"):
        load_sources_catalog(p)


# ------------------------------------------------------------------ règle 4 : pas de données du deal ailleurs

PARAM_FILES = ("defaults.yaml", "investor_targets.yaml", "sources_catalog.yaml")


def _number_variants(x: float) -> list[str]:
    """28000000 -> ['28000000', '28 000 000', '28,000,000', '28.000.000']."""
    n = int(x)
    grouped = f"{n:,}"
    return list(dict.fromkeys([str(n), grouped.replace(",", " "), grouped.replace(",", " "),
                               grouped, grouped.replace(",", ".")]))


def deal_markers(cfg) -> list[tuple[str, re.Pattern]]:
    """Marqueurs construits À PARTIR DE LA CONFIG : valables pour n'importe quel deal.

    - textes : nom de l'actif, adresse et voie du point de référence, code postal,
      secteur, quartier ;
    - nombres entiers >= 100 : prix, surfaces, coût travaux, loyers et surfaces des baux.
      Les ratios (0.15, 0.60...) et petits entiers (18 mois, 300 bps) sont exclus :
      ce sont des valeurs génériques qui apparaissent légitimement dans les paramètres
      (ex. une marge cible de 0.15) et créeraient des faux positifs.
    Les nombres sont cherchés comme mots entiers, pas comme sous-chaînes.
    """
    a, op = cfg.asset, cfg.operation
    texts = [a.name, a.sector, a.district, a.reference_point.address]
    street = a.reference_point.address.split(",")[0]
    texts.append(re.sub(r"^\d+\s*(bis|ter)?\s*", "", street))           # "rue Drouot"
    texts += re.findall(r"\b\d{5}\b", a.reference_point.address)        # code postal
    numbers = [op.asking_price_eur, a.surface_before_sqm, a.surface_after_sqm, op.capex_eur_per_sqm_after]
    for lease in op.current_leases:
        numbers += [lease.surface_sqm, lease.rent_eur_sqm_year]

    markers = [(t, re.compile(re.escape(t), re.IGNORECASE)) for t in texts if t and len(t) >= 4]
    for x in numbers:
        if x >= 100 and float(x).is_integer():
            markers += [(v, re.compile(rf"(?<![\d.,]){re.escape(v)}(?![\d.,]?\d)")) for v in _number_variants(x)]
    return markers


def find_deal_data(cfg, text: str) -> list[str]:
    return sorted({label for label, pat in deal_markers(cfg) if pat.search(text)})


def test_no_deal_data_outside_asset_config():
    """Règle 4 : aucune donnée du deal courant dans les fichiers de paramétrage."""
    cfg = load_config()
    for name in PARAM_FILES:
        text = (PROJECT_ROOT / "config" / name).read_text(encoding="utf-8")
        assert find_deal_data(cfg, text) == [], f"données du deal trouvées dans {name}"


def test_deal_markers_follow_the_config(tmp_path):
    """Le garde-fou suit le deal : avec un autre actif, il cherche les données de cet actif."""
    other = mutate(asset__name="Orion", asset__reference_point={"label": "Mairie", "address": "2 place Bellecour, 69002 Lyon"},
                   operation__asking_price_eur=41500000)
    cfg = load_config(write_cfg(tmp_path, other))
    leaked = "q: '69002'\nnote: immeuble Orion, place Bellecour, prix 41 500 000"
    assert find_deal_data(cfg, leaked) == ["41 500 000", "69002", "Orion", "place Bellecour"]
    # les données de l'ancien deal ne sont plus des marqueurs
    assert find_deal_data(cfg, "Hélios 75009 rue Drouot") == []


def test_deal_markers_no_false_positives():
    cfg = load_config()
    # nombres qui contiennent une valeur du deal sans en être une
    assert find_deal_data(cfg, "limit: 32000 ; idbank 001617112 ; value: 5200 ; 0.15") == []
    # vraies fuites, dans plusieurs formats
    assert find_deal_data(cfg, "prix: 28 000 000") == ["28 000 000"]
    assert find_deal_data(cfg, "surface: 3200") == ["3200"]
