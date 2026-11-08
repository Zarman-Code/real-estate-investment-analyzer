"""Sonde des sources de données (Phase 2, règle 5 : ne jamais deviner un endpoint ou un champ).

À lancer sur une machine qui a accès à internet :

    python scripts/probe_sources.py

Le script télécharge, pour chaque source :
  - sa documentation machine quand elle existe (OpenAPI / métadonnées de jeu de données) ;
  - un petit échantillon de VRAIE réponse.
Tout est enregistré tel quel dans cache/probe/<horodatage>/, avec un résumé _summary.json.
Ces fichiers servent ensuite à écrire et tester le parsing de chaque connecteur.

Aucune dépendance : bibliothèque standard Python uniquement.
Les URL marquées "candidate" sont des emplacements plausibles de documentation : un échec
est normal et simplement consigné.
"""
from __future__ import annotations

import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_BYTES = 25 * 1024 * 1024
UA = "helios-agent-probe/0.3"


def ssl_context() -> ssl.SSLContext:
    """Sous Windows, le Python de python.org ne voit pas toujours les certificats du système
    (erreur CERTIFICATE_VERIFY_FAILED constatée sur la BCE) : on utilise certifi s'il est installé."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL = ssl_context()


def fetch(url: str, params: dict | None = None, timeout: int = 40) -> tuple[int, str | None, bytes, str]:
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params, safe=',')}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL) as r:
            return r.status, r.headers.get("Content-Type"), r.read(MAX_BYTES), url
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type") if e.headers else None, e.read(200_000), url


def ext_for(ctype: str | None, url: str) -> str:
    c = (ctype or "").lower()
    # « spreadsheet » AVANT « xml » : le type MIME d'un .xlsx contient « openxmlformats »
    if "spreadsheet" in c or url.endswith(".xlsx"):
        return "xlsx"
    if "json" in c:
        return "json"
    if "csv" in c:
        return "csv"
    if "yaml" in c or url.endswith((".yaml", ".yml")):
        return "yaml"
    if "xml" in c:
        return "xml"
    if "html" in c:
        return "html"
    return "bin"


def main() -> int:
    cfg = json.loads((ROOT / "asset_config.json").read_text(encoding="utf-8"))
    ref_address = cfg["asset"]["reference_point"]["address"]
    out = ROOT / "cache" / "probe" / datetime.now().strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    summary: list[dict] = []

    def probe(pid: str, url: str, params: dict | None = None, kind: str = "sample") -> bytes | None:
        t0 = time.time()
        entry: dict = {"id": pid, "kind": kind, "requested": url, "params": params}
        try:
            status, ctype, body, final_url = fetch(url, params)
            ext = ext_for(ctype, final_url)
            (out / f"{pid}.{ext}").write_bytes(body)
            entry.update(status=status, content_type=ctype, bytes=len(body), file=f"{pid}.{ext}", url=final_url)
        except Exception as exc:  # noqa: BLE001 - une sonde ne doit jamais s'arrêter
            entry.update(status=None, error=f"{type(exc).__name__}: {exc}")
            body = None
        entry["seconds"] = round(time.time() - t0, 2)
        summary.append(entry)
        ok = entry.get("status") and entry["status"] < 400
        print(f"{'OK ' if ok else 'KO '} {str(entry.get('status')):>4}  {pid:<34} {entry.get('bytes', '')}")
        return body if ok else None

    # ---------------------------------------------------------------- IGN géocodage (point de référence)
    probe("ign_openapi", "https://data.geopf.fr/geocodage/openapi", kind="doc")
    body = probe("ign_search", "https://data.geopf.fr/geocodage/search",
                 {"q": ref_address, "index": "address", "limit": 1})
    lon = lat = citycode = postcode = None
    try:  # chaînage exploratoire seulement : le vrai parsing sera écrit après lecture de ce fichier
        feat = json.loads(body)["features"][0]
        lon, lat = feat["geometry"]["coordinates"]
        citycode = feat["properties"].get("citycode")
        postcode = feat["properties"].get("postcode")
    except Exception:  # noqa: BLE001
        print("   ! coordonnées non lues : les sondes géolocalisées sont ignorées")
    if postcode is None:
        m = re.search(r"\b\d{5}\b", ref_address)
        postcode = m.group(0) if m else None
    print(f"   point de référence : lon={lon} lat={lat} citycode={citycode} postcode={postcode}")

    if lon is not None:
        probe("ign_reverse", "https://data.geopf.fr/geocodage/reverse",
              {"lon": lon, "lat": lat, "index": "address", "limit": 5})

    # ---------------------------------------------------------------- BCE / INSEE
    probe("ecb_euribor", "https://data-api.ecb.europa.eu/service/data/FM/M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA",
          {"format": "csvdata", "startPeriod": "2024-01"})
    for pid, idbank in [("insee_ilat", "001617112"), ("insee_icc", "000008630"), ("insee_bt01", "001710986")]:
        # sans detail=dataonly : on veut les attributs (TITLE...) pour CONFIRMER que l'idbank est le bon
        probe(pid, f"https://bdm.insee.fr/series/sdmx/data/SERIES_BDM/{idbank}", {"startPeriod": "2023-01"})

    # ---------------------------------------------------------------- Cerema DVF+ open data
    base = "https://apidf-preprod.cerema.fr"
    probe("cerema_swagger_html", f"{base}/swagger/", kind="doc")
    probe("cerema_openapi_a", f"{base}/swagger/", {"format": "openapi"}, kind="doc-candidate")
    probe("cerema_openapi_b", f"{base}/swagger.json", kind="doc-candidate")
    if citycode:
        probe("cerema_dvf_mutations_commune", f"{base}/dvf_opendata/mutations/",
              {"code_insee": citycode, "anneemut_min": 2021, "page_size": 20, "fields": "all"})
    if lon is not None:
        bbox = f"{lon - 0.009:.5f},{lat - 0.006:.5f},{lon + 0.009:.5f},{lat + 0.006:.5f}"
        probe("cerema_dvf_mutations_bbox", f"{base}/dvf_opendata/mutations/",
              {"in_bbox": bbox, "anneemut_min": 2021, "page_size": 20, "fields": "all"})
        probe("cerema_dvf_geomutations_bbox", f"{base}/dvf_opendata/geomutations/",
              {"in_bbox": bbox, "anneemut_min": 2021, "page_size": 5})

    # ---------------------------------------------------------------- aucadastre (agrégateur tiers)
    if citycode:
        probe("aucadastre_commune", f"https://aucadastre.fr/api/commune/{citycode}")

    # ---------------------------------------------------------------- Paris Open Data (métadonnées = liste des champs)
    ods = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets"
    for ds in ["plub_surf_pvp", "plub_ecm", "autorisations-durbanisme-h"]:
        probe(f"paris_meta_{ds}", f"{ods}/{ds}", kind="doc")
        probe(f"paris_sample_{ds}", f"{ods}/{ds}/records", {"limit": 3})
    if lon is not None:
        probe("paris_within_plub_ecm", f"{ods}/plub_ecm/records",
              {"where": f"within_distance(geo_point_2d, geom'POINT({lon} {lat})', 500m)", "limit": 5})

    # ---------------------------------------------------------------- ADEME DPE tertiaire (data-fair)
    ademe = "https://data.ademe.fr/data-fair/api/v1/datasets/dpe01tertiaire"
    probe("ademe_meta", ademe, kind="doc")
    probe("ademe_apidocs", f"{ademe}/api-docs.json", kind="doc-candidate")
    if postcode:
        probe("ademe_sample", f"{ademe}/lines", {"q": postcode, "size": 5})

    # ---------------------------------------------------------------- Géorisques (chemins à confirmer)
    probe("georisques_doc_html", "https://www.georisques.gouv.fr/doc-api", kind="doc")
    probe("georisques_openapi_a", "https://georisques.gouv.fr/api/v1/api-docs", kind="doc-candidate")
    probe("georisques_openapi_b", "https://www.georisques.gouv.fr/api/v3/api-docs", kind="doc-candidate")
    if citycode:
        probe("georisques_catnat", "https://georisques.gouv.fr/api/v1/gaspar/catnat",
              {"code_insee": citycode, "page_size": 5}, kind="sample-candidate")
        probe("georisques_risques", "https://georisques.gouv.fr/api/v1/gaspar/risques",
              {"code_insee": citycode}, kind="sample-candidate")

    # ---------------------------------------------------------------- ImmoStat (fichier daté)
    probe("immostat_xlsx",
          "https://www.immostat.com/wp-content/uploads/2026/10/2026_10_06-ImmoStatoutputwebindic-1.xlsx")

    (out / "_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for s in summary if s.get("status") and s["status"] < 400)
    print(f"\n{ok}/{len(summary)} sondes OK. Résultats : {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
