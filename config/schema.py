"""Schémas Pydantic de la configuration et chargeurs (composant déterministe, hors agent).

- AssetConfig : tout ce qui est propre au deal (asset_config.json).
- DefaultsConfig : paramètres de recherche et seuils techniques (defaults.yaml).
- InvestorTargets : objectifs du fonds (investor_targets.yaml).
- SourcesCatalog : description des sources (sources_catalog.yaml).

`load_config()` est appelé AVANT le lancement du graphe : une config invalide
lève une ConfigError lisible et arrête tout avant le moindre appel d'API.

Convention d'unités : tout champ suffixé `_pct` est une FRACTION (0.15 = 15 %).
"""
from __future__ import annotations

import calendar
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

DPE_CLASSES = ("A", "B", "C", "D", "E", "F", "G")


class ConfigError(ValueError):
    """Erreur de configuration lisible (une ligne par problème)."""


class _Strict(BaseModel):
    # extra="forbid" : une faute de frappe dans un nom de champ est une erreur, pas un champ ignoré.
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- asset_config.json

class Meta(_Strict):
    source_document: str
    source_date: str | None = None
    notes: str | None = None


class ReferencePoint(_Strict):
    label: str
    address: str = Field(min_length=5)


class Asset(_Strict):
    name: str = Field(min_length=1)
    type: str
    district: str | None = None
    sector: str | None = None
    submarket: str | None = None
    # Pas d'adresse d'actif : l'analyse porte sur le secteur, pas sur l'immeuble.
    # Toutes les requêtes géolocalisées partent de ce point de référence (obligatoire).
    reference_point: ReferencePoint
    surface_before_sqm: float = Field(gt=0)
    surface_after_sqm: float = Field(gt=0)
    condition: str | None = None
    dpe_current: Literal["A", "B", "C", "D", "E", "F", "G"] | None = None
    dpe_target: Literal["A", "B", "C", "D", "E", "F", "G"] | None = None
    year_built_decade: int | None = Field(default=None, ge=1800, le=2100)
    floors: str | None = None

    @field_validator("year_built_decade")
    @classmethod
    def _decade(cls, v: int | None) -> int | None:
        if v is not None and v % 10 != 0:
            raise ValueError("doit être une décennie (ex. 1970)")
        return v


class Lease(_Strict):
    tenant: str
    surface_sqm: float = Field(gt=0)
    rent_eur_sqm_year: float = Field(gt=0)
    rent_basis: str | None = None
    end_date: date


class Financing(_Strict):
    ltv: float = Field(ge=0, le=1)
    reference_rate: Literal["Euribor3M"]
    margin_bps: float = Field(ge=0, le=2000)


class Timeline(_Strict):
    acquisition_date: date
    works_start: date
    works_duration_months: int = Field(gt=0, le=120)
    letting_year: int = Field(ge=2000, le=2100)
    exit_year: int = Field(ge=2000, le=2100)


class Operation(_Strict):
    asking_price_eur: float = Field(gt=0)
    price_basis: str | None = None
    strategy: str
    capex_eur_per_sqm_after: float = Field(ge=0)
    fees_and_contingency_pct_of_capex: float = Field(ge=0, le=1)
    project_scope: str | None = None
    current_leases: list[Lease] = Field(default_factory=list)
    occupancy_current_pct: float | None = Field(default=None, ge=0, le=1)
    financing: Financing
    timeline: Timeline


# Valeurs que l'utilisateur peut imposer à la place de l'estimation de l'agent.
# Elles sont enregistrées avec method="user_input". "fraction" => dans [0, 1].
OVERRIDABLE: dict[str, Literal["fraction", "positive", "non_negative"]] = {
    "exit_yield_pct": "fraction",
    "acquisition_fees_pct": "fraction",
    "sale_costs_pct": "fraction",
    "stabilised_occupancy_pct": "fraction",
    "non_recoverable_costs_pct": "fraction",
    "euribor_3m_pct": "fraction",
    "relet_rent_eur_sqm_year": "positive",
    "rent_free_months": "non_negative",
    "letting_period_months": "non_negative",
}


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


