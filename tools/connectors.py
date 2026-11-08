"""Interface entre research_market_data et les connecteurs de données.

Un connecteur est une fonction pure du point de vue de l'agent :

    def fetch(ctx: ConnectorContext, needs: list[DataNeed]) -> list[DataPoint]

Il reçoit les besoins qu'on lui confie (une ou plusieurs clés du registre) et renvoie
des DataPoint pour ces clés. Il peut lever SourceUnavailable ou ParseError : run_connector
transforme alors chaque clé en DataPoint(status="missing").

Les connecteurs réels (phase 2) s'enregistrent ici avec @connector("<id de source>").
Tant qu'une source n'a pas de connecteur, ses besoins sont marqués « connecteur non implémenté ».
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from config.schema import AssetConfig, DefaultsConfig, SourcesCatalog
from tools.registry import DataPoint, DataRegistry

if TYPE_CHECKING:
    from agent.state import DataNeed
    from tools.http import HttpClient


@dataclass
class ConnectorContext:
    config: AssetConfig
    defaults: DefaultsConfig
    catalog: SourcesCatalog
    registry: DataRegistry          # lecture seule pour les connecteurs (ex. point de référence géocodé)
    analysis_date: str
    client: "HttpClient | None" = None


ConnectorFn = Callable[[ConnectorContext, "list[DataNeed]"], list[DataPoint]]


@dataclass(frozen=True)
class ConnectorSpec:
    source_id: str
    fn: ConnectorFn
    needs_location: bool = False    # exécuté après le géocodage du point de référence


@dataclass
class ConnectorRegistry:
    specs: dict[str, ConnectorSpec] = field(default_factory=dict)

    def register(self, source_id: str, fn: ConnectorFn, needs_location: bool = False) -> None:
        if source_id in self.specs:
            raise ValueError(f"connecteur déjà enregistré : {source_id}")
        self.specs[source_id] = ConnectorSpec(source_id, fn, needs_location)

    def get(self, source_id: str) -> ConnectorSpec | None:
        return self.specs.get(source_id)

    def __contains__(self, source_id: object) -> bool:
        return source_id in self.specs


# Registre global des connecteurs réels (rempli par les modules tools/<source>.py en phase 2)
CONNECTORS = ConnectorRegistry()


def connector(source_id: str, needs_location: bool = False) -> Callable[[ConnectorFn], ConnectorFn]:
    def deco(fn: ConnectorFn) -> ConnectorFn:
        CONNECTORS.register(source_id, fn, needs_location)
        return fn
    return deco
