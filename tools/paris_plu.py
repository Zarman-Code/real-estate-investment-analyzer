"""Connecteurs Paris Open Data (Opendatasoft Explore v2.1) - PLU bioclimatique.

Format vérifié sur de vraies réponses et métadonnées (tests/fixtures/paris_sample_*.json, 2026-11-08) :
GET .../catalog/datasets/<id>/records -> {"total_count": n, "results": [...]}
  plub_ecm      : hauteur (0 si non indiquée sur le plan), emprise (0 si non indiquée), n_sq_ca (n° d'arrondissement), geo_point_2d
  plub_surf_pvp : n_sq_ca, st_area_shape, geo_point_2d
Le filtre géographique within_distance n'est pas encore validé (0 résultat lors de la sonde) :
en attendant, le périmètre est l'ARRONDISSEMENT du point de référence (filtre n_sq_ca, champ vérifié),
et le DataPoint le dit.
"""
from __future__ import annotations

from statistics import median

from tools.base import ParseError
from tools.common import make_point, require_client, require_fields, require_location
from tools.connectors import connector
from tools.http import SourceUnavailable

PAGE = 100          # maximum par appel indiqué au catalogue
MAX_RECORDS = 5000  # garde-fou de pagination


def paris_arrondissement(loc: dict, source) -> int:
    code = str(loc.get("citycode", ""))
    if len(code) != 5 or not code.startswith("751"):
        raise SourceUnavailable(source.id, source.url or "", f"source Paris uniquement (code INSEE {code!r})")
    n = int(code[3:])
    if not 1 <= n <= 20:
        raise ParseError(f"code INSEE parisien inattendu : {code}")
    return n


def fetch_all(client, source, where: str, select: str) -> tuple[list[dict], object]:
    records, offset, last = [], 0, None
    while True:
        r = client.get(source.id, source.url, {"where": where, "select": select, "limit": PAGE, "offset": offset})
        data = r.json()
        if "results" not in data or "total_count" not in data:
            raise ParseError("réponse Opendatasoft : clés total_count/results absentes")
        records += data["results"]
        last = r
        offset += PAGE
        if offset >= data["total_count"] or not data["results"] or offset >= MAX_RECORDS:
            return records, last


@connector("paris_plu_emprises", needs_location=True)
def fetch_plu_emprises(ctx, needs):
    source = ctx.catalog.by_id("paris_plu_emprises")
    arr = paris_arrondissement(require_location(ctx, source), source)
    recs, r = fetch_all(require_client(ctx, source), source, f"n_sq_ca={arr}", "hauteur,emprise,n_sq_ca")
    for rec in recs:
        require_fields(rec, ["hauteur", "emprise"], "plub_ecm")
    heights = [float(x["hauteur"]) for x in recs if x["hauteur"]]       # 0 = non indiquée sur le plan
    footprints = [float(x["emprise"]) for x in recs if x["emprise"]]
    value = {"arrondissement": arr, "n_polygons": len(recs),
             "n_with_height": len(heights),
             "max_height": max(heights) if heights else None,
             "median_height": median(heights) if heights else None,
             "n_with_footprint": len(footprints),
             "median_footprint_pct": median(footprints) if footprints else None}
    notes = (f"PLU bioclimatique, emprises constructibles maximales, {arr}e arrondissement (périmètre "
             f"arrondissement : filtre par rayon non validé) ; {len(recs)} polygones dont {len(heights)} avec "
             "une hauteur indiquée (unité non précisée par le jeu de données) ; contrainte réglementaire générale, "
             "PAS la règle applicable à la parcelle : ne confirme ni l'extension ni le rooftop")
    return [make_point(source, n.key, value, None, r.retrieved_at[:10], r, notes, confidence="medium")
            for n in needs]


@connector("paris_plu_protections", needs_location=True)
def fetch_plu_protections(ctx, needs):
    source = ctx.catalog.by_id("paris_plu_protections")
    arr = paris_arrondissement(require_location(ctx, source), source)
    recs, r = fetch_all(require_client(ctx, source), source, f"n_sq_ca={arr}", "n_sq_ca,st_area_shape")
    areas = [float(x["st_area_shape"]) for x in recs if x.get("st_area_shape") is not None]
    value = {"arrondissement": arr, "count": len(recs), "total_area_sqm": round(sum(areas), 1)}
    notes = (f"PLU bioclimatique, bâtiments protégés (protections patrimoniales), {arr}e arrondissement : "
             f"{len(recs)} emprises protégées ; indicateur de contexte, ne dit pas si l'immeuble lui-même est protégé")
    return [make_point(source, n.key, value, "bâtiments", r.retrieved_at[:10], r, notes, confidence="medium")
            for n in needs]