class AssetConfig(_Strict):
    meta: Meta
    asset: Asset
    operation: Operation
    overrides: dict[str, float] = Field(default_factory=dict)

    @field_validator("overrides")
    @classmethod
    def _check_overrides(cls, v: dict[str, float]) -> dict[str, float]:
        for key, val in v.items():
            if key not in OVERRIDABLE:
                raise ValueError(f"clé inconnue '{key}' (autorisées : {sorted(OVERRIDABLE)})")
            kind = OVERRIDABLE[key]
            if kind == "fraction" and not 0 <= val <= 1:
                raise ValueError(f"'{key}'={val} doit être une fraction dans [0, 1] (ex. 0.045 pour 4,5 %)")
            if kind == "positive" and val <= 0:
                raise ValueError(f"'{key}'={val} doit être > 0")
            if kind == "non_negative" and val < 0:
                raise ValueError(f"'{key}'={val} doit être >= 0")
        return v

    @model_validator(mode="after")
    def _coherence(self) -> "AssetConfig":
        a, op, t = self.asset, self.operation, self.operation.timeline
        errors: list[str] = []
        if a.surface_after_sqm < a.surface_before_sqm:
            errors.append(
                f"surface_after_sqm ({a.surface_after_sqm}) < surface_before_sqm ({a.surface_before_sqm})"
            )
        if t.works_start < t.acquisition_date:
            errors.append(f"works_start ({t.works_start}) antérieur à acquisition_date ({t.acquisition_date})")
        if t.letting_year < t.acquisition_date.year:
            errors.append(f"letting_year ({t.letting_year}) antérieur à l'acquisition ({t.acquisition_date.year})")
        if self.works_end > date(t.letting_year, 12, 31):
            errors.append(
                f"fin des travaux ({self.works_end}) postérieure à l'année de relocation ({t.letting_year})"
            )
        if t.exit_year < t.letting_year:
            errors.append(f"exit_year ({t.exit_year}) antérieur à letting_year ({t.letting_year})")
        leased = sum(l.surface_sqm for l in op.current_leases)
        if leased > a.surface_before_sqm:
            errors.append(f"surface louée ({leased}) > surface_before_sqm ({a.surface_before_sqm})")
        if errors:
            raise ValueError("incohérences : " + " ; ".join(errors))
        return self

    # ---------------------------------------------------------------- dérivés (pas des données)
    @property
    def works_end(self) -> date:
        t = self.operation.timeline
        return add_months(t.works_start, t.works_duration_months)

    def null_fields(self) -> list[str]:
        """Champs laissés à null : marqués « à rechercher » pour identify_data_needs."""
        out: list[str] = []

        def walk(obj: Any, prefix: str) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    walk(v, f"{prefix}.{k}" if prefix else k)
            elif obj is None:
                out.append(prefix)

        walk(self.model_dump(mode="json"), "")
        return out

    def consistency_warnings(self) -> list[str]:
        """Incohérences non bloquantes, remontées dans les logs."""
        a, op = self.asset, self.operation
        w: list[str] = []
        leased = sum(l.surface_sqm for l in op.current_leases)
        if op.occupancy_current_pct is not None:
            implied = leased / a.surface_before_sqm
            if abs(implied - op.occupancy_current_pct) > 0.02:
                w.append(
                    f"occupancy_current_pct={op.occupancy_current_pct:.2f} mais surface louée / surface = {implied:.2f}"
                )
        for l in op.current_leases:
            if l.end_date > op.timeline.works_start:
                w.append(f"bail '{l.tenant}' se termine ({l.end_date}) après le début des travaux ({op.timeline.works_start})")
        if a.dpe_current and a.dpe_target and DPE_CLASSES.index(a.dpe_target) > DPE_CLASSES.index(a.dpe_current):
            w.append(f"DPE cible {a.dpe_target} moins bon que le DPE actuel {a.dpe_current}")
        return w


# --------------------------------------------------------------------------- YAML de paramétrage

class ResearchDefaults(_Strict):
    radius_m: list[int] = Field(min_length=1)
    transactions_from: date
    transactions_to: date | Literal["today"]
    max_research_loops: int = Field(ge=0, le=5)
    parallel_workers: int = Field(default=4, ge=1, le=32)

    @field_validator("radius_m")
    @classmethod
    def _radii(cls, v: list[int]) -> list[int]:
        if any(r <= 0 for r in v) or v != sorted(v):
            raise ValueError("rayons > 0 et croissants")
        return v


class QualityChecks(_Strict):
    dvf_outlier_percentiles: tuple[float, float]
    source_divergence_alert_pct: float = Field(gt=0, le=1)
    geocoding_min_score: float = Field(ge=0, le=1)


class ShockChange(_Strict):
    field: str
    op: Literal["mult", "add", "set", "set_field", "add_months"]
    value: float | int | str


