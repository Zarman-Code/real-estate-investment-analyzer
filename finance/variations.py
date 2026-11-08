"""Application d'une variation à des entrées du modèle. Partagée par les chocs de risque (phase 5),
les sensibilités, les seuils de bascule, les grilles et les scénarios (phase 6) : une seule implémentation.

Opérations : mult (multiplie), add (ajoute), set (remplace), set_field (copie un autre champ),
add_months (décale une date à la fin du mois, n mois plus tard). Les champs entiers restent entiers.
"""
from __future__ import annotations

import calendar
from dataclasses import replace
from datetime import date
from typing import Any, Iterable

from finance.inputs import DealInputs


def month_end_after(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, calendar.monthrange(year, month)[1])


def apply_changes(inp: DealInputs, changes: Iterable[Any], value: float | None = None) -> DealInputs:
    """`changes` : objets ayant field / op / value (value peut être fournie à part, pour les recherches)."""
    out: dict[str, Any] = {}
    for c in changes:
        v = getattr(c, "value", None) if value is None else value
        if v is None:
            raise ValueError(f"valeur manquante pour {c.field}")
        current = out.get(c.field, getattr(inp, c.field))
        if c.op == "mult":
            new = current * float(v)
        elif c.op == "add":
            new = current + v
        elif c.op == "set":
            new = v
        elif c.op == "set_field":
            new = getattr(inp, str(v))
        elif c.op == "add_months":
            new = month_end_after(current, int(round(v)))
        else:
            raise ValueError(f"opération inconnue {c.op}")
        if isinstance(getattr(inp, c.field), int) and not isinstance(getattr(inp, c.field), bool):
            new = int(round(new))
        out[c.field] = new
    return replace(inp, **out)
