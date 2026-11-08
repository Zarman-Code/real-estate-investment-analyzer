"""Connecteur INSEE BDM (SDMX 2.1, StructureSpecificData) : ILAT, ICC, BT01.

Format vérifié sur de vraies réponses (tests/fixtures/insee_*.xml, 2026-11-08) :
<Series IDBANK=... FREQ="T"|"M" TITLE_FR=... LAST_UPDATE=...>
  <Obs TIME_PERIOD="2026-Q2"|"2026-08" OBS_VALUE="138.01" OBS_STATUS="A" .../>
Les observations arrivent de la plus récente à la plus ancienne ; on retrie quand même.
Garde-fou : TITLE_FR doit contenir expect_title_contains (catalogue), sinon l'idbank est faux.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from tools.base import ParseError
from tools.common import as_float, make_point, require_client
from tools.connectors import connector

# besoin du registre -> source du catalogue (une série par source)
SERIES = {"index.ilat": "insee_ilat", "index.icc": "insee_icc", "index.bt01": "insee_bt01"}


def parse_sdmx_series(content: bytes) -> tuple[dict, list[tuple[str, float]]]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ParseError(f"XML SDMX illisible : {exc}") from None
    series = [e for e in root.iter() if e.tag.split("}")[-1] == "Series"]
    if len(series) != 1:
        raise ParseError(f"une série attendue, {len(series)} reçue(s)")
    s = series[0]
    obs = []
    for o in s:
        if o.tag.split("}")[-1] != "Obs":
            continue
        period, value = o.get("TIME_PERIOD"), o.get("OBS_VALUE")
        if period is None or value is None:
            raise ParseError("observation sans TIME_PERIOD ou OBS_VALUE")
        obs.append((period, as_float(value, f"OBS_VALUE {period}")))
    if not obs:
        raise ParseError("série vide")
    obs.sort(key=lambda x: x[0])        # '2026-Q2' et '2026-08' se trient correctement en texte
    return dict(s.attrib), obs


def year_on_year(obs: list[tuple[str, float]], freq: str) -> float | None:
    lag = {"T": 4, "M": 12}.get(freq)
    if lag is None or len(obs) <= lag:
        return None
    last, prev = obs[-1][1], obs[-1 - lag][1]
    return round(last / prev - 1, 6)


def _fetch(ctx, need_key: str):
    source = ctx.catalog.by_id(SERIES[need_key])
    # pas de detail=dataonly : on veut les attributs de la série (TITLE_FR...) pour le garde-fou
    params = {k: v for k, v in source.params.items() if k != "detail"}
    r = require_client(ctx, source).get(source.id, source.url, params)
    attrs, obs = parse_sdmx_series(r.content)
    title = attrs.get("TITLE_FR", "")
    if source.expect_title_contains and source.expect_title_contains not in title:
        raise ParseError(f"série inattendue pour {source.id} : « {title} » (idbank à revoir)")
    freq = attrs.get("FREQ", "")
    yoy = year_on_year(obs, freq)
    period, last = obs[-1]
    value = {"last": last, "period": period, "yoy": yoy, "frequency": freq,
             "series": [{"period": p, "value": v} for p, v in obs]}
    notes = (f"{title} (idbank {attrs.get('IDBANK')}), dernière observation {period} = {last}"
             + (f", variation sur un an {yoy:+.2%}" if yoy is not None else "")
             + f" ; série mise à jour le {attrs.get('LAST_UPDATE')}")
    return make_point(source, need_key, value, "indice", period, r, notes)


def _make(source_id: str, key: str):
    @connector(source_id)
    def fetch(ctx, needs):
        return [_fetch(ctx, key) for n in needs if n.key == key]
    fetch.__name__ = f"fetch_{source_id}"
    return fetch


for _key, _sid in SERIES.items():
    _make(_sid, _key)
