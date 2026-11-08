"""Nœud research_market_data : exécute les connecteurs pour les besoins du plan de recherche.

- 1er passage : tous les besoins ; passages suivants : uniquement les lacunes signalées par
  validate_research, en essayant une source PAS ENCORE TENTÉE (sinon reboucler ne sert à rien).
- Règle du plan : API / fichier d'abord, rapports ensuite (ordre fixé par identify_data_needs).
- Deux vagues : connecteurs indépendants en parallèle, puis connecteurs qui ont besoin du point
  de référence géocodé.
- Tout résultat passe par DataRegistry.merge : une valeur trouvée n'est jamais écrasée par un échec.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from agent.state import AgentState, DataNeed
from tools.base import run_connector
from tools.connectors import ConnectorContext, ConnectorRegistry
from tools.http import HttpClient
from tools.registry import DataPoint


def plan_pass(needs: list[DataNeed], attempts: dict[str, list[str]], connectors: ConnectorRegistry,
              found_counts: dict[str, int] | None = None
              ) -> tuple[dict[str, list[DataNeed]], list[tuple[DataNeed, str]]]:
    """Choisit, pour chaque besoin, les sources candidates non tentées qui ont un connecteur, par priorité :
    autant qu'il en faut pour atteindre min_sources (recoupement), au moins une."""
    plan: dict[str, list[DataNeed]] = defaultdict(list)
    unreachable: list[tuple[DataNeed, str]] = []
    found_counts = found_counts or {}
    for need in needs:
        tried = attempts.get(need.key, [])
        usable = [s for s in need.target_sources if s not in tried and s in connectors]
        if usable:
            take = max(1, need.min_sources - found_counts.get(need.key, 0))
            for sid in usable[:take]:
                plan[sid].append(need)
        elif not need.target_sources:
            unreachable.append((need, f"aucune source au catalogue pour {need.indicators}"))
        else:
            without = [s for s in need.target_sources if s not in connectors]
            parts = [f"sources déjà essayées : {tried}"] if tried else []
            if without:
                parts.append(f"connecteur non implémenté pour {without}")
            unreachable.append((need, " ; ".join(parts)))
    return dict(plan), unreachable


def make_research_node(connectors: ConnectorRegistry, client: HttpClient | None = None):
    def research_market_data(state: AgentState) -> dict:
        pass_no = state["research_iteration"] + 1
        registry, catalog = state["registry"], state["catalog"]
        report = state["validation_report"]
        if pass_no == 1 or report is None:
            targets = state["data_needs"]
        else:
            targets = [n for n in state["data_needs"] if n.key in report.gaps]
        attempts = {k: list(v) for k, v in state["research_attempts"].items()}
        plan, unreachable = plan_pass(targets, attempts, connectors,
                                      {n.key: len(registry.found(n.key)) for n in targets})
        logs = [f"research_market_data passe {pass_no} : {len(targets)} besoins, "
                f"{len(plan)} sources interrogées, {len(unreachable)} sans source utilisable"]

        for need, reason in unreachable:
            if registry.get(need.key) is None:   # ne consigne le manque qu'une fois
                registry.merge(DataPoint.missing(need.key, "Agent - recherche", reason, method="none"))
            logs.append(f"  {need.key} : {reason}")

        ctx = ConnectorContext(config=state["config"], defaults=state["defaults"], catalog=catalog,
                               registry=registry, analysis_date=state["analysis_date"], client=client)
        waves = [[sid for sid in sorted(plan) if not connectors.get(sid).needs_location],
                 [sid for sid in sorted(plan) if connectors.get(sid).needs_location]]
        workers = state["defaults"].research.parallel_workers

        for wave in waves:
            if not wave:
                continue

            def call(sid: str) -> list[DataPoint]:
                needs = plan[sid]
                spec = connectors.get(sid)
                return run_connector(catalog.by_id(sid), [n.key for n in needs], lambda: spec.fn(ctx, needs))

            with ThreadPoolExecutor(max_workers=min(workers, len(wave))) as pool:
                results = dict(zip(wave, pool.map(call, wave)))
            # fusion par clé dans l'ORDRE DE PRIORITÉ des sources du besoin (pas l'ordre alphabétique) :
            # la source la plus fiable qui répond devient la valeur retenue, les autres servent au recoupement
            priority = {n.key: n.target_sources for n in targets}
            entries = [(dp.key, sid, dp) for sid in wave for dp in results[sid]]
            entries.sort(key=lambda e: (e[0], priority.get(e[0], []).index(e[1]) if e[1] in priority.get(e[0], []) else 99))
            for sid in wave:
                for key in {n.key for n in plan[sid]}:
                    attempts.setdefault(key, []).append(sid)
            for key, sid, dp in entries:
                action = registry.merge(dp)
                extra = "" if any(n.key == key for n in plan[sid]) else " (clé non demandée)"
                detail = dp.notes if dp.status == "missing" else f"{dp.status}, confiance {dp.confidence}"
                logs.append(f"  [{sid}] {key}{extra} : {detail} -> {action}")

        # la valeur retenue est la plus prioritaire des sources qui ont répondu, quel que soit l'ordre des vagues
        for need in targets:
            registry.rank_sources(need.key, [catalog.by_id(sid).name for sid in need.target_sources])
        return {"registry": registry, "research_iteration": pass_no, "research_attempts": attempts, "logs": logs}

    return research_market_data
