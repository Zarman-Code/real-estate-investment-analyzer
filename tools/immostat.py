"""Connecteur ImmoStat - fichier Excel des indicateurs du marché tertiaire Île-de-France.

Contenu vérifié sur le vrai fichier (tests/fixtures/immostat.xlsx, édition du 2026-10-06) :
onglet « Classique », en-tête exact [Indicateur, Zone, Periode, Valeur, Tranche, Type],
périodes « T3 2026 » (trimestres) ou entiers (années, ignorées ici).
Pour la zone « Paris Centre Ouest » : Loyer Moyen (Tranche « Seconde main »), Demande placée,
Offre Immédiate, Montant de l'investissement, Prix Moyen de l'investissement Bureaux (en €/m²).
Absents du fichier : loyer prime, taux de vacance, rendement, mesures d'accompagnement.

Le fichier n'écrit pas les unités : elles suivent les conventions de publication d'ImmoStat
et sont signalées comme telles (confiance « medium »).
"""
from __future__ import annotations

import io
import re

from tools.base import ParseError
from tools.common import make_point, needs_by_indicator, require_client
from tools.connectors import connector

HEADER = ["Indicateur", "Zone", "Periode", "Valeur", "Tranche", "Type"]

# indicateur du catalogue -> (libellé ImmoStat, tranche, unité, note d'unité)
MAPPING = {
    "average_rent": ("Loyer Moyen", "Seconde main", "€/m²/an",
                     "loyer moyen de SECONDE MAIN : borne basse pour un immeuble restructuré"),
    "take_up": ("Demande placée", None, "m²", "surface placée sur le trimestre"),
    "immediate_supply": ("Offre Immédiate", None, "m²", "offre immédiate (définition ImmoStat)"),
    "investment_volume": ("Montant de l'investissement", None, "M€", "volume investi sur le trimestre"),
    "investment_price_per_sqm": ("Prix Moyen de l'investissement Bureaux (en €/m²)", None, "€/m²",
                                 "prix moyen des transactions d'investissement en bureaux"),
}


def load_rows(content: bytes) -> list[tuple]:
    try:
        import openpyxl
    except ImportError as exc:  # dépendance déclarée dans requirements.txt
        raise ParseError("openpyxl n'est pas installé (pip install -r requirements.txt)") from exc
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - fichier corrompu, page HTML à la place de l'Excel...
        raise ParseError(f"fichier Excel illisible : {type(exc).__name__}") from None
    if "Classique" not in wb.sheetnames:
        raise ParseError(f"onglet « Classique » absent (onglets : {wb.sheetnames})")
    rows = list(wb["Classique"].iter_rows(values_only=True))
    if not rows or [str(c).strip() if c is not None else None for c in rows[0][:6]] != HEADER:
        raise ParseError(f"en-tête inattendu : {rows[0][:6] if rows else None} (attendu {HEADER})")
    return rows[1:]


def quarter_key(period) -> tuple[int, int] | None:
    m = re.fullmatch(r"T([1-4]) (\d{4})", str(period).strip())
    return (int(m[2]), int(m[1])) if m else None


def series(rows, label: str, zone: str, tranche: str | None) -> list[tuple[tuple[int, int], float]]:
    out = []
    for ind, z, period, value, tr, _type in (r[:6] for r in rows):
        q = quarter_key(period)
        if ind == label and z == zone and q and (tranche is None or tr == tranche) and isinstance(value, (int, float)):
            out.append((q, float(value)))
    return sorted(out)


@connector("immostat")
def fetch_immostat(ctx, needs):
    source = ctx.catalog.by_id("immostat")
    submarket = ctx.config.asset.submarket
    zone = ctx.defaults.submarket_zones.get(submarket or "", {}).get("immostat")
    if zone is None:
        raise ParseError(f"sous-marché {submarket!r} sans correspondance ImmoStat dans defaults.yaml/submarket_zones")
    wanted = needs_by_indicator(needs, MAPPING)
    r = require_client(ctx, source).get(source.id, source.url)
    rows = load_rows(r.content)
    zones = {row[1] for row in rows}
    if zone not in zones:
        raise ParseError(f"zone {zone!r} absente du fichier (zones : {sorted(z for z in zones if z)})")

    points = []
    for key, indicator in wanted.items():
        label, tranche, unit, what = MAPPING[indicator]
        s = series(rows, label, zone, tranche)
        if not s:
            raise ParseError(f"série « {label} » / {zone} absente")
        (year, q), last = s[-1]
        yoy = None
        prev = dict(s).get((year - 1, q))
        if prev:
            yoy = round(last / prev - 1, 6)
        last4 = [v for _, v in s[-4:]]
        value = {"last": round(last, 2), "period": f"{year}-T{q}", "yoy": yoy,
                 "last_4_quarters": [round(v, 2) for v in last4],
                 "series": [{"period": f"{y}-T{qq}", "value": round(v, 2)} for (y, qq), v in s]}
        if indicator in ("take_up", "investment_volume"):
            value["rolling_12m"] = round(sum(last4), 2) if len(last4) == 4 else None
        notes = (f"ImmoStat, zone « {zone} » utilisée pour le sous-marché « {submarket} » (correspondance "
                 f"approximative, voir defaults.yaml) ; {what} ; unité {unit} selon les conventions ImmoStat "
                 f"(non écrite dans le fichier) ; T{q} {year} = {last:,.0f}"
                 + (f", {yoy:+.1%} sur un an" if yoy is not None else ""))
        points.append(make_point(source, key, value, unit, f"{year}-T{q}", r, notes, confidence="medium"))
    return points
