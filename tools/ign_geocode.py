"""Connecteur IGN Géoplateforme - géocodage du point de référence.

Format vérifié sur une vraie réponse (tests/fixtures/ign_search.json, 2026-11-08) :
GeoJSON FeatureCollection ; features[0].geometry.coordinates = [lon, lat] ;
properties : label, score, citycode (code INSEE, 75109 pour Paris 9e), postcode, district, type.
"""
from __future__ import annotations

from tools.base import ParseError
from tools.common import LOCATION_KEY, as_float, make_point, require_client, require_fields
from tools.connectors import connector


@connector("ign_geocodage")
def geocode_reference_point(ctx, needs):
    source = ctx.catalog.by_id("ign_geocodage")
    ref = ctx.config.asset.reference_point
    r = require_client(ctx, source).get(source.id, source.url, {"q": ref.address, "index": "address", "limit": 1})
    data = r.json()
    if data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
        raise ParseError("réponse IGN : FeatureCollection attendue")
    if not data["features"]:
        raise ParseError(f"adresse introuvable : {ref.address!r}")
    feat = data["features"][0]
    props = feat.get("properties", {})
    require_fields(props, ["label", "score", "citycode", "postcode"], "réponse IGN")
    coords = (feat.get("geometry") or {}).get("coordinates")
    if not (isinstance(coords, list) and len(coords) == 2):
        raise ParseError("réponse IGN : coordonnées absentes")
    lon, lat = as_float(coords[0], "longitude"), as_float(coords[1], "latitude")
    score = as_float(props["score"], "score")

    min_score = ctx.defaults.quality_checks.geocoding_min_score
    low = score < min_score
    value = {"lon": lon, "lat": lat, "citycode": props["citycode"], "postcode": props["postcode"],
             "label": props["label"], "district": props.get("district"), "score": round(score, 3),
             "match_type": props.get("type")}
    notes = (f"géocodage de « {ref.label} » ({ref.address}) -> « {props['label']} », score {score:.2f}"
             + (f" INFÉRIEUR au seuil {min_score} : vérifier l'adresse" if low else ""))
    # data_date : date de la récupération (la BAN est mise à jour en continu)
    return [make_point(source, LOCATION_KEY, value, None, r.retrieved_at[:10], r, notes,
                       confidence="low" if low else "high", status="partial" if low else "ok")]
