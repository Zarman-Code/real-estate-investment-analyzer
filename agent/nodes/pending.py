"""Nœuds aval, en attente de leur phase. Ils existent dès maintenant pour que le graphe ait sa
topologie définitive (section 1.1) ; ils ne calculent rien et le disent dans le journal."""
from __future__ import annotations

from agent.state import AgentState


def _pending(name: str, phase: int):
    def node(state: AgentState) -> dict:
        return {"logs": [f"{name} : non implémenté (phase {phase}), aucun résultat produit"]}
    node.__name__ = name
    return node
