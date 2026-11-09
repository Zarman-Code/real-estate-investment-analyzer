"""Phrase chiffrée « traçable » : un texte (str, inchangé pour la note et le LLM) qui garde aussi son gabarit
et, pour chaque chiffre, la valeur brute, le format et la clé de la donnée dont il provient.

Le dashboard affiche le gabarit en rendant chaque chiffre par fmt() avec sa pastille de source (règle :
aucun chiffre sans source ni hypothèse). Le texte, lui, est identique à ce qu'il était.

    P("TRI de {0} pour un objectif de {1}", S(0.12, "pct", "financials.irr_equity"), S(0.15, "pct", "targets.min_levered_irr_pct"))
"""
from __future__ import annotations

from typing import Any

from decision import fmt

FORMATS = {
    "pct": fmt.pct, "pct0": lambda v: fmt.pct(v, 0), "pct2": lambda v: fmt.pct(v, 2), "meur": fmt.meur,
    "sqm": fmt.eur_sqm, "bps": fmt.bps, "pts": fmt.pts, "mult": fmt.mult, "int": lambda v: f"{v:.0f}",
    "bps_signed": lambda v: ("+" if v >= 0 else "") + fmt.bps(v),
    "crit": lambda v: f"{v:.0f}/25",                # criticité = probabilité (1-5) x impact (1-5)
    "dec1": lambda v: fmt._fr(v, 1),
    "prob": lambda v: f"{v:.0f}/5",
    "text": lambda v: str(v),
    "rent": lambda v: fmt.eur_sqm(v) + "/an",
    "pct_signed": lambda v: ("+" if v > 0 else "") + fmt.pct(v),
    "meur2": lambda v: fmt.meur(v, 2),                       # texte cité tel quel (justification produite par les règles)
}


def S(value: Any, format: str, key: str | None = None, field: str | None = None) -> dict:
    """Un chiffre de la phrase : valeur brute, format d'affichage, clé (et champ) de la donnée source si elle existe.
    La clé est une indication : le dashboard vérifie que la donnée vaut bien ce chiffre avant de l'utiliser."""
    if format not in FORMATS:
        raise ValueError(f"format inconnu {format}")
    return {"value": value, "format": format, "key": key, "field": field}


class Phrase(str):
    """`display` : variante du gabarit pour le dashboard (ex. sans « (source, date) », que la pastille montre déjà)."""
    template: str
    slots: list[dict]

    def __new__(cls, template: str, *slots: dict, display: str | None = None) -> "Phrase":
        obj = super().__new__(cls, template.format(*(FORMATS[s["format"]](s["value"]) for s in slots)))
        obj.template, obj.slots = display or template, list(slots)
        return obj


P = Phrase


def as_json(x: Any) -> dict:
    """Forme sérialisable : texte + gabarit + chiffres. Un texte simple a un gabarit sans chiffre."""
    if isinstance(x, Phrase):
        return {"text": str(x), "template": x.template, "slots": x.slots}
    return {"text": str(x), "template": str(x).replace("{", "{{").replace("}", "}}"), "slots": []}


def join(sep: str, parts: list) -> "Phrase | str":
    """Assemble des phrases (traçables ou non) en une seule, en renumérotant leurs chiffres."""
    import re
    tpl, slots = [], []
    for part in parts:
        if isinstance(part, Phrase):
            off = len(slots)
            tpl.append(re.sub(r"\{(\d+)\}", lambda m: "{" + str(int(m.group(1)) + off) + "}", part.template))
            slots += part.slots
        else:
            tpl.append(str(part).replace("{", "{{").replace("}", "}}"))
    if not slots:
        return sep.join(str(p) for p in parts)
    text = sep.join(str(p) for p in parts)
    obj = str.__new__(Phrase, text)
    obj.template, obj.slots = sep.replace("{", "{{").replace("}", "}}").join(tpl), slots
    return obj


def cap(x):
    """Majuscule initiale, en conservant la traçabilité."""
    if not x:
        return x
    if isinstance(x, Phrase):
        obj = str.__new__(Phrase, str(x)[0].upper() + str(x)[1:])
        obj.template, obj.slots = (x.template[0].upper() + x.template[1:]) if not x.template.startswith("{") else x.template, x.slots
        return obj
    return x[0].upper() + x[1:]
