"""Sonde n°2 : lève les incertitudes restantes avant d'écrire les derniers connecteurs.

    python scripts/probe_round2.py

Chaque requête répond à UNE question précise (voir `question`) ; les réponses brutes sont
enregistrées dans cache/probe/<horodatage>_round2/ avec _summary.json.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_sources import ROOT, ext_for, fetch  # noqa: E402


def main() -> int:
    cfg = json.loads((ROOT / "asset_config.json").read_text(encoding="utf-8"))
    out = ROOT / "cache" / "probe" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_round2")
    out.mkdir(parents=True, exist_ok=True)
    summary: list[dict] = []

    def probe(pid, question, url, params=None):
        t0 = time.time()
        e = {"id": pid, "question": question, "requested": url, "params": params}
        try:
            status, ctype, body, final = fetch(url, params, timeout=60)
            name = f"{pid}.{ext_for(ctype, final)}"
            (out / name).write_bytes(body)
            e.update(status=status, content_type=ctype, bytes=len(body), file=name, url=final)
        except Exception as exc:  # noqa: BLE001
            e.update(status=None, error=f"{type(exc).__name__}: {exc}")
            body = None
        e["seconds"] = round(time.time() - t0, 2)
        summary.append(e)
        ok = e.get("status") and e["status"] < 400
        print(f"{'OK ' if ok else 'KO '} {str(e.get('status')):>4}  {pid:<36} {e.get('bytes', e.get('error', ''))}")
        return body if ok else None

    # point de référence (format IGN vérifié en sonde n°1)
    body = probe("ign_search", "géocodage du point de référence", "https://data.geopf.fr/geocodage/search",
                 {"q": cfg["asset"]["reference_point"]["address"], "index": "address", "limit": 1})
    if body is None:
        print("Géocodage impossible : vérifier la connexion internet, puis relancer.")
        return 1
    feat = json.loads(body)["features"][0]
    lon, lat = feat["geometry"]["coordinates"]
    citycode = feat["properties"]["citycode"]
    commune = "75056" if citycode.startswith("751") else citycode   # code commune de Paris, à TESTER
    bbox = f"{lon - 0.009:.5f},{lat - 0.006:.5f},{lon + 0.009:.5f},{lat + 0.006:.5f}"

    probe("ign_openapi_yaml", "spécification officielle du géocodage", "https://data.geopf.fr/geocodage/openapi.yaml")

    # BCE : échec SSL en sonde n°1
    probe("ecb_euribor", "format CSV réel de la série Euribor 3M",
          "https://data-api.ecb.europa.eu/service/data/FM/M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA",
          {"format": "csvdata", "startPeriod": "2025-01"})

    # DVF : quels codes de typologie pour les bureaux / locaux d'activité ?
    cer = "https://apidf-preprod.cerema.fr/dvf_opendata"
    probe("dvf_geomut_bbox_500", "liste des typologies (codtypbien/libtypbien) autour du point",
          f"{cer}/geomutations/", {"in_bbox": bbox, "anneemut_min": 2021, "page_size": 500})
    probe("dvf_geomut_codtypbien_14", "le filtre codtypbien=14 renvoie-t-il des locaux d'activité ?",
          f"{cer}/geomutations/", {"in_bbox": bbox, "anneemut_min": 2021, "codtypbien": "14", "page_size": 100})
    probe("dvf_mutations_bbox", "la route mutations répond-elle (timeout en sonde n°1) ?",
          f"{cer}/mutations/", {"in_bbox": bbox, "anneemut_min": 2021, "page_size": 20})

    # Firmio (data.gouv.fr) : fiche du jeu de données, puis le dernier CSV qu'elle référence
    meta = probe("firmio_dataset", "fiche data.gouv.fr : URL et date du dernier CSV",
                 "https://www.data.gouv.fr/api/1/datasets/prix-de-limmobilier-des-locaux-dentreprises-par-commune-dapres-dvf/")
    if meta:
        csvs = [r for r in json.loads(meta).get("resources", []) if str(r.get("format", "")).lower() == "csv"]
        if csvs:
            probe("firmio_csv", "colonnes du CSV ; Paris par arrondissement (751xx) ou en une ligne (75056) ?",
                  csvs[0]["url"])

    # Géorisques : 0 résultat avec 75109 en sonde n°1
    geo = "https://georisques.gouv.fr/api/v1"
    probe("geo_catnat_75056", "arrêtés CatNat avec le code commune de Paris", f"{geo}/gaspar/catnat",
          {"code_insee": commune, "page_size": 10})
    probe("geo_catnat_latlon", "arrêtés CatNat par coordonnées", f"{geo}/gaspar/catnat",
          {"latlon": f"{lon},{lat}", "rayon": 1000, "page_size": 10})
    probe("geo_risques_latlon", "types de risques par coordonnées", f"{geo}/gaspar/risques",
          {"latlon": f"{lon},{lat}", "rayon": 1000})
    probe("geo_risques_75056", "types de risques avec le code commune", f"{geo}/gaspar/risques", {"code_insee": commune})
    probe("geo_rapport_latlon", "synthèse des risques au point", f"{geo}/resultats_rapport_risque",
          {"latlon": f"{lon},{lat}"})

    # Paris Open Data : syntaxes de filtre utilisées par les connecteurs
    ods = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets"
    arr = int(citycode[3:]) if citycode.startswith("751") else None
    probe("paris_pvp_within_500", "within_distance fonctionne-t-il (bâtiments protégés, 500 m) ?",
          f"{ods}/plub_surf_pvp/records",
          {"where": f"within_distance(geo_point_2d, geom'POINT({lon} {lat})', 500m)", "limit": 5})
    probe("paris_pvp_arr", "filtre n_sq_ca=<arrondissement> (comptage)", f"{ods}/plub_surf_pvp/records",
          {"where": f"n_sq_ca={arr}", "limit": 1})
    probe("paris_ecm_arr_page", "pagination limit/offset sur plub_ecm filtré", f"{ods}/plub_ecm/records",
          {"where": f"n_sq_ca={arr}", "select": "hauteur,emprise,n_sq_ca", "limit": 100, "offset": 0})

    # ADEME : libellés de secteur et filtre géographique
    ad = "https://data.ademe.fr/data-fair/api/v1/datasets/dpe01tertiaire"
    probe("ademe_values_secteur", "libellés exacts de secteur_activite (bureaux ?)", f"{ad}/values/secteur_activite",
          {"size": 100})
    probe("ademe_geo_500", "geo_distance lon:lat:500 filtre-t-il bien ?", f"{ad}/lines",
          {"geo_distance": f"{lon}:{lat}:500", "size": 5,
           "select": "etiquette_dpe,secteur_activite,_geopoint"})
    probe("ademe_agg_500", "forme de la réponse values_agg", f"{ad}/values_agg",
          {"field": "etiquette_dpe", "geo_distance": f"{lon}:{lat}:500"})

    (out / "_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for s in summary if s.get("status") and s["status"] < 400)
    print(f"\n{ok}/{len(summary)} sondes OK. Résultats : {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