class ParamChange(_Strict):
    """Changement paramétré : la valeur est fournie au moment de la recherche (seuils, grilles)."""
    field: str
    op: Literal["mult", "add", "add_months"]


class SensitivityVariable(_Strict):
    label: str
    optimistic: list[ShockChange] = Field(min_length=1)
    pessimistic: list[ShockChange] = Field(min_length=1)


class BreakevenSpec(_Strict):
    label: str
    changes: list[ParamChange] = Field(min_length=1)
    bounds: tuple[float, float]
    integer: bool = False


class GridAxis(_Strict):
    label: str
    changes: list[ParamChange] = Field(min_length=1)
    values: list[float] = Field(min_length=2)


class HeatmapSpec(_Strict):
    x: GridAxis
    y: GridAxis


class SensitivityConfig(_Strict):
    variables: dict[str, SensitivityVariable]
    breakeven: dict[str, BreakevenSpec]
    heatmaps: dict[str, HeatmapSpec]


class ScenarioSpec(_Strict):
    label: str
    variables: dict[str, Literal["optimistic", "pessimistic"]] = Field(default_factory=dict)
    rationale: str


class Shock(_Strict):
    changes: list[ShockChange] = Field(min_length=1)
    rationale: str = Field(min_length=10)


class ProbabilityThresholds(_Strict):
    months_of_supply_bands: tuple[float, float, float, float]
    vacancy_high: float
    supply_growth_high: float
    bt01_inflation_high: float
    protected_buildings_many: int
    dpe_gap_heavy: int = Field(ge=1, le=6)
    dpe_low_share_b_or_better: float
    investment_price_drop: float
    asbestos_ban_year: int


class RiskScoring(_Strict):
    criticality_levels: dict[str, tuple[int, int]]
    impact_irr_drop_pts: tuple[float, float, float, float]
    probability: ProbabilityThresholds
    shocks: dict[str, Shock]

    @field_validator("impact_irr_drop_pts", "probability")
    @classmethod
    def _ascending(cls, v):
        bands = v if isinstance(v, tuple) else v.months_of_supply_bands
        if list(bands) != sorted(bands):
            raise ValueError("seuils croissants attendus")
        return v

    @field_validator("criticality_levels")
    @classmethod
    def _cover_1_25(cls, v: dict[str, tuple[int, int]]) -> dict[str, tuple[int, int]]:
        covered = sorted(n for lo, hi in v.values() for n in range(lo, hi + 1))
        if covered != list(range(1, 26)):
            raise ValueError("les niveaux de criticité doivent couvrir 1..25 sans trou ni chevauchement")
        return v


class Fallback(_Strict):
    """Hypothèse de repli : utilisée seulement si la recherche échoue, enregistrée status=assumption."""
    value: float
    unit: str
    range: tuple[float, float]
    confidence: Literal["medium", "low"] = "low"
    rationale: str = Field(min_length=10)

    @model_validator(mode="after")
    def _in_range(self) -> "Fallback":
        lo, hi = self.range
        if not lo <= self.value <= hi:
            raise ValueError(f"valeur de repli {self.value} hors de sa fourchette {self.range}")
        return self


class NeedSpec(_Strict):
    key: str
    indicators: list[str] = Field(min_length=1)
    priority: Literal["P1", "P2", "P3"]
    scope: Literal["reference_point", "radius", "district", "submarket", "national"]
    reason: str
    max_age_months: int | None = Field(default=None, gt=0)
    plausible_range: tuple[float, float] | None = None
    value_field: str | None = None
    min_sources: int = Field(default=1, ge=1)
    min_sample: int | None = Field(default=None, gt=0)
    sample_size_field: str | None = None
    override: str | None = None
    fallback: Fallback | None = None

    @model_validator(mode="after")
    def _checks(self) -> "NeedSpec":
        if self.override is not None and self.override not in OVERRIDABLE:
            raise ValueError(f"override '{self.override}' inconnu (autorisés : {sorted(OVERRIDABLE)})")
        if (self.min_sample is None) != (self.sample_size_field is None):
            raise ValueError("min_sample et sample_size_field vont ensemble")
        if self.plausible_range and self.plausible_range[0] > self.plausible_range[1]:
            raise ValueError("plausible_range : borne basse > borne haute")
        if self.fallback and self.key.startswith("market."):
            raise ValueError("pas d'hypothèse de repli pour une donnée de marché : elle doit être recherchée")
        return self


class CoherenceRule(_Strict):
    greater_or_equal: str
    than: str
    message: str


