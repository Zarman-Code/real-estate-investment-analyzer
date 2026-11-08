"""Graphe LangGraph de l'agent : les 8 nœuds de la section 1.1, et seulement eux.

        identify_data_needs
                │
        research_market_data ◄──────┐  lacunes P1, retours <= max_research_loops
                │                   │
        validate_research ──────────┘
                │ sinon
        ┌───────┴────────┐
 financial_analysis   risk_analysis          (en parallèle)
        └───────┬────────┘
       sensitivity_analysis                  (attend les deux)
                │
        scenario_analysis
                │
     investment_recommendation               (attend financial, risk ET scenario)

Les dépendances (connecteurs, client HTTP, client LLM) sont injectées : les tests passent des doublures.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from agent.nodes.financial_analysis import make_financial_node
from agent.nodes.identify_data_needs import identify_data_needs
from agent.nodes.investment_recommendation import make_recommendation_node
from agent.nodes.research_market_data import make_research_node
from agent.nodes.risk_analysis import make_risk_node
from agent.nodes.scenario_analysis import make_scenario_node
from agent.nodes.sensitivity_analysis import make_sensitivity_node
from agent.nodes.validate_research import make_validate_node, route_after_validation
from agent.state import AgentState
from tools.connectors import CONNECTORS, ConnectorRegistry
from tools.http import HttpClient

NODES = ("identify_data_needs", "research_market_data", "validate_research", "financial_analysis",
         "risk_analysis", "sensitivity_analysis", "scenario_analysis", "investment_recommendation")


def build_graph(connectors: ConnectorRegistry = CONNECTORS, client: HttpClient | None = None, llm_client=None):
    g = StateGraph(AgentState)
    g.add_node("identify_data_needs", identify_data_needs)
    g.add_node("research_market_data", make_research_node(connectors, client))
    g.add_node("validate_research", make_validate_node(connectors))
    # un LLM par nœud d'analyse (rédaction contrôlée de son propre rapport)
    g.add_node("financial_analysis", make_financial_node(llm_client))
    g.add_node("risk_analysis", make_risk_node(llm_client))
    g.add_node("sensitivity_analysis", make_sensitivity_node(llm_client))
    g.add_node("scenario_analysis", make_scenario_node(llm_client))
    g.add_node("investment_recommendation", make_recommendation_node(llm_client))

    g.add_edge(START, "identify_data_needs")
    g.add_edge("identify_data_needs", "research_market_data")
    g.add_edge("research_market_data", "validate_research")
    g.add_conditional_edges("validate_research", route_after_validation,
                            ["research_market_data", "financial_analysis", "risk_analysis"])
    g.add_edge(["financial_analysis", "risk_analysis"], "sensitivity_analysis")
    g.add_edge("sensitivity_analysis", "scenario_analysis")
    g.add_edge(["financial_analysis", "risk_analysis", "scenario_analysis"], "investment_recommendation")
    g.add_edge("investment_recommendation", END)
    return g.compile()


def recursion_limit(max_research_loops: int) -> int:
    """Borne de sécurité LangGraph : couvre le pire cas prévu (1 + max_loops passes de recherche) avec marge.
    La vraie limite de la boucle est la règle de validate_research ; ceci n'est qu'un filet."""
    return 2 * (1 + max_research_loops) + 10
