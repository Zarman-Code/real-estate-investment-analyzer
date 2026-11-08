"""Utilitaires partagés par les connecteurs."""
from __future__ import annotations

from typing import Any

from config.schema import Source
from tools.base import ParseError
from tools.connectors import ConnectorContext
from tools.http import FetchResult, SourceUnavailable
from tools.registry import Confidence, DataPoint, Status

LOCATION_KEY = "location.reference_point"


def require_client(ctx: ConnectorContext, source: Source):
    if ctx.client is None:
        raise SourceUnavailable(source.id, source.url or "", "aucun client HTTP fourni")
    return ctx.client


def require_location(ctx: ConnectorContext, source: Source) -> dict[str, Any]:
    """Point de référence géocodé (produit par le connecteur IGN)."""
    found = ctx.registry.found(LOCATION_KEY)
    if not found or not isinstance(found[0].value, dict):
        raise SourceUnavailable(source.id, source.url or "", "point de référence non géocodé")
    return found[0].value


def require_fields(record: dict, fields: list[str], what: str) -> None:
    missing = [f for f in fields if f not in record]
    if missing:
        raise ParseError(f"{what} : champs attendus absents {missing} (champs reçus : {sorted(record)[:15]})")


def as_float(v: Any, what: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ParseError(f"{what} : valeur non numérique {v!r}") from None


def make_point(source: Source, key: str, value: Any, unit: str | None, data_date: str | None,
               fetched: FetchResult | None, notes: str, confidence: Confidence = "high",
               status: Status = "ok") -> DataPoint:
    return DataPoint(key=key, value=value, unit=unit, source_name=source.name,
                     source_url=fetched.url if fetched else source.url, data_date=data_date,
                     retrieved_at=fetched.retrieved_at if fetched else _now(),
                     method="api", status=status, confidence=confidence, notes=notes)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def needs_by_indicator(needs, mapping: dict[str, str]) -> dict[str, Any]:
    """Associe chaque besoin confié au connecteur à l'indicateur qu'il sait produire."""
    out = {}
    for n in needs:
        ind = next((i for i in n.indicators if i in mapping), None)
        if ind is None:
            raise ParseError(f"le connecteur ne sait pas produire {n.key} ({n.indicators})")
        out[n.key] = ind
    return out
