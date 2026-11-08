"""Modèle de risque, criticité = probabilité x impact, niveaux et matrice 5 x 5 (plan, section 6).

Règle d'or : une note n'existe que si elle cite ses preuves et la règle appliquée. Sinon le risque est
« incertain », avec la liste des informations manquantes. Aucune note n'est choisie librement.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal["marché_locatif", "travaux", "financier", "réglementaire", "environnemental"]
Level = Literal["faible", "modéré", "élevé", "critique", "incertain"]


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str                         # clé du registre, chemin de la config, ou « finance.<indicateur> »
    label: str
    value: Any
    source: str | None = None
    data_date: str | None = None


class Risk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: Category
    title: str
    description: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    probability: int | None = Field(default=None, ge=1, le=5)
    probability_rationale: str = ""
    impact: int | None = Field(default=None, ge=1, le=5)
    impact_rationale: str = ""
    financial_effect: dict[str, Any] | None = None     # TRI et marge avant / après choc
    criticality: int | None = None
    level: Level = "incertain"
    uncertain: bool = True
    missing_information: list[str] = Field(default_factory=list)
    mitigations: list[str] = Field(default_factory=list)
    seller_requests: list[str] = Field(default_factory=list)
    financial_link: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _justified(self) -> "Risk":
        """Contrôle automatique du plan (6.3) : pas de note sans preuve ni règle."""
        if self.probability is not None and (not self.evidence or not self.probability_rationale.strip()):
            raise ValueError(f"{self.id} : probabilité sans preuve ou sans règle explicite")
        if self.impact is not None and (not self.financial_effect or not self.impact_rationale.strip()):
            raise ValueError(f"{self.id} : impact sans effet financier chiffré ou sans règle")
        complete = self.probability is not None and self.impact is not None
        if complete != (not self.uncertain):
            raise ValueError(f"{self.id} : 'uncertain' incohérent avec les notes")
        if self.uncertain and not self.missing_information:
            raise ValueError(f"{self.id} : un risque incertain doit lister les informations manquantes")
        if not self.mitigations:
            raise ValueError(f"{self.id} : au moins une mesure de réduction attendue")
        return self


def level_for(criticality: int, levels: dict[str, tuple[int, int]]) -> Level:
    for name, (lo, hi) in levels.items():
        if lo <= criticality <= hi:
            return name   # type: ignore[return-value]
    raise ValueError(f"criticité {criticality} hors des niveaux définis")


def finalise(risk: Risk, levels: dict[str, tuple[int, int]]) -> Risk:
    """Calcule criticité et niveau quand les deux notes existent ; sinon le risque reste incertain."""
    data = risk.model_dump()
    if risk.probability is not None and risk.impact is not None:
        c = risk.probability * risk.impact
        data.update(criticality=c, level=level_for(c, levels), uncertain=False)
    else:
        data.update(criticality=None, level="incertain", uncertain=True)
    return Risk.model_validate(data)


def matrix(risks: list[Risk]) -> list[list[list[str]]]:
    """Matrice 5 x 5 : matrix[impact-1][probabilité-1] = identifiants des risques de la case."""
    grid: list[list[list[str]]] = [[[] for _ in range(5)] for _ in range(5)]
    for r in risks:
        if r.probability is not None and r.impact is not None:
            grid[r.impact - 1][r.probability - 1].append(r.id)
    return grid


def impact_from_effect(base_irr: float | None, shocked_irr: float | None, base_margin: float, shocked_margin: float,
                       bands: tuple[float, float, float, float]) -> tuple[int, str]:
    """Impact 5 si la marge DEVIENT négative sous l'effet du choc (plan 6.2). Sinon, selon la plus forte
    dégradation, en points, du TRI fonds propres ou de la marge sur coût. La marge sert de garde-fou quand le
    TRI est trompeur : pour une opération déjà déficitaire, un retard rend le TRI moins négatif tout en
    aggravant la perte."""
    if base_margin >= 0 > shocked_margin:
        return 5, f"la marge devient négative ({base_margin:.1%} -> {shocked_margin:.1%}) : impact 5"
    irr_drop = None if base_irr is None or shocked_irr is None else (base_irr - shocked_irr) * 100
    margin_drop = (base_margin - shocked_margin) * 100
    # arrondi : -25 % -> -30 % donne 4,9999999 pt en virgule flottante, qui doit compter pour 5
    drop = round(max(margin_drop, irr_drop if irr_drop is not None else margin_drop), 9)
    impact = 1 + sum(drop >= b for b in bands)
    which = "TRI fonds propres" if irr_drop is not None and irr_drop >= margin_drop else "marge sur coût"
    return impact, (f"dégradation de {drop:.1f} pt ({which} ; TRI {'n.d.' if irr_drop is None else f'{-irr_drop:+.1f}'} pt, "
                    f"marge {-margin_drop:+.1f} pt ; seuils {list(bands)} pt) : impact {impact}")
