"""Sérialisation des entrées du modèle (DealInputs) pour le moteur JavaScript du dashboard.
Les dates passent en ISO ; `_int_fields` liste les champs entiers (arrondis comme en Python par applyChanges)."""
from __future__ import annotations

from dataclasses import asdict, fields
from datetime import date

from finance.inputs import DealInputs


def _jsonable(v):
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


def inputs_to_json(inp: DealInputs) -> dict:
    out = {k: _jsonable(v) for k, v in asdict(inp).items()}
    out["_int_fields"] = [f.name for f in fields(inp)
                          if isinstance(getattr(inp, f.name), int) and not isinstance(getattr(inp, f.name), bool)]
    return out


def inputs_from_json(data: dict) -> DealInputs:
    """Inverse de inputs_to_json : reconstruit DealInputs (dates ISO -> date, baux -> Lease)."""
    from finance.inputs import Lease
    d = {k: v for k, v in data.items() if not k.startswith("_")}
    for k in ("acquisition_date", "works_start", "exit_date"):
        d[k] = date.fromisoformat(d[k])
    d["current_leases"] = tuple(Lease(l["surface_sqm"], l["rent_eur_sqm_year"], date.fromisoformat(l["end_date"]))
                                for l in d["current_leases"])
    return DealInputs(**d)
