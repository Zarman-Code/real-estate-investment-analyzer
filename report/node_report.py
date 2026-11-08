"""Rapport d'un nœud d'analyse : chiffres clés + analyse + réponses aux questions du comité qui lui sont confiées.

Chaque nœud (financial, risk, sensitivity, scenario) produit :
  - `figures`  : chiffres clés DÉJÀ calculés et formatés, avec leur source (seule matière autorisée) ;
  - `summary`  : analyse rédigée ; `answers` : réponses à ses questions du comité.
Rédaction par le LLM du nœud SI ET SEULEMENT SI son texte passe le contrôle : JSON attendu, questions
connues, et CHAQUE nombre présent dans les chiffres du nœud. Sinon, rédaction automatique (référence).
Le LLM du nœud ne calcule rien et ne voit que les résultats de son propre nœud.
"""
from __future__ import annotations

import json
from typing import Any

from config.schema import PROJECT_ROOT, DefaultsConfig
from decision.llm import LlmClient, allowed_numbers, unknown_numbers

NOT_AVAILABLE = "Information non disponible"


def figure(fid: str, label: str, value: str, raw: Any = None, source: str | None = None) -> dict:
    return {"id": fid, "label": label, "value": value, "raw": raw, "source": source}


def node_questions(defaults: DefaultsConfig, node: str) -> dict[str, str]:
    return {qid: q.text for qid, q in defaults.committee_questions.items() if q.node == node}


def make_report(node: str, figures: list[dict], summary: str, answers: dict[str, str],
                defaults: DefaultsConfig, llm: LlmClient | None) -> dict:
    questions = node_questions(defaults, node)
    answers = {qid: answers.get(qid) or f"{NOT_AVAILABLE}." for qid in questions}
    report = {"node": node, "figures": figures, "questions": questions, "summary": summary, "answers": answers,
              "reference_summary": summary, "reference_answers": dict(answers),
              "drafting": {"mode": "template", "status": "pas d'appel LLM", "model": None}}
    cfg = defaults.llm
    prompt_path = cfg.node_prompts.get(node) if cfg else None
    if llm is None or cfg is None or not prompt_path:
        return report
    payload = {"nœud": node, "chiffres": [{k: f[k] for k in ("label", "value", "source")} for f in figures],
               "questions": questions, "analyse_de_référence": summary, "réponses_de_référence": answers}
    try:
        raw = llm.complete((PROJECT_ROOT / prompt_path).read_text(encoding="utf-8"),
                           json.dumps(payload, ensure_ascii=False, indent=1))
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        report["drafting"] = {"mode": "template", "status": f"appel LLM échoué : {type(exc).__name__}", "model": llm.model}
        return report
    status = _check(data, questions, figures, cfg.number_tolerance_rel)
    if status:
        report["drafting"] = {"mode": "template", "status": f"rejeté : {status}", "model": llm.model}
        return report
    report["summary"] = data["summary"]
    report["answers"] = {qid: (data.get("answers") or {}).get(qid) or answers[qid] for qid in questions}
    report["drafting"] = {"mode": "llm", "status": "accepté", "model": llm.model}
    return report


def _check(data: Any, questions: dict, figures: list[dict], tol: float) -> str | None:
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str) or not data["summary"].strip():
        return "résumé absent"
    ans = data.get("answers") or {}
    if not isinstance(ans, dict) or set(ans) - set(questions):
        return "réponses à des questions inconnues"
    texts = [data["summary"]] + [str(v) for v in ans.values()]
    allowed = allowed_numbers([{"v": f["value"], "r": f["raw"]} for f in figures])
    bad = sorted({x for t in texts for x in unknown_numbers(t, allowed, tol)})
    return f"nombres absents des chiffres du nœud {bad}" if bad else None
