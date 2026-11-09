"""Résolution des entrées du modèle financier, avec provenance de CHAQUE paramètre.

Ordre de priorité pour un paramètre :
  1. override de l'utilisateur (asset_config.json/overrides)       origin = "override"
  2. donnée du registre (recherche, ou hypothèse de repli validée)  origin = "registry" / "computed"
  3. hypothèse de modélisation (defaults.yaml/financial_assumptions) origin = "assumption"
Une donnée de marché P1 (loyers, taux prime, Euribor) n'a JAMAIS d'hypothèse : si elle manque,
le modèle est déclaré incomplet et la liste des manques est renvoyée.

Aucun chiffre du deal n'est écrit ici : tout vient de asset_config.json, du registre ou de defaults.yaml.
"""
from __future__ import annotations

from decision import fmt

import calendar
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from config.schema import AssetConfig, DefaultsConfig
from tools.registry import DataRegistry


@dataclass(frozen=True)
class Lease:
    surface_sqm: float
    rent_eur_sqm_year: float
    end_date: date


@dataclass(frozen=True)
class DealInputs:
    """Toutes les grandeurs du modèle, en unités explicites (€, m², fractions, mois)."""
    # acquisition
    price: float
    acq_fees_pct: float
    surface_before: float
    surface_after: float
    # travaux
    capex_per_sqm: float                 # €/m² de surface APRÈS travaux
    fees_contingency_pct: float          # en % du coût des travaux
    # calendrier
    acquisition_date: date
    works_start: date
    works_months: int
    exit_date: date                      # dernier jour du trimestre de cession
    # revenus
    current_leases: tuple[Lease, ...]
    relet_rent: float                    # loyer facial €/m²/an
    letting_void_months: int
    rent_free_months: float
    stabilised_occupancy: float
    non_recoverable_pct: float
    vacant_holding_cost: float           # €/m²/an sur les surfaces vides
    marketing_fees_pct: float
    # sortie
    exit_yield: float
    sale_costs_pct: float
    # dette
    ltv: float
    ltv_basis: str                       # "total_cost" | "price"
    euribor: float
    margin_bps: float
    euribor_floor: float
    arrangement_fee_pct: float
    firm_lease_term_months: int = 72

    @property
    def interest_rate(self) -> float:
        return max(self.euribor, self.euribor_floor) + self.margin_bps / 10_000


@dataclass
class Param:
    name: str
    value: Any
    unit: str | None
    origin: str              # config | registry | computed | override | assumption
    reference: str | None    # clé du registre, chemin de la config ou de defaults.yaml
    note: str | None = None
    fallback: bool = False   # valeur de repli faute de la donnée de marché visée : signalée dans les réserves


@dataclass
class ResolvedInputs:
    inputs: DealInputs | None
    params: list[Param] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.inputs is not None and not self.missing

    def assumptions(self) -> list[Param]:
        return [p for p in self.params if p.origin == "assumption"]


def quarter_end(year: int, quarter: int) -> date:
    month = 3 * quarter
    return date(year, month, calendar.monthrange(year, month)[1])