class FinancialAssumption(_Strict):
    value: float | str
    unit: str | None = None
    range: tuple[float, float] | None = None
    override: str | None = None
    rationale: str = Field(min_length=10)

    @model_validator(mode="after")
    def _checks(self) -> "FinancialAssumption":
        if self.override is not None and self.override not in OVERRIDABLE:
            raise ValueError(f"override '{self.override}' inconnu")
        if self.range and isinstance(self.value, (int, float)) and not self.range[0] <= self.value <= self.range[1]:
            raise ValueError(f"valeur {self.value} hors de sa fourchette {self.range}")
        return self


REQUIRED_ASSUMPTIONS = {
    "relet_rent_position", "relet_rent_fallback", "exit_yield_fallback", "exit_yield_premium_bps", "letting_void_months", "firm_lease_term_months",
    "incentives_if_missing", "stabilised_occupancy", "non_recoverable_pct", "vacant_holding_cost_eur_sqm_year",
    "marketing_fees_pct_annual_rent", "sale_costs_pct", "debt_arrangement_fee_pct", "euribor_floor",
    "ltv_basis", "interest_mode", "exit_quarter",
}


class DvfFilters(_Strict):
    property_type_codes: list[str] = Field(min_length=1)
    sale_natures: list[str] = Field(min_length=1)
    exclude_vefa: bool = True
    large_lot_min_sqm: float = Field(gt=0)
    max_bbox_deg: float = Field(gt=0, le=0.1)


class CommitteeQuestion(_Strict):
    node: Literal["financial_analysis", "risk_analysis", "sensitivity_analysis", "scenario_analysis"]
    text: str


class DecisionRules(_Strict):
    required_data: list[str] = Field(default_factory=list)
    max_negotiable_discount: float = Field(ge=0, le=1)
    max_critical_risks_conditional: int = Field(ge=0)
    robustness_scenario: str
    top_risks: int = Field(ge=1, le=10)
    max_arguments: int = Field(ge=3, le=10)


class LlmConfig(_Strict):
    provider: Literal["openai"]
    model: str
    temperature: float = Field(ge=0, le=2)
    api_key_env: str
    prompt_file: str
    node_prompts: dict[str, str] = Field(default_factory=dict)
    number_tolerance_rel: float = Field(gt=0, le=0.05)


class DefaultsConfig(_Strict):
    research: ResearchDefaults
    quality_checks: QualityChecks
    data_needs: list[NeedSpec] = Field(min_length=1)
    coherence_rules: list[CoherenceRule] = Field(default_factory=list)
    submarket_zones: dict[str, dict[str, str]] = Field(default_factory=dict)
    dvf: DvfFilters | None = None
    financial_assumptions: dict[str, FinancialAssumption] = Field(default_factory=dict)
    sensitivity: SensitivityConfig | None = None
    scenarios: dict[str, ScenarioSpec] = Field(default_factory=dict)
    committee_questions: dict[str, CommitteeQuestion] = Field(default_factory=dict)
    decision_rules: DecisionRules | None = None
    llm: LlmConfig | None = None
    risk_scoring: RiskScoring

    @model_validator(mode="after")
    def _unique_keys(self) -> "DefaultsConfig":
        keys = [n.key for n in self.data_needs]
        dup = sorted({k for k in keys if keys.count(k) > 1})
        if dup:
            raise ValueError(f"besoins de données dupliqués : {dup}")
        fa = self.financial_assumptions
        missing = REQUIRED_ASSUMPTIONS - fa.keys()
        if missing:
            raise ValueError(f"hypothèses de modélisation manquantes : {sorted(missing)}")
        if fa["ltv_basis"].value not in ("total_cost", "price"):
            raise ValueError("ltv_basis : 'total_cost' ou 'price'")
        if fa["interest_mode"].value != "capitalised":
            raise ValueError("interest_mode : seul 'capitalised' est modélisé")
        if fa["exit_quarter"].value not in (1, 2, 3, 4):
            raise ValueError("exit_quarter : 1 à 4")
        if self.decision_rules and self.decision_rules.robustness_scenario not in self.scenarios:
            raise ValueError(f"decision_rules.robustness_scenario inconnu : {self.decision_rules.robustness_scenario}")
        if self.sensitivity:
            for name, sc in self.scenarios.items():
                unknown = set(sc.variables) - set(self.sensitivity.variables)
                if unknown:
                    raise ValueError(f"scénario {name} : variables inconnues {sorted(unknown)}")
            if self.scenarios and "central" not in self.scenarios:
                raise ValueError("un scénario « central » est obligatoire")
        for r in self.coherence_rules:
            for k in (r.greater_or_equal, r.than):
                if k not in keys:
                    raise ValueError(f"règle de cohérence sur une clé inconnue : {k}")
        return self

    def need(self, key: str) -> NeedSpec:
        for n in self.data_needs:
            if n.key == key:
                return n
        raise KeyError(key)


