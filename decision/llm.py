"""Rédaction par LLM, sous contrôle. Le LLM reçoit la décision et les faits déjà calculés et formatés ;
il renvoie un JSON strict. Le texte n'est ACCEPTÉ que si :
  - le JSON a la forme attendue ;
  - la note (lignes libres) fait 1 à 10 lignes et commence par la décision exacte ; les lignes obligatoires
    (prix maximum, robustesse, conditions, vendeur, réserves) sont ajoutées par le code, pas par le LLM ;
  - CHAQUE nombre du texte correspond à un chiffre des faits (tolérance d'arrondi d'affichage) ;
  - chaque réponse reprend les chiffres clés exigés par l'analyste ; aucun qualificatif favorable pour une marge
    ou un TRI négatifs ; sous un NO-GO, le prix maximum n'est pas présenté comme une voie de réalisation.
Sinon : rejet, la rédaction automatique sans LLM est utilisée, et la raison du rejet est tracée.
Sans clé d'API ou sans bibliothèque openai : pas d'appel, rédaction automatique.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Protocol

from config.schema import PROJECT_ROOT, LlmConfig


class LlmClient(Protocol):
    model: str

    def complete(self, system: str, user: str) -> str: ...


class OpenAIClient:
    """Client officiel OpenAI (bibliothèque `openai`, API chat.completions, sortie JSON)."""

    def __init__(self, cfg: LlmConfig, api_key: str):
        from openai import OpenAI          # import tardif : dépendance optionnelle
        self._client = OpenAI(api_key=api_key)
        self.model, self.temperature = cfg.model, cfg.temperature

    def complete(self, system: str, user: str) -> str:
        resp = self._client.chat.completions.create(
            model=self.model, temperature=self.temperature, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        return resp.choices[0].message.content or ""


def make_client(cfg: LlmConfig | None) -> tuple[LlmClient | None, str]:
    if cfg is None:
        return None, "LLM non configuré"
    key = os.environ.get(cfg.api_key_env)
    if not key:
        return None, f"variable d'environnement {cfg.api_key_env} absente"
    try:
        return OpenAIClient(cfg, key), "ok"
    except ImportError:
        return None, "bibliothèque openai non installée (pip install openai)"


# ------------------------------------------------------------------ contrôle des nombres

NUMBER = re.compile(r"(?<![\w])-?\d{1,3}(?:[   ]\d{3})+(?:,\d+)?|-?\d+(?:,\d+)?")


# dates (2026-10, 2021-12-30, 2026-T3, 2026-Q2) : ce ne sont pas des chiffres à contrôler
DATE = re.compile(r"\b\d{4}-(?:\d{2}(?:-\d{2})?|[TQ][1-4])\b")


def parse_numbers(text: str) -> list[float]:
    out = []
    for tok in NUMBER.findall(DATE.sub(" ", text)):
        clean = re.sub(r"[   ]", "", tok).replace(",", ".")
        try:
            out.append(float(clean))
        except ValueError:
            pass
    return out


def allowed_numbers(obj: Any) -> set[float]:
    """Tous les nombres des faits, avec leurs formes d'affichage (%, M€, pb, points)."""
    vals: set[float] = set()

    def walk(o):
        if isinstance(o, bool):
            return
        if isinstance(o, (int, float)):
            x = float(o)
            vals.update({x, abs(x), x * 100, abs(x) * 100, x / 1e6, abs(x) / 1e6, x * 10_000, abs(x) * 10_000})
        elif isinstance(o, str):
            vals.update(parse_numbers(o))
            vals.update(abs(v) for v in parse_numbers(o))
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)
    walk(obj)
    return vals


def unknown_numbers(text: str, allowed: set[float], tol_rel: float) -> list[float]:
    bad = []
    for x in parse_numbers(text):
        ax = abs(x)
        if ax <= 12 or 2000 <= ax <= 2100:     # petits entiers (énumérations, mois, R1..R9) et années
            continue
        ok = any(abs(ax - a) <= max(0.051, tol_rel * abs(a)) for a in allowed)
        if not ok:
            bad.append(x)
    return bad


# ------------------------------------------------------------------ complétude et qualificatifs

def _norm(text: str) -> str:
    return text.casefold().replace("’", "'").replace("\u2212", "-")


