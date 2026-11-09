"""Schéma de validation de `window.DATA` (specification_dashboard_helios.md, section 3).

Le générateur REFUSE d'écrire le dashboard si un bloc obligatoire manque ou si un champ a un type inattendu.
Les valeurs peuvent être `null` (affichées « non disponible ») mais les blocs et les clés doivent exister.

Écarts assumés par rapport à la section 3.1 de la spécification (tous signalés dans le résumé de l'étape 1) :
  - DataPoint.status admet aussi « rejected » (donnée écartée par les contrôles de plausibilité de l'agent),
    DataPoint.method admet aussi « none » (aucune source n'a répondu) : ce sont les valeurs réelles du registre ;
  - Risk.title : nom court du risque (le bandeau 6.1 affiche « nom + niveau », la spécification n'a pas de champ nom) ;
  - recommendation.decision admet aussi « NON_DETERMINE » (l'agent refuse de trancher si une donnée exigée manque) ;
  - recommendation.rationale peut contenir des jetons {clé|format} rendus par fmt() : aucun chiffre en clair.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


Num = float | int | None

# ------------------------------------------------------------------ 3.1 meta, deal, datapoints


class Meta(_M):
    asset_name: str
    analysis_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    generated_at: str
    currency: str
    language: str


class Deal(_M):
    asset: dict[str, Any]
    operation: dict[str, Any]


class DataPoint(_M):
    key: str | None = None
    label: str | None = None             # extension : libellé lisible (fiche source, légendes)
    value: Any = None
    unit: str | None = None
    source_name: str
    source_url: str | None = None
    data_date: str | None = None
    retrieved_at: str | None = None
    method: Literal["api", "report_extraction", "computed", "assumption", "user_input", "none"]
    status: Literal["ok", "partial", "missing", "assumption", "rejected"]
    confidence: Literal["high", "medium", "low"]
    notes: str | None = None


# ------------------------------------------------------------------ market


class Transaction(_M):
    date: str | None
    price_eur: Num
    surface_sqm: Num
    price_per_sqm: Num
    lat: Num
    lon: Num
    distance_m: Num
    source_ref: str | None


class YearStat(_M):
    year: int
    median: Num
    count: int | None


class TransactionsStats(_M):
    median: Num
    q1: Num
    q3: Num
    count: int | None
    by_year: list[YearStat]


class Rents(_M):
    prime: Num
    average: Num
    relet_assumed: Num
    incentives_months: Num
    effective: Num


class Yields(_M):
    prime: Num
    exit_assumed: Num


class SupplyDemand(_M):
    vacancy_pct: Num
    future_supply_sqm: Num
    take_up_sqm: Num


class SeriesPoint(_M):
    date: str
    value: float


class Rates(_M):
    euribor3m_series: list[SeriesPoint]
    euribor3m_last: Num
    ilat_series: list[SeriesPoint]
    debt_rate_all_in: Num


class DpeComparables(_M):
    distribution: dict[Literal["A", "B", "C", "D", "E", "F", "G"], int | None]
    count: int | None


class Plu(_M):
    zone: str | None
    max_height_m: Num
    heritage_constraints: str | None
    rooftop_extension_notes: str | None


class Market(_M):
    transactions: list[Transaction]
    transactions_stats: TransactionsStats
    rents: Rents
    yields: Yields
    supply_demand: SupplyDemand
    rates: Rates
    dpe_comparables: DpeComparables
    plu: Plu


# ------------------------------------------------------------------ financials


class ValueRange(_M):
    low: Num
    high: Num


class TotalCost(_M):
    acquisition: Num
    fees: Num
    works: Num
    contingency: Num
    financing_costs: Num
    other: Num
    total: Num
    per_sqm_after: Num


class MaxPrice(_M):
    central: Num
    pessimistic: Num
    target_used: str | None


class CashflowRow(_M):
    period: str
    noi: Num
    capex: Num
    debt_draw: Num
    interest: Num
    net: Num


class Financials(_M):
    price_per_sqm_asked: Num
    price_vs_median_pct: Num
    value_range: ValueRange
    total_cost: TotalCost
    noi_stabilized: Num
    yoc_pct: Num
    exit_value: Num
    margin_eur: Num
    margin_pct: Num
    spread_bps: Num
    irr_project: Num
    irr_equity: Num
    equity_multiple: Num
    peak_equity: Num
    max_price: MaxPrice
    cashflows: list[CashflowRow]


# ------------------------------------------------------------------ sensitivity, scenarios


class TornadoRow(_M):
    variable: str
    label: str
    low_case: Num
    high_case: Num
    metric: Literal["margin_pct", "irr_equity"]
    delta_low: Num
    delta_high: Num


class Breakeven(_M):
    variable: str
    label: str
    base: Num
    breakeven: Num
    unit: str | None


class Heatmap(_M):
    id: str
    x_label: str
    y_label: str
    x_values: list[float]
    y_values: list[float]
    z: list[list[float | None]]
    metric: Literal["margin_pct", "irr_equity"]


class Sensitivity(_M):
    tornado: list[TornadoRow]
    breakeven: list[Breakeven]
    heatmaps: list[Heatmap]
    engine_params: dict[str, Any]


class Scenario(_M):
    name: str
    assumptions: dict[str, Any]
    yoc_pct: Num
    spread_bps: Num
    margin_pct: Num
    irr_equity: Num
    equity_multiple: Num
    max_price: Num


# ------------------------------------------------------------------ risks, targets, recommendation


class Risk(_M):
    id: str
    title: str                                   # extension : nom court (bandeau 6.1)
    category: str
    description: str
    evidence: list[Any]
    probability: int | None = Field(default=None, ge=1, le=5)
    probability_rationale: str
    impact: int | None = Field(default=None, ge=1, le=5)
    impact_rationale: str
    criticality: int | None = Field(default=None, ge=1, le=25)
    level: Literal["faible", "modéré", "élevé", "critique", "incertain"]
    uncertain: bool
    mitigations: list[str]
    financial_link: list[str]
    financial_effect: dict[str, Any] | None = None    # extension : effet du choc sur la marge et le TRI (lien risque -> chiffre)
    missing_information: list[str] = Field(default_factory=list)   # extension : ce qui manque pour mieux évaluer


class Targets(_M):
    min_spread_vs_exit_bps: Num
    min_levered_irr_pct: Num
    min_equity_multiple: Num
    min_margin_on_cost_pct: Num
    source: Literal["assumption", "confirmed"]


class ExplanationSection(_M):
    title: str
    paragraphs: list[str]                          # jetons {clé|format} : aucun chiffre en clair


class Recommendation(_M):
    decision: Literal["GO", "GO_AVEC_CONTRAINTES", "GO_SOUS_CONDITIONS", "NO_GO", "NON_DETERMINE"]
    rationale: str
    pros: list[str]
    cons: list[str]
    key_indicators: list[str]                    # clés de datapoints
    priority_risks: list[str]                    # identifiants de risques
    conditions: list[str]
    missing_information: list[str]
    seller_requests: list[str]
    max_price: Num
    note: list[str] = Field(default_factory=list)   # extension : note go/no-go publiée (5 à 10 lignes, jetons {clé|format})
    explanation: list[ExplanationSection] = Field(default_factory=list)   # extension : texte explicatif final


class Source(_M):
    id: str
    name: str
    url: str | None
    data_date: str | None
    used_for: list[str]


class Assumption(_M):
    key: str
    value: Any
    unit: str | None
    justification: str
    range: list[float] | None


class Missing(_M):
    key: str
    reason: str
    impact: str


# ------------------------------------------------------------------ objet complet

TOKEN = re.compile(r"\{([a-zA-Z0-9_.@#-]+)(?::([a-zA-Z0-9_]+))?(?:\|([a-z_0-9]+))?\}")
# chiffre « de valeur » écrit en clair : nombre de 2 chiffres ou plus, décimal, ou suivi d'une unité
# (un nom comme « Euribor 3 mois » reste admis)
BARE_NUMBER = re.compile(r"\d{2,}|\d[.,]\d|\d\s*(%|€|pb|pt|m²|x\b)")


class DashboardData(_M):
    meta: Meta
    deal: Deal
    datapoints: dict[str, DataPoint]
    market: Market
    financials: Financials
    sensitivity: Sensitivity
    scenarios: list[Scenario]
    risks: list[Risk]
    targets: Targets
    recommendation: Recommendation
    sources: list[Source]
    assumptions: list[Assumption]
    missing: list[Missing]

    @model_validator(mode="after")
    def _references(self) -> "DashboardData":
        """Toute référence (jeton de texte, indicateur clé, risque prioritaire) doit exister."""
        bad = []
        rec = self.recommendation
        texts = [("rationale", rec.rationale)] + [(f"{n}[{i}]", t) for n in ("pros", "cons", "conditions", "note")
                                                  for i, t in enumerate(getattr(rec, n))]
        texts += [(f"explanation[{i}].{j}", p) for i, s in enumerate(rec.explanation) for j, p in enumerate(s.paragraphs)]
        for where, text in texts:
            for m in TOKEN.finditer(text):
                if m.group(1) not in self.datapoints:
                    bad.append(f"{where} : jeton {{{m.group(1)}}} : datapoint absent")
            if BARE_NUMBER.search(TOKEN.sub("", text)):
                bad.append(f"recommendation.{where} contient un chiffre hors jeton (il doit passer par fmt)")
        bad += [f"indicateur clé {k} : datapoint absent" for k in self.recommendation.key_indicators if k not in self.datapoints]
        ids = {r.id for r in self.risks}
        bad += [f"risque prioritaire {r} absent de risks" for r in self.recommendation.priority_risks if r not in ids]
        if bad:
            raise ValueError(" ; ".join(bad))
        return self


def validate_data(data: dict) -> DashboardData:
    """Lève pydantic.ValidationError (bloc manquant, type inattendu, référence cassée)."""
    return DashboardData.model_validate(data)