class Target(_Strict):
    value: float
    unit: str
    status: Literal["assumption", "confirmed"]
    rationale: str = Field(min_length=1)


class InvestorTargets(_Strict):
    fund: str
    strategy: str | None = None
    targets: dict[str, Target]

    @field_validator("targets")
    @classmethod
    def _required(cls, v: dict[str, Target]) -> dict[str, Target]:
        required = {"levered_irr_min", "equity_multiple_min", "margin_on_cost_min", "yoc_spread_min_bps"}
        missing = required - v.keys()
        if missing:
            raise ValueError(f"objectifs manquants : {sorted(missing)}")
        return v


class Source(_Strict):
    id: str
    name: str
    type: Literal["api", "file", "report"]
    url: str | None = None
    doc_url: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    indicators: list[str] = Field(min_length=1)
    priority: Literal["very_high", "high", "medium", "low"]
    verified: bool = False
    verified_on: str | None = None      # date de la vraie réponse qui a servi à écrire le parsing
    expect_title_contains: str | None = None   # garde-fou : la série renvoyée doit porter ce titre
    limits: str | None = None


class SourcesCatalog(_Strict):
    sources: list[Source]

    @field_validator("sources")
    @classmethod
    def _unique_ids(cls, v: list[Source]) -> list[Source]:
        ids = [s.id for s in v]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"identifiants de source dupliqués : {sorted(dup)}")
        return v

    def by_id(self, source_id: str) -> Source:
        for s in self.sources:
            if s.id == source_id:
                return s
        raise KeyError(source_id)

    def for_indicator(self, indicator: str) -> list[Source]:
        order = {"very_high": 0, "high": 1, "medium": 2, "low": 3}
        return sorted((s for s in self.sources if indicator in s.indicators), key=lambda s: order[s.priority])


# --------------------------------------------------------------------------- chargeurs

def _format_validation_error(path: Path, err: ValidationError) -> str:
    lines = [f"Configuration invalide : {path}"]
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "<racine>"
        msg = e["msg"].removeprefix("Value error, ")
        lines.append(f"  - {loc} : {msg}")
    return "\n".join(lines)


def _read(path: Path, kind: Literal["json", "yaml"]) -> Any:
    if not path.is_file():
        raise ConfigError(f"Fichier introuvable : {path}")
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text) if kind == "json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"Fichier illisible ({kind}) : {path}\n  - {exc}") from exc


def _load(path: Path, kind: Literal["json", "yaml"], model: type[BaseModel]) -> Any:
    data = _read(path, kind)
    try:
        return model.model_validate(data)
    except ValidationError as err:
        raise ConfigError(_format_validation_error(path, err)) from None


def check_needs_against_catalog(defaults: DefaultsConfig, catalog: SourcesCatalog) -> list[str]:
    """Contrôle croisé (non bloquant) : besoins dont aucun indicateur n'a de source au catalogue.
    Un tel besoin finira 'missing' ou en hypothèse de repli : on le signale dès le démarrage."""
    out = []
    for n in defaults.data_needs:
        if not any(catalog.for_indicator(i) for i in n.indicators):
            tail = "hypothèse de repli prévue" if n.fallback else "restera manquant"
            out.append(f"{n.key} : aucune source au catalogue pour {n.indicators} ({tail})")
    return out


def load_config(path: str | Path = PROJECT_ROOT / "asset_config.json") -> AssetConfig:
    return _load(Path(path), "json", AssetConfig)


def load_defaults(path: str | Path = CONFIG_DIR / "defaults.yaml") -> DefaultsConfig:
    return _load(Path(path), "yaml", DefaultsConfig)


def load_investor_targets(path: str | Path = CONFIG_DIR / "investor_targets.yaml") -> InvestorTargets:
    return _load(Path(path), "yaml", InvestorTargets)


def load_sources_catalog(path: str | Path = CONFIG_DIR / "sources_catalog.yaml") -> SourcesCatalog:
    return _load(Path(path), "yaml", SourcesCatalog)
