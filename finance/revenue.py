"""Module 2 : revenus locatifs (loyers en place, nouveau bail, NOI stabilisé)."""
from __future__ import annotations

from finance.inputs import DealInputs
from finance.timeline import Timeline, active_fraction, lease_month_fraction


def let_rent_annual(inp: DealInputs) -> float:
    """Loyer facial annuel du nouveau bail = loyer €/m²/an x surface louable x occupation stabilisée."""
    return inp.relet_rent * inp.surface_after * inp.stabilised_occupancy


def effective_rent_annual(inp: DealInputs) -> float:
    """Loyer économique : facial diminué de la franchise étalée sur la durée ferme du bail."""
    return let_rent_annual(inp) * (1 - inp.rent_free_months / inp.firm_lease_term_months)


def noi_stabilised(inp: DealInputs) -> float:
    """NOI stabilisé (facial) = loyer facial x (1 - charges non récupérables) - portage des surfaces vides."""
    vacant = inp.surface_after * (1 - inp.stabilised_occupancy)
    return let_rent_annual(inp) * (1 - inp.non_recoverable_pct) - vacant * inp.vacant_holding_cost


def noi_effective(inp: DealInputs) -> float:
    vacant = inp.surface_after * (1 - inp.stabilised_occupancy)
    return effective_rent_annual(inp) * (1 - inp.non_recoverable_pct) - vacant * inp.vacant_holding_cost


def current_rent_monthly(inp: DealInputs, tl: Timeline) -> list[float]:
    """Loyers des baux en place, jusqu'à leur échéance et au plus tard jusqu'au début des travaux."""
    out = [0.0] * tl.n_months
    for m in range(min(tl.works_start, tl.n_months)):
        out[m] = sum(l.surface_sqm * l.rent_eur_sqm_year / 12 * lease_month_fraction(tl.origin, m, l.end_date)
                     for l in inp.current_leases)
    return out


def leased_surface_before_works(inp: DealInputs, tl: Timeline, m: int) -> float:
    return sum(l.surface_sqm * lease_month_fraction(tl.origin, m, l.end_date) for l in inp.current_leases)


def new_lease_rent_monthly(inp: DealInputs, tl: Timeline) -> list[float]:
    """Loyer encaissé du nouveau bail : nul pendant la franchise, facial ensuite."""
    monthly = let_rent_annual(inp) / 12
    return [monthly * active_fraction(m, tl.rent_start) for m in range(tl.n_months)]
