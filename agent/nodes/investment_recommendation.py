"""Nœud investment_recommendation (plan, section 8) : rapporteur final.

Il ne reçoit QUE les sorties des quatre nœuds d'analyse (financial, risk, sensitivity, scenario) et leurs
rapports rédigés ; il n'accède ni au registre ni aux sources.
1. DÉCISION par règles codées (decision/rules.py) : GO / GO SOUS CONDITIONS / NO-GO / NON DÉTERMINÉ
   (NON DÉTERMINÉ si une donnée exigée manque : prix/m² DVF, loyer ImmoStat, prix d'investissement, YoC, taux de sortie) ;
2. réponses aux questions du comité : celles des analystes, ou la synthèse du LLM final si elle passe le contrôle ;
3. note go/no-go de 5 à 10 lignes. Le LLM final ne peut ni changer la décision ni introduire un chiffre.
"""
from __future__ import annotations

from agent.state import AgentState
from decision import fmt
from decision.arguments import build_arguments
from decision.facts import analysis_outputs, collect_facts
from decision.llm import LlmClient, draft
from decision.rules import conditions, decide, top_risks
from report.note_go_nogo import template_lines


def _node_answers(facts: dict, questions: dict) -> dict:
    out = {}
    for rep in facts["reports"].values():
        out.update(rep.get("answers") or {})
    return {qid: out.get(qid, "Information non disponible.") for qid in questions}


def final_payload(rec: dict) -> dict:
    """Ce que voit le LLM final : la décision, et les rapports des quatre analystes. Rien d'autre."""
    return {"décision": rec["decision"], "déclencheurs": rec["triggers"], "conditions": rec["conditions"],
            "demandes_vendeur": rec["seller_requests"], "questions": rec["questions"],
            "rapports_des_analystes": {
                node: {"analyse": r["summary"], "réponses": r["answers"],
                       "chiffres": [{k: f[k] for k in ("label", "value", "source")} for f in r["figures"]]}
                for node, r in rec["facts"]["reports"].items()},
            "note_de_référence": rec["template_lines"]}


def make_recommendation_node(llm_client: LlmClient | None = None):
    def investment_recommendation(state: AgentState) -> dict:
        defaults = state["defaults"]
        rules = defaults.decision_rules
        facts = collect_facts(analysis_outputs(state))
        questions = {qid: q.text for qid, q in defaults.committee_questions.items()}
        verdict = decide(facts, rules)
        rec = {"decision": verdict["decision"], "triggers": verdict["triggers"], "criteria": verdict["criteria"],
               "facts": facts, "conditions": conditions(facts, verdict), "questions": questions,
               "top_risks": top_risks(facts, rules.top_risks), "seller_requests": facts["seller_requests"],
               "arguments": build_arguments(facts, rules.max_arguments),
               "node_drafting": {n: r["drafting"] for n, r in facts["reports"].items()}}
        rec["template_lines"] = template_lines(rec)
        rec["drafting"] = draft(llm_client, defaults.llm, rec["decision"], final_payload(rec), list(questions))
        node_answers = _node_answers(facts, questions)
        llm_answers = rec["drafting"].get("answers") or {}
        rec["answers"] = {qid: llm_answers.get(qid) or node_answers[qid] for qid in questions}
        rec["synthesis"] = rec["drafting"].get("synthesis") or " ".join(
            r["summary"] for r in facts["reports"].values() if r.get("summary"))
        rec["note_lines"] = rec["drafting"].get("note_lines") or rec["template_lines"]
        logs = [f"investment_recommendation : {rec['decision']} ({' ; '.join(rec['triggers'])})",
                f"  rédaction finale : {rec['drafting']['mode']} ({rec['drafting']['status']})"]
        if rec["drafting"].get("disagreement"):
            logs.append(f"  DÉSACCORD DU LLM (affiché, sans effet sur la décision) : {rec['drafting']['disagreement']}")
        return {"recommendation": rec, "logs": logs}
    return investment_recommendation


investment_recommendation = make_recommendation_node(None)

__all__ = ["final_payload", "make_recommendation_node", "investment_recommendation", "fmt"]
