"""Formatage français des chiffres affichés (et seule source des formats : le contrôle des nombres
du texte rédigé par le LLM s'appuie sur les mêmes conventions)."""
from __future__ import annotations


def _fr(x: float, decimals: int) -> str:
    s = f"{x:,.{decimals}f}"
    return s.replace(",", " ").replace(".", ",")


def pct(v: float | None, decimals: int = 1) -> str:
    return "n.d." if v is None else f"{_fr(v * 100, decimals)} %"


def pts(v: float | None, decimals: int = 1) -> str:
    return "n.d." if v is None else f"{_fr(v, decimals)} pt"


def bps(v: float | None) -> str:
    return "n.d." if v is None else f"{_fr(v, 0)} pb"


def meur(v: float | None, decimals: int = 1) -> str:
    return "n.d." if v is None else f"{_fr(v / 1e6, decimals)} M€"


def eur_sqm(v: float | None) -> str:
    return "n.d." if v is None else f"{_fr(v, 0)} €/m²"


def mult(v: float | None) -> str:
    return "n.d." if v is None else f"{_fr(v, 2)}x"
