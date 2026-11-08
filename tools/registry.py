"""DataPoint et DataRegistry : cœur de la traçabilité.

Règles imposées par le code (pas seulement par convention) :
- status="missing"     -> value doit être None, confidence="low".
- status="ok"/"partial"-> value obligatoire.
- status="assumption"  -> value obligatoire ET justification dans `notes`.
- method="assumption"  -> status="assumption".
- method api / report_extraction avec status="ok" -> data_date obligatoire
  (chaque chiffre doit afficher sa source ET la date de la donnée).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# "none" : aucune collecte n'a pu être tentée (pas de source au catalogue, connecteur absent)
Method = Literal["api", "report_extraction", "computed", "assumption", "user_input", "none"]
# "rejected" : valeur reçue mais écartée par validate_research (ex. hors plage plausible) ;
# conservée pour la traçabilité, JAMAIS utilisée dans les calculs.
Status = Literal["ok", "partial", "missing", "assumption", "rejected"]
Confidence = Literal["high", "medium", "low"]

KEY_PATTERN = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DataPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str                                     # ex. "market.prime_rent_qca"
    value: float | int | str | bool | dict | list | None
    unit: str | None = None                      # "€/m²/an", "fraction", "m²"...
    source_name: str = Field(min_length=1)
    source_url: str | None = None
    data_date: str | None = None                 # date de la donnée (pas de la requête)
    retrieved_at: str = Field(default_factory=_now)
    method: Method
    status: Status
    confidence: Confidence
    notes: str | None = None                     # limites, filtres, justification d'hypothèse

    @field_validator("key")
    @classmethod
    def _key_format(cls, v: str) -> str:
        if not KEY_PATTERN.match(v):
            raise ValueError(f"clé '{v}' invalide : format 'domaine.indicateur' en minuscules (ex. market.prime_rent_qca)")
        return v

    @model_validator(mode="after")
    def _invariants(self) -> "DataPoint":
        if self.status == "missing":
            if self.value is not None:
                raise ValueError("status='missing' impose value=None (ne jamais inventer de valeur)")
            if self.confidence != "low":
                raise ValueError("status='missing' impose confidence='low'")
        else:
            if self.value is None:
                raise ValueError(f"status='{self.status}' impose une valeur ; sinon utiliser status='missing'")
        if self.status == "rejected" and not (self.notes and "REJETÉE" in self.notes):
            raise ValueError("une valeur rejetée doit dire pourquoi dans 'notes' (préfixe REJETÉE)")
        if self.method == "none" and self.status != "missing":
            raise ValueError("method='none' impose status='missing'")
        if self.method == "assumption" and self.status != "assumption":
            raise ValueError("method='assumption' impose status='assumption'")
        if self.status == "assumption" and not (self.notes and self.notes.strip()):
            raise ValueError("une hypothèse doit être justifiée dans 'notes'")
        if self.status == "ok" and self.method in ("api", "report_extraction") and not self.data_date:
            raise ValueError("donnée sourcée sans data_date : utiliser status='partial' ou renseigner la date")
        return self

    @classmethod
    def missing(cls, key: str, source_name: str, notes: str, unit: str | None = None,
                source_url: str | None = None, method: Method = "api") -> "DataPoint":
        """Raccourci pour une donnée introuvable (échec d'API, champ absent...)."""
        return cls(key=key, value=None, unit=unit, source_name=source_name, source_url=source_url,
                   method=method, status="missing", confidence="low", notes=notes)


class DataRegistry:
    """Stockage des DataPoint. Une clé peut porter plusieurs sources :
    le premier élément est la valeur retenue (primaire), les suivants sont
    des sources alternatives conservées pour la vérification croisée."""

    def __init__(self) -> None:
        self._points: dict[str, list[DataPoint]] = {}

    # ------------------------------------------------------------- écriture
    def set(self, dp: DataPoint) -> None:
        """Définit la valeur retenue pour dp.key (remplace les sources existantes)."""
        self._points[dp.key] = [dp]

    def add(self, dp: DataPoint) -> None:
        """Ajoute une source alternative (devient primaire si la clé est vide)."""
        self._points.setdefault(dp.key, []).append(dp)

    def merge(self, dp: DataPoint) -> Literal["set", "replaced_missing", "alternative", "ignored_missing"]:
        """Intègre un résultat de recherche sans jamais dégrader l'existant :
        - clé absente                      -> devient la valeur retenue ;
        - existant 'missing', nouveau trouvé -> le nouveau remplace le manque ;
        - existant trouvé, nouveau trouvé  -> source alternative (vérification croisée) ;
        - nouveau 'missing'                -> ignoré si une valeur existe déjà (sinon remplace le manque)."""
        current = self.get(dp.key)
        if current is None:
            self.set(dp)
            return "set"
        if current.status == "missing":
            self.set(dp)
            return "replaced_missing" if dp.status != "missing" else "set"
        if current.status == "rejected":          # une nouvelle valeur passe devant la valeur rejetée
            if dp.status == "missing":
                return "ignored_missing"
            self._points[dp.key].insert(0, dp)
            return "replaced_missing"
        if dp.status == "missing":
            return "ignored_missing"
        self.add(dp)
        return "alternative"

    def promote(self, dp: DataPoint) -> None:
        """Fait d'une source alternative existante la valeur retenue (ex. primaire aberrante écartée)."""
        pts = self._points.get(dp.key, [])
        if dp not in pts:
            raise ValueError(f"{dp.key} : la source à promouvoir n'est pas dans le registre")
        pts.remove(dp)
        pts.insert(0, dp)

    def rank_sources(self, key: str, source_priority: list[str]) -> None:
        """Réordonne les sources d'une clé : utilisables d'abord, par priorité (liste de noms de sources),
        puis rejetées, puis manquantes. Indépendant de l'ordre d'arrivée des réponses."""
        status_rank = {"ok": 0, "partial": 0, "assumption": 1, "rejected": 2, "missing": 3}
        pts = self._points.get(key)
        if not pts:
            return
        pos = {name: i for i, name in enumerate(source_priority)}
        pts.sort(key=lambda p: (status_rank[p.status], pos.get(p.source_name, len(pos))))

    def found(self, key: str) -> list[DataPoint]:
        """Toutes les sources utilisables d'une clé (ni manquantes, ni rejetées)."""
        return [p for p in self.get_all(key) if p.status not in ("missing", "rejected")]

    # ------------------------------------------------------------- lecture
    def get(self, key: str) -> DataPoint | None:
        pts = self._points.get(key)
        return pts[0] if pts else None

    def get_all(self, key: str) -> list[DataPoint]:
        return list(self._points.get(key, []))

    def value(self, key: str) -> Any:
        """Valeur exploitable ; lève KeyError si absente, manquante ou rejetée (pas de défaut silencieux)."""
        dp = self.get(key)
        if dp is None or dp.status in ("missing", "rejected"):
            raise KeyError(f"donnée indisponible : {key}" + (f" ({dp.notes})" if dp is not None else ""))
        return dp.value

    def reject(self, dp: DataPoint, reason: str) -> DataPoint:
        """Écarte une valeur présente : elle reste dans le registre (traçabilité) mais ne sera plus utilisée."""
        pts = self._points.get(dp.key, [])
        if dp not in pts:
            raise ValueError(f"{dp.key} : valeur à rejeter absente du registre")
        rejected = dp.model_copy(update={"status": "rejected", "confidence": "low",
                                         "notes": f"REJETÉE : {reason}" + (f" | {dp.notes}" if dp.notes else "")})
        pts[pts.index(dp)] = rejected
        # la valeur retenue devient la première source encore utilisable, sinon la valeur rejetée
        pts.sort(key=lambda p: p.status == "rejected")
        return rejected

    def rejected(self) -> list[DataPoint]:
        return [p for pts in self._points.values() for p in pts if p.status == "rejected"]

    def keys(self) -> list[str]:
        return sorted(self._points)

    def missing(self) -> list[DataPoint]:
        return [pts[0] for k, pts in sorted(self._points.items()) if pts[0].status == "missing"]

    def assumptions(self) -> list[DataPoint]:
        return [pts[0] for k, pts in sorted(self._points.items()) if pts[0].status == "assumption"]

    def __contains__(self, key: object) -> bool:
        return key in self._points

    def __len__(self) -> int:
        return len(self._points)

    # ------------------------------------------------------------- persistance
    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {k: [dp.model_dump(mode="json") for dp in pts] for k, pts in sorted(self._points.items())}

    def export_json(self, path: str | Path | None = None) -> dict[str, list[dict[str, Any]]]:
        data = self.to_dict()
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data

    @classmethod
    def from_dict(cls, data: dict[str, list[dict[str, Any]]]) -> "DataRegistry":
        reg = cls()
        for key, pts in data.items():
            for raw in pts:
                dp = DataPoint.model_validate(raw)
                if dp.key != key:
                    raise ValueError(f"clé incohérente : {key} vs {dp.key}")
                reg.add(dp)
        return reg

    @classmethod
    def load_json(cls, path: str | Path) -> "DataRegistry":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def register_config_inputs(config: Any, registry: DataRegistry) -> None:
    """Enregistre les données du deal (asset_config.json) comme DataPoint user_input,
    sourcés au document d'origine, et les overrides comme valeurs imposées.
    Ainsi les données du case sont tracées comme n'importe quelle autre donnée."""
    src, src_date = config.meta.source_document, config.meta.source_date
    a, op = config.asset, config.operation

    def put(key: str, value: Any, unit: str | None, notes: str | None = None) -> None:
        if value is None:
            return  # champ null : à rechercher, pas une donnée
        registry.set(DataPoint(key=key, value=value, unit=unit, source_name=src, data_date=src_date,
                               method="user_input", status="ok", confidence="high", notes=notes))

    put("deal.asking_price_eur", op.asking_price_eur, "€", op.price_basis)
    put("deal.surface_before_sqm", a.surface_before_sqm, "m²", "surface utile avant travaux")
    put("deal.surface_after_sqm", a.surface_after_sqm, "m²", "surface utile après travaux")
    put("deal.capex_eur_per_sqm_after", op.capex_eur_per_sqm_after, "€ HT/m²", "par m² de surface utile après travaux")
    put("deal.fees_and_contingency_pct_of_capex", op.fees_and_contingency_pct_of_capex, "fraction",
        "honoraires et aléas, en % du coût des travaux")
    put("deal.occupancy_current_pct", op.occupancy_current_pct, "fraction")
    put("deal.ltv", op.financing.ltv, "fraction", "« environ 60 % de dette » ; assiette à préciser en phase 4")
    put("deal.debt_margin_bps", op.financing.margin_bps, "bps", f"marge sur {op.financing.reference_rate}")
    put("deal.dpe_current", a.dpe_current, None)
    put("deal.dpe_target", a.dpe_target, None)
    put("deal.current_leases", [l.model_dump(mode="json") for l in op.current_leases], None)
    put("deal.timeline", op.timeline.model_dump(mode="json"), None)

    for key, val in config.overrides.items():
        registry.set(DataPoint(key=f"override.{key}", value=val,
                               unit="fraction" if key.endswith("_pct") else None,
                               source_name="Override utilisateur (asset_config.json)", data_date=None,
                               method="user_input", status="ok", confidence="high",
                               notes="valeur imposée par l'utilisateur à la place de l'estimation de l'agent"))
