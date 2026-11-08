from datetime import date

import pytest
from pydantic import ValidationError

from agent.state import DataNeed, build_initial_state
from config.schema import load_config, load_defaults, load_investor_targets, load_sources_catalog
from tools.registry import DataPoint, DataRegistry


def dp(**kw):
    base = dict(key="market.prime_rent_qca", value=1000.0, unit="€/m²/an", source_name="Test",
                data_date="2026-09-30", method="report_extraction", status="ok", confidence="medium")
    base.update(kw)
    return DataPoint(**base)


# ------------------------------------------------------------------ invariants DataPoint

def test_valid_datapoint():
    assert dp().retrieved_at  # horodaté automatiquement


def test_missing_must_have_no_value():
    with pytest.raises(ValidationError, match="ne jamais inventer"):
        dp(status="missing", confidence="low")


def test_missing_shortcut():
    m = DataPoint.missing("market.vacancy_qca", "ImmoStat", "onglet QCA absent")
    assert m.value is None and m.confidence == "low"


def test_ok_requires_value():
    with pytest.raises(ValidationError, match="impose une valeur"):
        dp(value=None)


def test_assumption_requires_justification():
    with pytest.raises(ValidationError, match="justifiée"):
        dp(method="assumption", status="assumption", notes=None)
    assert dp(method="assumption", status="assumption", notes="loyer prime -5 %").status == "assumption"


def test_assumption_method_requires_assumption_status():
    with pytest.raises(ValidationError, match="method='assumption'"):
        dp(method="assumption", status="ok", notes="x")


def test_sourced_ok_requires_data_date():
    with pytest.raises(ValidationError, match="data_date"):
        dp(data_date=None)
    assert dp(data_date=None, status="partial").status == "partial"


@pytest.mark.parametrize("key", ["prime_rent", "Market.Rent", "market.", "market rent.x"])
def test_key_format(key):
    with pytest.raises(ValidationError):
        dp(key=key)


# ------------------------------------------------------------------ registre

def test_registry_set_get_and_alternatives():
    reg = DataRegistry()
    reg.add(dp(value=1000.0, source_name="A"))
    reg.add(dp(value=1050.0, source_name="B"))
    assert reg.get("market.prime_rent_qca").source_name == "A"
    assert [p.source_name for p in reg.get_all("market.prime_rent_qca")] == ["A", "B"]
    reg.set(dp(value=990.0, source_name="C"))
    assert len(reg.get_all("market.prime_rent_qca")) == 1


def test_registry_value_never_returns_silent_default():
    reg = DataRegistry()
    reg.set(DataPoint.missing("market.vacancy_qca", "ImmoStat", "absent"))
    with pytest.raises(KeyError):
        reg.value("market.vacancy_qca")
    with pytest.raises(KeyError):
        reg.value("market.unknown")


def test_registry_missing_and_assumptions():
    reg = DataRegistry()
    reg.set(dp())
    reg.set(DataPoint.missing("market.vacancy_qca", "ImmoStat", "absent"))
    reg.set(dp(key="deal.acquisition_fees_pct", value=0.075, unit="fraction", method="assumption",
               status="assumption", notes="droits de mutation + frais"))
    assert [d.key for d in reg.missing()] == ["market.vacancy_qca"]
    assert [d.key for d in reg.assumptions()] == ["deal.acquisition_fees_pct"]


def test_registry_json_roundtrip(tmp_path):
    reg = DataRegistry()
    reg.add(dp(source_name="A"))
    reg.add(dp(source_name="B", value=1100.0))
    path = tmp_path / "out" / "data_registry.json"
    reg.export_json(path)
    back = DataRegistry.load_json(path)
    assert back.to_dict() == reg.to_dict()


# ------------------------------------------------------------------ état initial

def test_initial_state():
    cfg = load_config()
    st = build_initial_state(cfg, load_defaults(), load_investor_targets(), load_sources_catalog(),
                             analysis_date=date(2026, 11, 8))
    reg = st["registry"]
    assert st["analysis_date"] == "2026-11-08"
    assert st["research_iteration"] == 0
    assert reg.value("deal.asking_price_eur") == 28_000_000
    price = reg.get("deal.asking_price_eur")
    assert price.method == "user_input" and price.source_name.startswith("Case study")
    assert reg.missing() == [] and reg.assumptions() == []
    assert not any("champs null" in line for line in st["logs"])
    assert st["financials"] is None and st["recommendation"] is None


def test_initial_state_registers_overrides(tmp_path):
    import json
    from config.schema import PROJECT_ROOT
    data = json.loads((PROJECT_ROOT / "asset_config.json").read_text(encoding="utf-8"))
    data["overrides"] = {"exit_yield_pct": 0.045}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    st = build_initial_state(load_config(p), load_defaults(), load_investor_targets(), load_sources_catalog())
    assert st["registry"].value("override.exit_yield_pct") == 0.045


def test_data_need_priority():
    with pytest.raises(ValidationError):
        DataNeed(key="market.prime_rent_qca", indicator="prime_rent", priority="P4")
