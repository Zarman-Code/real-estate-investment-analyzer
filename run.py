"""Point d'entrée : charge et valide la configuration, exécute le graphe, exporte le registre.

    python run.py                       # asset_config.json du projet
    python run.py chemin/vers/autre.json
    python run.py --offline             # sans réseau : rejoue uniquement les réponses du cache disque
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent.graph import build_graph, recursion_limit
from agent.state import build_initial_state
from config.schema import (ConfigError, PROJECT_ROOT, check_needs_against_catalog, load_config,
                           load_defaults, load_investor_targets, load_sources_catalog)
import tools.sources  # noqa: F401  (enregistre les connecteurs dans CONNECTORS)
from tools.connectors import CONNECTORS
from decision.llm import make_client
from report.dashboard import write_dashboard
from tools.http import HttpClient


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Agent d'aide à la décision d'investissement")
    ap.add_argument("config", nargs="?", default=str(PROJECT_ROOT / "asset_config.json"))
    ap.add_argument("--offline", action="store_true", help="aucune requête réseau : uniquement le cache disque")
    ap.add_argument("--out", default=str(PROJECT_ROOT / "output"))
    args = ap.parse_args(argv)

    try:  # tout est validé AVANT le moindre appel réseau
        cfg, defaults = load_config(args.config), load_defaults()
        targets, catalog = load_investor_targets(), load_sources_catalog()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    state = build_initial_state(cfg, defaults, targets, catalog)
    state["logs"] += [f"catalogue : {w}" for w in check_needs_against_catalog(defaults, catalog)]
    client = HttpClient(offline=args.offline)
    llm_client, llm_status = (None, "mode hors ligne") if args.offline else make_client(defaults.llm)
    state["logs"].append(f"LLM de rédaction : {defaults.llm.model if llm_client else 'aucun'} ({llm_status})")
    try:
        final = build_graph(CONNECTORS, client, llm_client).invoke(
            state, config={"recursion_limit": recursion_limit(defaults.research.max_research_loops)})
    finally:
        client.close()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    final["registry"].export_json(out / "data_registry.json")
    report = final["validation_report"]
    (out / "validation_report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (out / "run_log.txt").write_text("\n".join(final["logs"]), encoding="utf-8")
    analysis = {"analysis_date": final["analysis_date"], "asset": cfg.asset.name, "financials": final["financials"],
                "risks": final["risks"], "sensitivity": final["sensitivity"], "scenarios": final["scenarios"],
                "recommendation": final["recommendation"]}
    (out / "analysis_results.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2, default=str),
                                               encoding="utf-8")
    rec = final["recommendation"]          # la note go/no-go est intégrée au dashboard (section « Note go/no-go »)
    try:
        dashboard = write_dashboard(out, cfg, defaults)        # nouvelle version (specification_dashboard_helios.md)
    except Exception as exc:  # noqa: BLE001 - un livrable en échec ne doit pas masquer les résultats de l'agent
        dashboard = f"NON GÉNÉRÉ ({type(exc).__name__} : {exc})"

    print("\n".join(final["logs"]))
    print(f"\nverdict : {report.verdict} | passes de recherche : {final['research_iteration']} | "
          f"manquants : {len(final['registry'].missing())} | hypothèses : {len(final['registry'].assumptions())} | "
          f"rejetées : {len(final['registry'].rejected())}")
    print(f"recommandation : {rec['decision']} | rédaction : {rec['drafting']['mode']} ({rec['drafting']['status']})")
    if rec.get("explanation_text"):                     # texte explicatif final : décision, risques, hypothèses à valider
        print("\n" + "=" * 100 + "\nEXPLICATION DE LA DÉCISION\n" + "=" * 100 + "\n" + rec["explanation_text"] + "\n" + "=" * 100)
    print(f"sorties : {out}")
    print(f"dashboard : {dashboard}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
