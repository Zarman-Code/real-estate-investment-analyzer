"""Connecteur DVF+ open data (Cerema, API données foncières) - prix au m² des locaux d'activité.

Vérifié sur de vraies réponses (tests/fixtures/dvf_*.json, 2026-11-08) :
GET https://apidf-preprod.cerema.fr/dvf_opendata/mutations/ ?in_bbox=lonmin,latmin,lonmax,latmax
    &anneemut_min=AAAA &codtypbien=14 &page_size=N  -> {"count", "next", "previous", "results": [...]}
Champs : datemut, anneemut, libnatmut (« Vente »...), vefa, valeurfonc (texte décimal), sbati (texte),
codtypbien / libtypbien (14 = « ACTIVITE »), nblocmut, idmutinvar.
Pièges constatés : liens `next` en http:// (réécrits en https://) ; serveur de préproduction lent
(délais dépassés) ; in_bbox limité à 0,02° de côté.

Traitement (plan, section 3.2) : ventes seulement, hors VEFA, typologie « activité », surface bâtie > 0,
prix/m² hors percentiles 5-95 écartés ; médiane, quartiles, effectif, médianes annuelles, sous-ensemble
des grandes surfaces. DVF ne donne PAS de loyers et ne distingue pas bureaux et commerces.
"""
from __future__ import annotations

import math
from collections import defaultdict
from statistics import median, quantiles

from tools.base import ParseError
from tools.common import as_float, make_point, require_client, require_location
from tools.connectors import connector

PAGE_SIZE = 200
MAX_PAGES = 30


def bbox_for(lon: float, lat: float, radius_m: float) -> tuple[float, float, float, float]:
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * math.cos(math.radians(lat)))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def largest_radius_within(lat: float, radii: list[int], max_deg: float) -> int:
    ok = [r for r in radii if 2 * r / (111_320 * math.cos(math.radians(lat))) <= max_deg]
    if not ok:
        raise ParseError(f"aucun rayon {radii} ne tient dans la limite in_bbox de {max_deg}°")
    return max(ok)


def clean(rows: list[dict], filters) -> tuple[list[dict], dict]:
    kept, dropped = [], defaultdict(int)
    for row in rows:
        if row.get("libnatmut") not in filters.sale_natures:
            dropped["nature"] += 1
        elif filters.exclude_vefa and row.get("vefa"):
            dropped["vefa"] += 1
        elif str(row.get("codtypbien")) not in filters.property_type_codes:
            dropped["typologie"] += 1
        else:
            s, v = as_float(row.get("sbati"), "sbati"), as_float(row.get("valeurfonc"), "valeurfonc")
            if s <= 0 or v <= 0:
                dropped["surface_ou_valeur_nulle"] += 1
            else:
                kept.append({"date": row["datemut"], "year": int(row["anneemut"]), "value": v, "sbati": s,
                             "price_per_sqm": v / s, "lots": row.get("nblocmut"), "id": row.get("idmutinvar")})
    seen, unique = set(), []
    for k in kept:                                           # une mutation n'est comptée qu'une fois
        if k["id"] not in seen:
            seen.add(k["id"]); unique.append(k)
    dropped["doublon"] += len(kept) - len(unique)
    if len(unique) >= 20:                                    # écrêtage seulement si l'échantillon le permet
        q = quantiles([k["price_per_sqm"] for k in unique], n=20)
        lo, hi = q[0], q[-1]
        trimmed = [k for k in unique if lo <= k["price_per_sqm"] <= hi]
        dropped["hors_p5_p95"] += len(unique) - len(trimmed)
        unique = trimmed
    return unique, dict(dropped)


def stats(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    p = sorted(k["price_per_sqm"] for k in rows)
    q = quantiles(p, n=4) if len(p) >= 4 else [p[0], median(p), p[-1]]
    return {"median": round(median(p)), "q1": round(q[0]), "q3": round(q[2]), "n": len(p)}


@connector("dvf", needs_location=True)
def fetch_dvf(ctx, needs):
    source = ctx.catalog.by_id("dvf")
    filters = ctx.defaults.dvf
    if filters is None:
        raise ParseError("filtres DVF absents de defaults.yaml")
    loc = require_location(ctx, source)
    radius = largest_radius_within(loc["lat"], ctx.defaults.research.radius_m, filters.max_bbox_deg)
    box = ",".join(f"{c:.5f}" for c in bbox_for(loc["lon"], loc["lat"], radius))
    year_min = str(ctx.defaults.research.transactions_from)[:4]
    client = require_client(ctx, source)
    url, params = source.url, {"in_bbox": box, "anneemut_min": year_min,
                               "codtypbien": ",".join(filters.property_type_codes), "page_size": PAGE_SIZE}
    rows, pages, count, r = [], 0, None, None
    while url and pages < MAX_PAGES:
        r = client.get(source.id, url, params)
        data = r.json()
        if "results" not in data or "count" not in data:
            raise ParseError("réponse Cerema : clés count/results absentes")
        rows += data["results"]
        count = data["count"]
        url = data.get("next")
        if url and url.startswith("http://"):
            url = "https://" + url[len("http://"):]          # le serveur renvoie des liens http
        params, pages = None, pages + 1                       # `next` porte déjà les paramètres
    truncated = count is not None and len(rows) < count
    kept, dropped = clean(rows, filters)
    if not kept:
        raise ParseError(f"aucune transaction exploitable (reçues {len(rows)}, écartées {dropped})")
    by_year = defaultdict(list)
    for k in kept:
        by_year[k["year"]].append(k["price_per_sqm"])
    large = [k for k in kept if k["sbati"] >= filters.large_lot_min_sqm]
    value = stats(kept) | {
        "radius_m": radius, "bbox": box, "since": year_min, "received": len(rows), "count_api": count,
        "dropped": dropped, "by_year": {y: {"median": round(median(v)), "n": len(v)} for y, v in sorted(by_year.items())},
        "large_lots": stats(large) | {"min_sqm": filters.large_lot_min_sqm} if large else None,
    }
    last_date = max(k["date"] for k in kept)
    notes = (f"DVF+ open data, locaux d'ACTIVITÉ (codtypbien {filters.property_type_codes}), ventes hors VEFA depuis "
             f"{year_min}, carré de ~{2 * radius} m de côté autour du point de référence ; {len(kept)} transactions "
             f"retenues sur {len(rows)} reçues ({dropped}) ; mélange bureaux et commerces, état des immeubles inconnu, "
             "pas de loyers" + (" ; PAGINATION TRONQUÉE" if truncated else ""))
    conf = "low" if len(kept) < 5 else "medium"
    return [make_point(source, n.key, value, "€/m²", last_date, r, notes, confidence=conf,
                       status="partial" if truncated else "ok") for n in needs]
