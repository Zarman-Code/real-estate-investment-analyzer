"""Module 3 : coûts (acquisition, travaux, portage, commercialisation, charges non récupérables)."""
from __future__ import annotations

from finance.inputs import DealInputs
from finance.revenue import leased_surface_before_works, let_rent_annual
from finance.timeline import Timeline, active_fraction


def acquisition_fees(inp: DealInputs) -> float:
    return inp.price * inp.acq_fees_pct


def capex_total(inp: DealInputs) -> float:
    """Travaux = coût €/m² x surface APRÈS travaux (base du case)."""
    return inp.capex_per_sqm * inp.surface_after


def fees_contingency_total(inp: DealInputs) -> float:
    """Honoraires et aléas = % du coût des travaux."""
    return capex_total(inp) * inp.fees_contingency_pct


def works_monthly(inp: DealInputs, tl: Timeline) -> list[float]:
    """Travaux + honoraires et aléas, décaissés linéairement sur la durée des travaux."""
    per_month = (capex_total(inp) + fees_contingency_total(inp)) / inp.works_months
    return [per_month if m in tl.works_months() else 0.0 for m in range(tl.n_months)]


def vacant_surface(inp: DealInputs, tl: Timeline, m: int) -> float:
    if m < tl.works_start:
        return max(0.0, inp.surface_before - leased_surface_before_works(inp, tl, m))
    if m < tl.works_end:
        return inp.surface_before                       # immeuble entièrement vide pendant les travaux
    let_share = inp.stabilised_occupancy * active_fraction(m, tl.lease_start)
    return inp.surface_after * (1 - let_share)


def holding_monthly(inp: DealInputs, tl: Timeline) -> list[float]:
    """Portage des surfaces vides (taxes, assurance, charges)."""
    return [vacant_surface(inp, tl, m) * inp.vacant_holding_cost / 12 for m in range(tl.n_months)]


def non_recoverable_monthly(inp: DealInputs, tl: Timeline) -> list[float]:
    """Charges non récupérables sur les surfaces louées, dès la prise d'effet du bail (franchise comprise)."""
    base = let_rent_annual(inp) * inp.non_recoverable_pct / 12
    return [base * active_fraction(m, tl.lease_start) for m in range(tl.n_months)]


def marketing_monthly(inp: DealInputs, tl: Timeline) -> list[float]:
    out = [0.0] * tl.n_months
    if tl.lease_start < tl.n_months:
        out[tl.lease_start] = let_rent_annual(inp) * inp.marketing_fees_pct
    return out
