"""Calendrier du modèle : grille mensuelle à partir du mois d'acquisition.

Conventions (documentées dans le paragraphe « méthode ») :
- le mois 0 est le mois de l'acquisition ; l'acquisition est réglée au jour 0 (période T0) ;
- un flux du mois m est rattaché au trimestre (m // 3) + 1 ; le flux d'un trimestre est daté à sa fin ;
- la fin des travaux est exclusive : works_start + durée = premier mois après les travaux (livraison).
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from finance.inputs import DealInputs


def month_index(origin: date, d: date) -> int:
    return (d.year - origin.year) * 12 + (d.month - origin.month)


def month_start(origin: date, m: int) -> date:
    y, mo = divmod(origin.month - 1 + m, 12)
    return date(origin.year + y, mo + 1, 1)


def quarter_label(origin: date, k: int) -> str:
    """Libellé du trimestre k (k = 1 pour le trimestre de l'acquisition)."""
    d = month_start(origin, 3 * (k - 1))
    return f"{d.year}-T{(d.month - 1) // 3 + 1}"


@dataclass(frozen=True)
class Timeline:
    n_months: int            # mois modélisés, mois de cession inclus
    works_start: int         # indice du premier mois de travaux
    works_end: int           # indice du mois de livraison (exclusif)
    lease_start: int         # indice du mois de prise d'effet du nouveau bail
    rent_start: float        # indice (fractionnaire) de fin de franchise
    origin: date

    @property
    def n_quarters(self) -> int:
        return -(-self.n_months // 3)

    def works_months(self) -> range:
        return range(self.works_start, self.works_end)


def build_timeline(inp: DealInputs) -> Timeline:
    origin = date(inp.acquisition_date.year, inp.acquisition_date.month, 1)
    n = month_index(origin, inp.exit_date) + 1
    ws = month_index(origin, inp.works_start)
    we = ws + inp.works_months
    if ws < 0:
        raise ValueError("les travaux commencent avant l'acquisition")
    if we > n:
        raise ValueError("la cession intervient avant la fin des travaux")
    ls = we + inp.letting_void_months
    return Timeline(n_months=n, works_start=ws, works_end=we, lease_start=ls,
                    rent_start=ls + inp.rent_free_months, origin=origin)


def lease_month_fraction(origin: date, m: int, end: date) -> float:
    """Part du mois m couverte par un bail qui s'achève le jour `end` (inclus)."""
    start = month_start(origin, m)
    days = calendar.monthrange(start.year, start.month)[1]
    if end < start:
        return 0.0
    if end >= date(start.year, start.month, days):
        return 1.0
    return end.day / days


def active_fraction(m: int, start: float) -> float:
    """Part du mois m postérieure à l'instant `start` (exprimé en mois, éventuellement fractionnaire)."""
    return min(1.0, max(0.0, m + 1 - start))
