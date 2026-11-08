"""État partagé du graphe (AgentState) et objets échangés entre nœuds.

Les résultats des phases 4 à 7 (financials, risks, sensitivity, scenarios,
recommendation) sont typés `dict | None` pour l'instant : leurs schémas
Pydantic seront définis dans leurs phases respectives, à partir du code de
calcul réel, plutôt que figés ici à l'aveugle.
"""
from __future__ import annotations

import operator
from datetime import date
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from config.schema import AssetConfig, DefaultsConfig, InvestorTargets, SourcesCatalog
from tools.registry import DataRegistry, register_config_inputs

Priority = Literal["P1", "P2", "P3"]  # P1 bloquant pour la décision, P2 utile, P3 confort


class DataNeed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str                              # clé cible dans le registre, ex. "market.prime_rent"
    indicators: list[str] = Field(min_length=1)   # indicateurs du catalogue, ex. ["prime_rent", "face_rent"]
    priority: Priority
    target_sources: list[str] = Field(default_factory=list)   # ids de sources_catalog.yaml, par priorité
    min_sources: int = Field(default=1, ge=1)                  # sources à interroger pour le recoupement
    scope: str | None = None              # périmètre : rayon, sous-marché, arrondissement...
    period: str | None = None             # période de référence
    reason: str | None = None


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    check: Literal["completeness", "freshness", "coherence", "plausibility", "convergence", "sample_size"]
    severity: Literal["info", "warning", "blocking"]
    message: str


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iteration: int = 0
    issues: list[ValidationIssue] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)          # clés P1 manquantes ou suspectes
    assumptions_applied: list[str] = Field(default_factory=list)
    verdict: Literal["sufficient", "research_again", "proceed_with_assumptions"] | None = None

    def blocking(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "blocking"]


class AgentState(TypedDict):
    config: AssetConfig
    defaults: DefaultsConfig
    targets: InvestorTargets
    catalog: SourcesCatalog
    analysis_date: str                    # générée à l'exécution, jamais saisie
    data_needs: list[DataNeed]
    registry: DataRegistry
    research_iteration: int               # nombre de passages dans research_market_data
    research_attempts: dict[str, list[str]]   # clé -> sources déjà essayées
    validation_report: ValidationReport | None
    financials: dict[str, Any] | None
    risks: dict[str, Any] | None          # {risks, matrix, seller_requests, missing_information, ...}
    sensitivity: dict[str, Any] | None
    scenarios: dict[str, Any] | None
    recommendation: dict[str, Any] | None
    # journal pour le paragraphe « méthode » ; chaque nœud renvoie SES lignes, LangGraph les concatène
    # (indispensable : financial_analysis et risk_analysis écrivent en parallèle)
    logs: Annotated[list[str], operator.add]


def build_initial_state(config: AssetConfig, defaults: DefaultsConfig, targets: InvestorTargets,
                        catalog: SourcesCatalog, analysis_date: date | None = None) -> AgentState:
    registry = DataRegistry()
    register_config_inputs(config, registry)
    logs = [f"config chargée : {config.asset.name} ({len(registry)} données du deal enregistrées)"]
    logs += [f"avertissement config : {w}" for w in config.consistency_warnings()]
    nulls = config.null_fields()
    if nulls:
        logs.append(f"champs null à rechercher : {', '.join(nulls)}")
    return AgentState(
        config=config, defaults=defaults, targets=targets, catalog=catalog,
        analysis_date=(analysis_date or date.today()).isoformat(),
        data_needs=[], registry=registry, research_iteration=0, research_attempts={}, validation_report=None,
        financials=None, risks=None, sensitivity=None, scenarios=None, recommendation=None,
        logs=logs,
    )