def cites_value(text: str, value: str) -> bool:
    """Le texte reprend-il le chiffre affiché `value` (au signe près : « -25,3 % » ou « 25,3 % négative ») ?"""
    wanted = [abs(x) for x in parse_numbers(value)]
    if not wanted:
        return True                       # valeur non numérique (« n.d. ») : rien à exiger
    got = [abs(x) for x in parse_numbers(text)]
    return all(any(abs(w - g) <= max(0.051, 0.005 * w) for g in got) for w in wanted)


def missing_citations(texts: dict[str, str], figures: list[dict]) -> list[str]:
    """Chiffres clés exigés : chaque figure marquée `cite` doit apparaître dans le texte de chaque cible citée
    (id de question ou « summary »). Une cible absente de `texts` n'est pas contrôlée (repli sur la référence)."""
    out = []
    for f in figures:
        for target in f.get("cite") or ():
            text = texts.get(target)
            if text is None:
                continue
            ok = (_norm(f["label"]) in _norm(text)) if f.get("cite_label") else cites_value(text, f["value"])
            if not ok:
                out.append(f"{target} : {f['label']}" + ("" if f.get("cite_label") else f" ({f['value']})"))
    return out


# qualificatifs favorables interdits pour une grandeur dont TOUTES les valeurs du nœud sont négatives
FAVOURABLE = re.compile(r"\b(positi(?:f|fs|ve|ves)|faibles?|légères?|modestes?|limitées?|confortables?|"
                        r"satisfaisant(?:e|es|s)?|bénéficiaires?)\b")
NEGATION = re.compile(r"\b(non|pas|ni|jamais|aucun|aucune|guère)\s+(?:\w+\s+)?$")
QUANTITIES = {"marge": re.compile(r"\bmarges?\b"), "TRI": re.compile(r"\btri\b")}


def misleading_qualifiers(texts: list[str], figures: list[dict]) -> list[str]:
    """Phrases qui qualifient favorablement (« faible », « positive »…) une marge ou un TRI alors que toutes
    les valeurs correspondantes des chiffres sont négatives."""
    negative = {}
    for q in QUANTITIES:
        vals = [f["raw"] for f in figures if isinstance(f.get("raw"), (int, float)) and not isinstance(f["raw"], bool)
                and QUANTITIES[q].search(_norm(f["label"])) and "effet" not in _norm(f["label"])
                and "amplitude" not in _norm(f["label"]) and "à" not in _norm(f["label"]).split()]
        negative[q] = bool(vals) and all(v < 0 for v in vals)
    bad = []
    for text in texts:
        for sentence in re.split(r"(?<=[.;:!?])\s+", _norm(text)):
            for q, rx in QUANTITIES.items():
                if not negative[q] or not rx.search(sentence) or "négati" in sentence or "perte" in sentence:
                    continue
                for m in FAVOURABLE.finditer(sentence):
                    if not NEGATION.search(sentence[:m.start()]):
                        bad.append(f"« {m.group(0)} » pour une {q} négative")
    return sorted(set(bad))


# ------------------------------------------------------------------ rédaction

def load_prompt(cfg: LlmConfig) -> str:
    """Consigne du LLM ; « <MAX_LIGNES> » y est remplacé par la limite de config/defaults.yaml (llm.max_text_lines)."""
    return (PROJECT_ROOT / cfg.prompt_file).read_text(encoding="utf-8").replace("<MAX_LIGNES>", str(cfg.max_text_lines))


SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÀ-ÖØ-Þ«\"(])")


def limit_lines(text: str, n: int) -> tuple[str, bool]:
    """Texte ramené à `n` lignes au plus : une phrase par ligne (les retours à la ligne du LLM comptent aussi).
    Renvoie (texte, tronqué ?). Coupe aux frontières de phrases, jamais au milieu d'une phrase."""
    lines = [s.strip() for chunk in text.splitlines() for s in SENTENCE_END.split(chunk) if s.strip()]
    return "\n".join(lines[:n]), len(lines) > n


# sous un NO-GO : formulations qui présentent la baisse de prix comme une voie de réalisation
NO_GO_FORBIDDEN = re.compile(r"\b(ramener|baisser|réduire|renégocier|négocier)\s+(?:le|du)\s+prix\b")


