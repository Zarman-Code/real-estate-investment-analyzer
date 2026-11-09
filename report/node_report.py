"""Rapport d'un nœud d'analyse : chiffres clés + analyse + réponses aux questions du comité qui lui sont confiées.

Chaque nœud (financial, risk, sensitivity, scenario) produit :
  - `figures`  : chiffres clés DÉJÀ calculés et formatés, avec leur source (seule matière autorisée) ;
  - `summary`  : analyse rédigée ; `answers` : réponses à ses questions du comité.
Rédaction par le LLM du nœud SI ET SEULEMENT SI son texte passe le contrôle :
  - JSON attendu, questions connues ;
  - CHAQUE nombre présent dans les chiffres du nœud ou dans ses textes de référence ;
  - COMPLÉTUDE : les chiffres clés (figures marquées `cite`) repris dans l'analyse et dans chaque réponse ;
  - aucun qualificatif favorable (« faible », « positive »…) pour une marge ou un TRI négatifs.
Sinon, rédaction automatique (référence).
Le LLM du nœud ne calcule rien et ne voit que les résultats de son propre nœud.
"""
from __future__ import annotations

import json
import re
from typing import Any

from config.schema import PROJECT_ROOT, DefaultsConfig
from decision.llm import LlmClient, allowed_numbers, misleading_qualifiers, missing_citations, unknown_numbers

NOT_AVAILABLE = "Information non disponible"


def figure(fid: str, label: str, value: str, raw: Any = None, source: str | None = None,
           cite: tuple[str, ...] = (), cite_label: bool = False) -> dict:
    """`cite` : textes (id de question ou « summary ») qui DOIVENT reprendre ce chiffre (ou son libellé si
    `cite_label`, pour un risque) ; sinon le texte du LLM est rejeté pour incomplétude."""
    return {"id": fid, "label": label, "value": value, "raw": raw, "source": source,
            "cite": list(cite), "cite_label": cite_label}


def required_citations(figures: list[dict]) -> dict[str, list[str]]:
    """Par texte (« summary » ou id de question) : ce qui doit y figurer (valeur affichée, ou libellé d'un risque)."""
    out: dict[str, list[str]] = {}
    for f in figures:
        for t in f.get("cite") or ():
            out.setdefault(t, []).append(f["label"] if f.get("cite_label") else f"{f['label']} : {f['value']}")
    return out


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
               "chiffres_obligatoires": required_citations(figures),
               "questions": questions, "analyse_de_référence": summary, "réponses_de_référence": answers}
    try:
        raw = llm.complete((PROJECT_ROOT / prompt_path).read_text(encoding="utf-8"),
                           json.dumps(payload, ensure_ascii=False, indent=1))
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        report["drafting"] = {"mode": "template", "status": f"appel LLM échoué : {type(exc).__name__}", "model": llm.model}
        return report
    status = _check(data, questions, figures, cfg.number_tolerance_rel, [summary, *answers.values()])
    if status:
        report["drafting"] = {"mode": "template", "status": f"rejeté : {status}", "model": llm.model}
        return report
    report["summary"] = data["summary"]
    report["answers"] = {qid: (data.get("answers") or {}).get(qid) or answers[qid] for qid in questions}
    report["drafting"] = {"mode": "llm", "status": "accepté", "model": llm.model}
    return report


def _check(data: Any, questions: dict, figures: list[dict], tol: float, reference_texts: list[str]) -> str | None:
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str) or not data["summary"].strip():
        return "résumé absent"
    ans = data.get("answers") or {}
    if not isinstance(ans, dict) or set(ans) - set(questions):
        return "réponses à des questions inconnues"
    texts = [data["summary"]] + [str(v) for v in ans.values()]
    # nombres autorisés : chiffres du nœud ET nombres de ses textes de référence (ex. nombre de transactions)
    allowed = allowed_numbers([{"v": f["value"], "r": f["raw"]} for f in figures] + list(reference_texts))
    bad = sorted({x for t in texts for x in unknown_numbers(t, allowed, tol)})
    if bad:
        return f"nombres absents des chiffres du nœud {bad}"
    gaps = missing_citations({"summary": data["summary"]} | {k: str(v) for k, v in ans.items()}, figures)
    if gaps:
        return "chiffres clés omis : " + " ; ".join(gaps)
    wrong = misleading_qualifiers(texts, figures)
    if wrong:
        return "qualificatif trompeur : " + " ; ".join(wrong)
    altered = altered_risk_notes(texts, figures)
    return "note de risque modifiée : " + " ; ".join(altered) if altered else None


# notes d'un risque citées dans un texte : « P 4/5 », « I 5/5 », « 20/25 », niveau
_NOTE = re.compile(r"\b([PI])\s*(\d)\s*/\s*5\b|\b(\d{1,2})\s*/\s*25\b")
_LEVELS = ("critique", "élevé", "modéré", "faible", "incertain")


def _notes(text: str) -> set[tuple[str, str]]:
    out = {(m[1], m[2]) if m[1] else ("C", m[3]) for m in _NOTE.finditer(text)}
    return out | {("N", lv) for lv in _LEVELS if re.search(rf"\b{lv}s?\b", text)}


def altered_risk_notes(texts: list[str], figures: list[dict]) -> list[str]:
    """Une note de risque (probabilité /5, impact /5, criticité /25, niveau) écrite juste après le libellé d'un risque
    doit être celle que le moteur a transmise pour CE risque. Ces petits nombres échappent au contrôle général des
    nombres (entiers <= 12 tolérés) : sans ce contrôle, une note modifiée passerait."""
    risks = [f for f in figures if f.get("cite_label") and re.search(r"/\s*25", str(f.get("value")))]
    if not risks:
        return []
    labels = [r["label"] for r in risks]
    bad = []
    for text in texts:
        low = text.casefold()
        for r in risks:
            expected = _notes(r["value"])
            for m in re.finditer(re.escape(r["label"].casefold()), low):
                end = len(text)
                for stop in (";", ". ", "\n"):
                    i = text.find(stop, m.end())
                    end = min(end, i) if i >= 0 else end
                for other in labels:
                    if other != r["label"]:
                        j = low.find(other.casefold(), m.end())
                        end = min(end, j) if j >= 0 else end
                got = _notes(text[m.end():min(end, m.end() + 60)])     # la note suit le libellé de près
                diff = got - expected
                if diff:
                    bad.append(f"{r['label']} : {sorted(diff)} au lieu de « {r['value']} »")
    return sorted(set(bad))