def registry_number(registry: DataRegistry, defaults: DefaultsConfig, key: str) -> float | None:
    """Valeur numérique utilisable d'une clé (None si absente, manquante ou rejetée)."""
    try:
        v = registry.value(key)
    except KeyError:
        return None
    if isinstance(v, dict):
        field_name = next((n.value_field for n in defaults.data_needs if n.key == key), None) or "last"
        v = v.get(field_name)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def resolve_inputs(config: AssetConfig, registry: DataRegistry, defaults: DefaultsConfig) -> ResolvedInputs:
    a, op, t, fin = config.asset, config.operation, config.operation.timeline, config.operation.financing
    fa, ov = defaults.financial_assumptions, config.overrides
    params: list[Param] = []
    missing: list[str] = []

    def from_config(name, value, unit, path):
        params.append(Param(name, value, unit, "config", f"asset_config.json:{path}"))
        return value

    def assumption(name):
        spec = fa[name]
        if spec.override and spec.override in ov:
            params.append(Param(name, ov[spec.override], spec.unit, "override", f"overrides.{spec.override}"))
            return ov[spec.override]
        params.append(Param(name, spec.value, spec.unit, "assumption", f"defaults.yaml:financial_assumptions.{name}",
                            spec.rationale.strip()))
        return spec.value

    def market(name, key, unit, override=None, note=None):
        if override and override in ov:
            params.append(Param(name, ov[override], unit, "override", f"overrides.{override}"))
            return ov[override]
        v = registry_number(registry, defaults, key)
        if v is None:
            missing.append(key)
            return None
        dp = registry.get(key)
        origin = "assumption" if dp.status == "assumption" else "registry"
        params.append(Param(name, v, unit, origin, key, note or (f"{dp.source_name} ({dp.data_date})" if dp.data_date
                                                                else (dp.notes or dp.source_name)),
                            fallback=dp.status == "assumption"))
        return v

    # ---------------------------------------------------------------- données du case
    price = from_config("price", op.asking_price_eur, "€", "operation.asking_price_eur")
    s_before = from_config("surface_before", a.surface_before_sqm, "m²", "asset.surface_before_sqm")
    s_after = from_config("surface_after", a.surface_after_sqm, "m²", "asset.surface_after_sqm")
    capex = from_config("capex_per_sqm", op.capex_eur_per_sqm_after, "€/m²", "operation.capex_eur_per_sqm_after")
    fees_c = from_config("fees_contingency_pct", op.fees_and_contingency_pct_of_capex, "fraction",
                         "operation.fees_and_contingency_pct_of_capex")
    ltv = from_config("ltv", fin.ltv, "fraction", "operation.financing.ltv")
    margin = from_config("margin_bps", fin.margin_bps, "bps", "operation.financing.margin_bps")
    leases = tuple(Lease(l.surface_sqm, l.rent_eur_sqm_year, l.end_date) for l in op.current_leases)
    params.append(Param("current_leases", [l.__dict__ for l in leases], None, "config", "asset_config.json:operation.current_leases"))
    exit_q = int(assumption("exit_quarter"))
    exit_date = quarter_end(t.exit_year, exit_q)
    params.append(Param("exit_date", exit_date.isoformat(), None, "computed", "timeline.exit_year + exit_quarter"))

    # ---------------------------------------------------------------- marché (P1 : jamais d'hypothèse)
    acq_fees = market("acq_fees_pct", "deal.acquisition_fees_pct", "fraction", override="acquisition_fees_pct")
    euribor = market("euribor", "rates.euribor_3m", "fraction", override="euribor_3m_pct",
                     note="dernière valeur observée, supposée constante (pas de courbe à terme)")

    if "relet_rent_eur_sqm_year" in ov:
        relet = ov["relet_rent_eur_sqm_year"]
        params.append(Param("relet_rent", relet, "€/m²/an", "override", "overrides.relet_rent_eur_sqm_year"))
    else:
        # loyer prime : complémentaire (P2) ; loyer moyen ImmoStat : exigé (P1)
        prime = registry_number(registry, defaults, "market.prime_rent")
        avg = registry_number(registry, defaults, "market.average_rent")
        relet = None
        if avg is None:
            missing.append("market.average_rent")
        elif prime is not None:
            pos = assumption("relet_rent_position")
            relet = avg + pos * (prime - avg)
            params.append(Param("relet_rent", relet, "€/m²/an", "computed",
                                "market.average_rent + position x (market.prime_rent - market.average_rent)",
                                f"loyer moyen {fmt.eur_sqm(avg)}/an + {fmt.pct(pos, 0)} de l'écart avec le loyer prime "
                                f"{fmt.eur_sqm(prime)}/an"))
        else:
            relet = avg
            params.append(Param("relet_rent", relet, "€/m²/an", "registry", "market.average_rent",
                                fa["relet_rent_fallback"].rationale.strip(), fallback=True))

    if "exit_yield_pct" in ov:
        exit_yield = ov["exit_yield_pct"]
        params.append(Param("exit_yield", exit_yield, "fraction", "override", "overrides.exit_yield_pct"))
    else:
        # rendement prime : complémentaire (P2) ; à défaut, rendement implicite ImmoStat (loyer / prix d'investissement)
        prime_y = registry_number(registry, defaults, "market.prime_yield")
        exit_yield = None
        if prime_y is not None:
            prem = assumption("exit_yield_premium_bps")
            exit_yield = prime_y + prem / 10_000
            params.append(Param("exit_yield", exit_yield, "fraction", "computed", "market.prime_yield + prime de risque",
                                f"taux de rendement prime {fmt.pct(prime_y, 2)} + prime de risque de {fmt.bps(prem)}"))
        else:
            avg_r = registry_number(registry, defaults, "market.average_rent")
            inv = registry_number(registry, defaults, "market.investment_price_per_sqm")
            if avg_r is None or inv is None or inv <= 0:
                missing += [k for k, v in (("market.average_rent", avg_r), ("market.investment_price_per_sqm", inv)) if v is None]
            else:
                exit_yield = avg_r / inv
                params.append(Param("exit_yield", exit_yield, "fraction", "computed",
                                    "market.average_rent / market.investment_price_per_sqm",
                                    f"rendement implicite {avg_r:.0f} / {inv:.0f} = {exit_yield:.4f} ; "
                                    + fa["exit_yield_fallback"].rationale.strip(), fallback=True))

    # ---------------------------------------------------------------- franchise : donnée P2, hypothèse admise
    firm = int(assumption("firm_lease_term_months"))
    if "rent_free_months" in ov:
        rent_free = ov["rent_free_months"]
        params.append(Param("rent_free_months", rent_free, "mois", "override", "overrides.rent_free_months"))
    else:
        inc = registry_number(registry, defaults, "market.incentives")
        assumed = inc is None
        if not assumed:
            params.append(Param("incentives", inc, "fraction", "registry", "market.incentives"))
        else:
            inc = assumption("incentives_if_missing")
        rent_free = inc * firm
        params.append(Param("rent_free_months", rent_free, "mois", "computed", "incentives x bail ferme",
                            f"{inc:.0%} x {firm} mois", fallback=assumed))

    # ---------------------------------------------------------------- hypothèses de modélisation
    void = int(assumption("letting_void_months"))
    occ = assumption("stabilised_occupancy")
    nonrec = assumption("non_recoverable_pct")
    holding = assumption("vacant_holding_cost_eur_sqm_year")
    mkt_fees = assumption("marketing_fees_pct_annual_rent")
    sale = assumption("sale_costs_pct")
    arr_fee = assumption("debt_arrangement_fee_pct")
    floor = assumption("euribor_floor")
    basis = assumption("ltv_basis")
    assumption("interest_mode")

    if missing:
        return ResolvedInputs(None, params, sorted(set(missing)))
    inputs = DealInputs(
        price=price, acq_fees_pct=acq_fees, surface_before=s_before, surface_after=s_after,
        capex_per_sqm=capex, fees_contingency_pct=fees_c,
        acquisition_date=t.acquisition_date, works_start=t.works_start, works_months=t.works_duration_months,
        exit_date=exit_date, current_leases=leases, relet_rent=relet, letting_void_months=void,
        rent_free_months=rent_free, stabilised_occupancy=occ, non_recoverable_pct=nonrec,
        vacant_holding_cost=holding, marketing_fees_pct=mkt_fees, exit_yield=exit_yield, sale_costs_pct=sale,
        ltv=ltv, ltv_basis=basis, euribor=euribor, margin_bps=margin, euribor_floor=floor,
        arrangement_fee_pct=arr_fee, firm_lease_term_months=firm)
    return ResolvedInputs(inputs, params, [])
