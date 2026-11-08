"""Connecteur ADEME - DPE tertiaire (data-fair), distribution des étiquettes autour du point de référence.

Paramètres lus dans l'OpenAPI officiel (api-docs.json du jeu dpe01tertiaire, 2026-11-08) :
  /lines : size, select, geo_distance = "lon:lat:distance_en_mètres", pagination par l'URL `next`.
Champs vérifiés sur une vraie réponse (tests/fixtures/ademe_sample.json) :
  etiquette_dpe, etiquette_ges, secteur_activite, annee_construction, periode_construction,
  surface_utile, conso_kwhep_m2_an, date_etablissement_dpe ; réponse {"total", "next", "results"}.
Limite officielle : base NON représentative du parc. Le libellé exact du secteur « bureaux » n'est
pas encore confirmé : la répartition est donc donnée tous secteurs ET par secteur.
"""
from __future__ import annotations

from collections import Counter
from statistics import median

from tools.base import ParseError
from tools.common import make_point, require_client, require_location
from tools.connectors import connector

FIELDS = ["etiquette_dpe", "etiquette_ges", "secteur_activite", "annee_construction", "surface_utile",
          "conso_kwhep_m2_an", "date_etablissement_dpe"]
CLASSES = ["A", "B", "C", "D", "E", "F", "G"]
PAGE_SIZE = 1000
MAX_PAGES = 10


def summarise(rows: list[dict], radius_m: int) -> dict:
    labels = Counter(r.get("etiquette_dpe") for r in rows if r.get("etiquette_dpe") in CLASSES)
    n = sum(labels.values())
    conso = [float(r["conso_kwhep_m2_an"]) for r in rows if isinstance(r.get("conso_kwhep_m2_an"), (int, float))]
    years = [int(r["annee_construction"]) for r in rows if isinstance(r.get("annee_construction"), int)]
    sectors = Counter(r.get("secteur_activite") or "non renseigné" for r in rows)
    by_sector = {}
    for sector, _count in sectors.most_common(8):
        c = Counter(r.get("etiquette_dpe") for r in rows
                    if (r.get("secteur_activite") or "non renseigné") == sector and r.get("etiquette_dpe") in CLASSES)
        by_sector[sector] = {k: c.get(k, 0) for k in CLASSES}
    return {
        "radius_m": radius_m, "n_dpe": len(rows), "n_with_label": n,
        "by_class": {k: labels.get(k, 0) for k in CLASSES},
        "share_b_or_better": round((labels["A"] + labels["B"]) / n, 4) if n else None,
        "share_f_or_worse": round((labels["F"] + labels["G"]) / n, 4) if n else None,
        "median_kwhep_m2_year": median(conso) if conso else None,
        "median_construction_year": median(years) if years else None,
        "by_sector": by_sector,
    }


@connector("ademe_dpe_tertiaire", needs_location=True)
def fetch_dpe(ctx, needs):
    source = ctx.catalog.by_id("ademe_dpe_tertiaire")
    loc = require_location(ctx, source)
    client = require_client(ctx, source)
    radius = ctx.defaults.research.radius_m[0]
    params = {"geo_distance": f"{loc['lon']}:{loc['lat']}:{radius}", "size": PAGE_SIZE, "select": ",".join(FIELDS)}
    rows, url, r, pages = [], source.url, None, 0
    while url and pages < MAX_PAGES:
        r = client.get(source.id, url, params)
        data = r.json()
        if "results" not in data:
            raise ParseError("réponse data-fair : clé results absente")
        rows += data["results"]
        url, params, pages = data.get("next"), None, pages + 1   # `next` contient déjà tous les paramètres
    total = data.get("total")
    truncated = total is not None and len(rows) < total
    if not rows:
        raise ParseError(f"aucun DPE tertiaire dans un rayon de {radius} m")
    value = summarise(rows, radius)
    s = value["share_b_or_better"]
    notes = (f"{len(rows)} DPE tertiaires établis depuis juillet 2021 dans un rayon de {radius} m"
             + (f" (sur {total}, pagination tronquée)" if truncated else "")
             + (f" ; part des étiquettes A-B : {s:.0%}" if s is not None else "")
             + " ; tous secteurs d'activité (bureaux non isolés, libellé à confirmer) ; "
               "avertissement ADEME : base non représentative du parc")
    return [make_point(source, n.key, value, None, r.retrieved_at[:10], r, notes, confidence="medium",
                       status="partial" if truncated else "ok") for n in needs]
