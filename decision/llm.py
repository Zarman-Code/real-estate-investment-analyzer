"""Rédaction par LLM, sous contrôle. Le LLM reçoit la décision et les faits déjà calculés et formatés ;
il renvoie un JSON strict. Le texte n'est ACCEPTÉ que si :
  - le JSON a la forme attendue ;
  - la note fait 5 à 10 lignes et commence par la décision exacte ;
  - CHAQUE nombre du texte correspond à un chiffre des faits (tolérance d'arrondi d'affichage).
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


# ------------------------------------------------------------------ rédaction

def load_prompt(cfg: LlmConfig) -> str:
    return (PROJECT_ROOT / cfg.prompt_file).read_text(encoding="utf-8")


def draft(client: LlmClient | None, cfg: LlmConfig | None, decision: str, payload: dict,
          question_ids: list[str] | None = None) -> dict:
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
    if not (isinstance(lines, list) and all(isinstance(x, str) for x in lines) and 5 <= len(lines) <= 10):
        return {"mode": "template", "status": "rejeté : note_lines absente ou hors de 5 à 10 lignes", "model": client.model}
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
    return {"mode": "llm", "status": "accepté", "model": client.model, "note_lines": lines,
            "answers": {k: str(v) for k, v in answers.items()}, "synthesis": synthesis,
            "arguments_for": [x for x in data.get("arguments_for", []) if isinstance(x, str)],
            "arguments_against": [x for x in data.get("arguments_against", []) if isinstance(x, str)],
            "disagreement": dis.get("reason") if isinstance(dis, dict) and dis.get("disagrees") else None}
