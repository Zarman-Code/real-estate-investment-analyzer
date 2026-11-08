"""Module 1 : prix d'acquisition comparé aux références de marché disponibles dans le registre."""
from __future__ import annotations

from config.schema import DefaultsConfig
from tools.registry import DataRegistry

# références comparées au prix demandé, et base de surface pertinente pour chacune
REFERENCES = {
    "market.dvf_price_per_sqm": ("médiane DVF des comparables", "median", "surface_before"),
    "market.investment_price_per_sqm": ("prix moyen d'investissement bureaux du sous-marché", "last", "surface_after"),
}


def compare_price(price: float, surface_before: float, surface_after: float, registry: DataRegistry,
                  defaults: DefaultsConfig) -> dict:
    per_sqm = {"surface_before": price / surface_before, "surface_after": price / surface_after}
    out = {"asking_price_per_sqm_before": per_sqm["surface_before"],
           "asking_price_per_sqm_after": per_sqm["surface_after"], "references": []}
    for key, (label, field_name, basis) in REFERENCES.items():
        for dp in registry.found(key):
            v = dp.value.get(field_name) if isinstance(dp.value, dict) else dp.value
            if not isinstance(v, (int, float)):
                continue
            ref = {"key": key, "label": label, "source": dp.source_name, "data_date": dp.data_date,
                   "confidence": dp.confidence, "value_per_sqm": float(v), "basis": basis,
                   "gap_pct": per_sqm[basis] / float(v) - 1}
            if isinstance(dp.value, dict) and {"q1", "q3"} <= dp.value.keys():
                ref["value_range"] = [dp.value["q1"] * surface_before, dp.value["q3"] * surface_before]
            out["references"].append(ref)
    out["note"] = ("bâtiment obsolète (DPE F) : une décote est attendue face à des références d'immeubles "
                   "en état d'usage ou restructurés")
    return out
