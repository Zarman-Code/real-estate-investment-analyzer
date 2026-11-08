"""Connecteur BCE - Euribor 3 mois (série FM.M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA, format csvdata).

Format vérifié sur une vraie réponse (tests/fixtures/ecb_euribor.csv, 2026-11-08) :
CSV séparé par des virgules, une ligne par mois ; colonnes utilisées : TIME_PERIOD (« 2026-10 »),
OBS_VALUE, TITLE (« Euribor 3-month - Historical close, average of observations through period »),
UNIT = « PCPA » (POURCENTAGE annuel) -> 2.63 signifie 2,63 % et devient 0.0263 dans le registre.
Garde-fous : titre de série attendu, unité connue, une seule série.
"""
from __future__ import annotations

import csv
import io

from tools.base import ParseError
from tools.common import as_float, make_point, require_client
from tools.connectors import connector

UNIT_DIVISOR = {"PCPA": 100.0}      # pourcentage annuel -> fraction


@connector("ecb_euribor")
def fetch_euribor(ctx, needs):
    source = ctx.catalog.by_id("ecb_euribor")
    r = require_client(ctx, source).get(source.id, source.url, dict(source.params))
    try:
        rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
    except UnicodeDecodeError:
        raise ParseError("réponse BCE non UTF-8") from None
    if not rows or not {"TIME_PERIOD", "OBS_VALUE", "UNIT", "TITLE"} <= rows[0].keys():
        raise ParseError(f"colonnes BCE inattendues : {list(rows[0].keys())[:12] if rows else 'réponse vide'}")
    if len({row.get("KEY") for row in rows}) != 1:
        raise ParseError("plusieurs séries dans la réponse")
    title, unit = rows[0]["TITLE"], rows[0]["UNIT"]
    if source.expect_title_contains and source.expect_title_contains not in title:
        raise ParseError(f"série inattendue : « {title} »")
    if unit not in UNIT_DIVISOR:
        raise ParseError(f"unité inconnue {unit!r} : conversion impossible sans risque")
    div = UNIT_DIVISOR[unit]
    series = sorted((row["TIME_PERIOD"], as_float(row["OBS_VALUE"], row["TIME_PERIOD"]) / div)
                    for row in rows if row["OBS_VALUE"] not in ("", None))
    if not series:
        raise ParseError("série vide")
    period, last = series[-1]
    last12 = [v for _, v in series[-12:]]
    # aucune valeur arrondie dans les données : l'arrondi est réservé à l'affichage
    value = {"last": last, "period": period, "avg_12m": sum(last12) / len(last12),
             "min_12m": min(last12), "max_12m": max(last12),
             "series": [{"period": p, "value": v} for p, v in series]}
    notes = (f"{title} ; {period} = {last:.4%} (valeur publiée {last * div:.4f} en {unit}, convertie en fraction) ; "
             "moyenne MENSUELLE historique, pas une prévision ; vérifier la convention du contrat de crédit")
    return [make_point(source, n.key, value, "fraction", period, r, notes) for n in needs]