def draft(client: LlmClient | None, cfg: LlmConfig | None, decision: str, payload: dict,
          question_ids: list[str] | None = None, figures: list[dict] | None = None) -> dict:
    """Rédaction finale. Renvoie {"mode": "llm"|"template", "status": ...}. N'altère jamais la décision.
    Acceptée seulement si : JSON attendu, note de 5 à 10 lignes commençant par la décision exacte,
    et CHAQUE nombre (note, réponses, synthèse, désaccord) présent dans les rapports des analystes."""
    if client is None or cfg is None:
        return {"mode": "template", "status": "pas d'appel LLM", "model": None}
    user = "DÉCISION : " + decision + "\n\nENTRÉES (JSON) :\n" + json.dumps(payload, ensure_ascii=False, indent=1, default=str)
    try:
        raw = client.complete(load_prompt(cfg), user)
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - réseau, quota, JSON invalide : repli sans LLM
        return {"mode": "template", "status": f"appel LLM échoué : {type(exc).__name__}", "model": client.model}
    if not isinstance(data, dict):
        return {"mode": "template", "status": "rejeté : réponse non structurée", "model": client.model}
    lines = data.get("note_lines")
    if not (isinstance(lines, list) and all(isinstance(x, str) for x in lines) and 1 <= len(lines) <= 10):
        return {"mode": "template", "status": "rejeté : note_lines absente ou hors de 1 à 10 lignes", "model": client.model}
    if f"Recommandation : {decision}" not in lines[0]:
        return {"mode": "template", "status": "rejeté : la note ne reprend pas la décision calculée", "model": client.model}
    answers = data.get("answers") or {}
    if not isinstance(answers, dict) or (question_ids and set(answers) - set(question_ids)):
        return {"mode": "template", "status": "rejeté : réponses à des questions inconnues", "model": client.model}
    synthesis = data.get("synthesis") if isinstance(data.get("synthesis"), str) else ""
    texts = lines + [str(v) for v in answers.values()] + [synthesis]
    texts += [x for k in ("arguments_for", "arguments_against") for x in data.get(k, []) if isinstance(x, str)]
    dis = data.get("disagreement") or {}
    if isinstance(dis, dict) and dis.get("reason"):
        texts.append(str(dis["reason"]))
    allowed = allowed_numbers(payload)
    bad = sorted({x for t in texts for x in unknown_numbers(t, allowed, cfg.number_tolerance_rel)})
    if bad:
        return {"mode": "template", "status": f"rejeté : nombres absents des faits {bad}", "model": client.model}
    if figures:
        gaps = missing_citations({k: str(v) for k, v in answers.items()}, figures)
        if gaps:
            return {"mode": "template", "status": "rejeté : chiffres clés omis : " + " ; ".join(gaps), "model": client.model}
        wrong = misleading_qualifiers(texts, figures)
        if wrong:
            return {"mode": "template", "status": "rejeté : qualificatif trompeur : " + " ; ".join(wrong), "model": client.model}
    if decision == "NO-GO" and any(NO_GO_FORBIDDEN.search(_norm(t)) for t in texts):
        return {"mode": "template", "status": "rejeté : baisse de prix présentée comme une voie de réalisation sous un NO-GO",
                "model": client.model}
    synthesis, cut = limit_lines(synthesis, cfg.max_text_lines)
    # lignes de la note écrites par le LLM ; les lignes obligatoires (prix, vendeur, réserves...) viennent du code et
    # sont remplacées par celles du code à la fusion : elles ne comptent pas dans la limite
    from report.note_go_nogo import is_mandatory
    free = [i for i, x in enumerate(lines) if not is_mandatory(x)]
    if len(free) > cfg.max_text_lines:
        keep = set(free[:cfg.max_text_lines])
        lines, cut = [x for i, x in enumerate(lines) if i in keep], True
    status = "accepté" + (f" (texte ramené à {cfg.max_text_lines} lignes)" if cut else "")
    return {"mode": "llm", "status": status, "model": client.model, "note_lines": lines,
            "answers": {k: str(v) for k, v in answers.items()}, "synthesis": synthesis,
            "arguments_for": [x for x in data.get("arguments_for", []) if isinstance(x, str)],
            "arguments_against": [x for x in data.get("arguments_against", []) if isinstance(x, str)],
            "disagreement": dis.get("reason") if isinstance(dis, dict) and dis.get("disagrees") else None}
