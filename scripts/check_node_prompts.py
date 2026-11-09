"""Vérifie les prompts des quatre nœuds d'analyse avec le VRAI LLM, sur les données enregistrées (aucun appel aux sources).

Usage (depuis le dossier helios_agent, clé d'API dans la variable d'environnement prévue par config/defaults.yaml) :
    python scripts/check_node_prompts.py            # 3 essais
    python scripts/check_node_prompts.py --runs 5

Pour chaque nœud et chaque essai : texte du LLM accepté ou rejeté par le validateur (et pourquoi), longueur
(analyse 3 à 6 phrases, réponses 1 à 3 phrases), et contrôles complémentaires que le validateur ne fait pas :
risque non transmis, scénario non transmis, variable de sensibilité non testée, recommandation formulée.
Ces contrôles complémentaires sont des signaux à relire, pas des rejets.
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from config.schema import load_defaults  # noqa: E402
from decision.llm import make_client  # noqa: E402

NODES = {"financial_analysis": "financials", "risk_analysis": "risks", "sensitivity_analysis": "sensitivity",
         "scenario_analysis": "scenarios"}
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÀ-ÖØ-Þ«])")
RECOMMENDATION = re.compile(r"\b(GO|NO-GO|recommand\w+ (?:d'|de )?(?:acheter|investir|acquérir))\b")
UNTESTED = ("prix d'acquisition", "frais d'acquisition", "coûts de portage", "coût de portage")


def sentences(text: str) -> int:
    return len([s for s in SENTENCE.split(text.strip()) if s.strip()])


def extra_checks(node: str, rep: dict, final: dict) -> list[str]:
    texts = [rep["summary"], *rep["answers"].values()]
    full = " ".join(texts)
    out = []
    if not 3 <= sentences(rep["summary"]) <= 6:
        out.append(f"analyse de {sentences(rep['summary'])} phrases (attendu 3 à 6)")
    out += [f"{q} : {sentences(a)} phrases (attendu 1 à 3)" for q, a in rep["answers"].items() if not 1 <= sentences(a) <= 3]
    if RECOMMENDATION.search(full):
        out.append("formule une recommandation (réservée au rapporteur final)")
    if node == "risk_analysis":
        for e in final["risks"].get("excluded", []):
            if e["title"].casefold() in full.casefold():
                out.append(f"risque exclu mentionné : {e['title']}")
    if node == "scenario_analysis":
        names = {f["label"].split("scénario ", 1)[1].casefold() for f in rep["figures"]}
        for m in re.finditer(r"scénario\s+([A-ZÉ][\wéèê-]+)", full):
            if m[1].casefold() not in names:
                out.append(f"scénario non transmis : {m[1]}")
    if node == "sensitivity_analysis":
        out += [f"variable non testée citée : {v}" for v in UNTESTED if v in full.casefold()]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    d = load_defaults()
    llm, status = make_client(d.llm)
    if llm is None:
        print(f"LLM indisponible : {status}")
        return 2
    from realdata import run_on_fixtures
    tally = {n: 0 for n in NODES}
    for i in range(1, args.runs + 1):
        final = run_on_fixtures(Path(tempfile.mkdtemp()), llm)
        print(f"\n===== essai {i}/{args.runs} ({llm.model})")
        for node, key in NODES.items():
            rep = final[key]["report"]
            ok = rep["drafting"]["mode"] == "llm"
            tally[node] += ok
            print(f"  {node:22s} {'ACCEPTÉ' if ok else 'REJETÉ '} {'' if ok else rep['drafting']['status']}")
            if ok:
                for w in extra_checks(node, rep, final):
                    print(f"      à relire : {w}")
    print("\nTaux d'acceptation : " + " ; ".join(f"{n} {k}/{args.runs}" for n, k in tally.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
