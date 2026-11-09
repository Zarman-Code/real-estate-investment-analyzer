"""Nœud investment_recommendation (plan, section 8) : rapporteur final.

Il ne reçoit QUE les sorties des quatre nœuds d'analyse (financial, risk, sensitivity, scenario) et leurs
rapports rédigés ; il n'accède ni au registre ni aux sources.
1. DÉCISION par règles codées (decision/rules.py) : GO / GO SOUS CONDITIONS / NO-GO / NON DÉTERMINÉ
   (NON DÉTERMINÉ si une donnée exigée manque : prix/m² DVF, loyer ImmoStat, prix d'investissement, YoC, taux de sortie) ;
2. réponses aux questions du comité : celles des analystes, ou la synthèse du LLM final si elle passe le contrôle ;
3. note go/no-go de 5 à 10 lignes. Le LLM final ne peut ni changer la décision ni introduire un chiffre ;
4. texte explicatif (decision/explanation.py) : décision et raisons, risques déclarés, hypothèses à valider,
   seuils qui feraient changer la décision. Chaque chiffre est traçable jusqu'à sa source.
"""
from __future__ import annotations

from agent.state import AgentState
from decision import fmt
from decision.arguments import build_arguments
from decision.explanation import as_text, build_explanation
from decision.facts import analysis_outputs, collect_facts
from decision.llm import LlmClient, draft
from decision.phrase import as_json
from decision.rules import conditions, decide, top_risks
from report.node_report import required_citations
from report.note_go_nogo import merge_note, template_lines


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
            "chiffres_obligatoires_par_question": {
                q: v for r in rec["facts"]["reports"].values() for q, v in required_citations(r["figures"]).items()
                if q != "summary"},
            "note_de_référence": rec["template_lines"],
            "explication_de_référence": rec.get("explanation_text", "")}


def make_recommendation_node(llm_client: LlmClient | None = None):
    def investment_recommendation(state: AgentState) -> dict:
        defaults = state["defaults"]
        rules = defaults.decision_rules
        facts = collect_facts(analysis_outputs(state))
        questions = {qid: q.text for qid, q in defaults.committee_questions.items()}
        verdict = decide(facts, rules)
        conds = conditions(facts, verdict)
        rec = {"decision": verdict["decision"], "triggers": verdict["triggers"], "criteria": verdict["criteria"],
               "facts": facts, "conditions": [str(c) for c in conds], "questions": questions,
               "conditions_struct": [as_json(c) for c in conds],      # gabarits chiffrés pour le dashboard
               "top_risks": top_risks(facts, rules.top_risks), "seller_requests": facts["seller_requests"],
               "arguments": build_arguments(facts, rules.max_arguments),
               "node_drafting": {n: r["drafting"] for n, r in facts["reports"].items()}}
        rec["conditions_phr"] = conds                           # phrases traçables (gabarits) pour la note
        rec["template_lines"] = template_lines(rec)
        del rec["conditions_phr"]
        # texte explicatif final : décision, risques déclarés, hypothèses à valider, seuils de bascule (écrit par le code)
        sections = build_explanation(facts, verdict, rules, defaults.explanation,
                                     defaults.sensitivity.variables if defaults.sensitivity else {})
        rec["explanation"] = [{"title": s["title"], "paragraphs": [as_json(p) for p in s["paragraphs"]]} for s in sections]
        rec["explanation_text"] = as_text(sections)
        figures = [f for r in facts["reports"].values() for f in r["figures"]]
        rec["drafting"] = draft(llm_client, defaults.llm, rec["decision"], final_payload(rec), list(questions), figures)
        node_answers = _node_answers(facts, questions)
        llm_answers = rec["drafting"].get("answers") or {}
        rec["answers"] = {qid: llm_answers.get(qid) or node_answers[qid] for qid in questions}
        rec["synthesis"] = rec["drafting"].get("synthesis") or " ".join(
            r["summary"] for r in facts["reports"].values() if r.get("summary"))
        llm_lines = rec["drafting"].get("note_lines")
        # lignes obligatoires (prix, robustesse, conditions, vendeur, réserves) : toujours celles du code
        rec["note_lines"] = merge_note(llm_lines, rec["template_lines"]) if llm_lines else rec["template_lines"]
        rec["note_struct"] = [as_json(x) for x in rec["note_lines"]]   # note publiée, chiffres traçables (dashboard)
        logs = [f"investment_recommendation : {rec['decision']} ({' ; '.join(rec['triggers'])})",
                f"  rédaction finale : {rec['drafting']['mode']} ({rec['drafting']['status']})"]
        if rec["drafting"].get("disagreement"):
            logs.append(f"  DÉSACCORD DU LLM (affiché, sans effet sur la décision) : {rec['drafting']['disagreement']}")
        return {"recommendation": rec, "logs": logs}
    return investment_recommendation


investment_recommendation = make_recommendation_node(None)

__all__ = ["final_payload", "make_recommendation_node", "investment_recommendation", "fmt"]
