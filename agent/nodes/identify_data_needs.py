"""Nœud identify_data_needs : transforme le catalogue de besoins (defaults.yaml) en plan de recherche.

Déterministe, sans LLM : pour chaque besoin encore non couvert, choisit les sources candidates
dans sources_catalog.yaml (règle du plan : API / fichier structuré d'abord, rapports ensuite),
résout le périmètre à partir de asset_config.json et trie par priorité.
Un besoin couvert par un override reste recherché (référence de marché), mais n'est plus bloquant.
Aucun indicateur de marché n'est jamais demandé à l'utilisateur.
"""
from __future__ import annotations

from config.schema import NeedSpec, SourcesCatalog
from agent.state import AgentState, DataNeed

TYPE_RANK = {"api": 0, "file": 0, "report": 1}
PRIORITY_RANK = {"very_high": 0, "high": 1, "medium": 2, "low": 3}


def candidate_sources(spec: NeedSpec, catalog: SourcesCatalog) -> list[str]:
    seen: dict[str, tuple[int, int, int]] = {}
    for i_rank, indicator in enumerate(spec.indicators):
        for s in catalog.for_indicator(indicator):
            rank = (TYPE_RANK[s.type], i_rank, PRIORITY_RANK[s.priority])
            if s.id not in seen or rank < seen[s.id]:
                seen[s.id] = rank
    return sorted(seen, key=lambda sid: (seen[sid], sid))


def resolve_scope(spec: NeedSpec, state: AgentState) -> str:
    a, research = state["config"].asset, state["defaults"].research
    ref = a.reference_point.label
    return {
        "reference_point": f"point de référence : {ref}",
        "radius": f"rayons {'/'.join(map(str, research.radius_m))} m autour de : {ref}",
        "district": a.district or f"arrondissement du point de référence ({ref})",
        "submarket": a.submarket or "sous-marché du point de référence",
        "national": "national / zone euro",
    }[spec.scope]


def resolve_period(spec: NeedSpec, state: AgentState) -> str | None:
    research = state["defaults"].research
    if spec.max_age_months:
        return f"donnée de moins de {spec.max_age_months} mois au {state['analysis_date']}"
    if spec.scope == "radius" and spec.min_sample:
        end = state["analysis_date"] if research.transactions_to == "today" else str(research.transactions_to)
        return f"transactions du {research.transactions_from} au {end}"
    return None


def identify_data_needs(state: AgentState) -> dict:
    cfg, registry, catalog = state["config"], state["registry"], state["catalog"]
    needs: list[DataNeed] = []
    logs: list[str] = []
    for spec in state["defaults"].data_needs:
        priority, reason = spec.priority, spec.reason
        if spec.override and spec.override in cfg.overrides:
            # la valeur imposée sera utilisée dans les calculs, mais la référence de marché reste recherchée
            # pour que le comité puisse juger l'écart ; elle n'est plus bloquante (P1 -> P2)
            priority = "P2" if spec.priority == "P1" else spec.priority
            reason = (f"{spec.reason} ; valeur imposée par l'override '{spec.override}' = "
                      f"{cfg.overrides[spec.override]} : recherche pour comparaison seulement")
            logs.append(f"besoin {spec.key} : override '{spec.override}' utilisé dans les calculs, "
                        "référence de marché recherchée pour comparaison (non bloquante)")
        if registry.found(spec.key):
            logs.append(f"besoin {spec.key} : déjà présent dans le registre, non recherché")
            continue
        needs.append(DataNeed(key=spec.key, indicators=spec.indicators, priority=priority,
                              target_sources=candidate_sources(spec, catalog), min_sources=spec.min_sources,
                              scope=resolve_scope(spec, state), period=resolve_period(spec, state),
                              reason=reason))
    needs.sort(key=lambda n: n.priority)  # tri stable : l'ordre du fichier est conservé dans chaque priorité
    counts = {p: sum(n.priority == p for n in needs) for p in ("P1", "P2", "P3")}
    logs.append(f"identify_data_needs : {len(needs)} besoins ({counts['P1']} P1, {counts['P2']} P2, {counts['P3']} P3)")
    for n in needs:
        if not n.target_sources:
            logs.append(f"  {n.key} : aucune source au catalogue pour {n.indicators}")
    return {"data_needs": needs, "logs": logs}
