"""Exécute le graphe complet sur les VRAIES réponses enregistrées des sources (tests/fixtures), sans réseau."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx

import tools.sources  # noqa: F401
from agent.graph import build_graph, recursion_limit
from agent.state import build_initial_state
from config.schema import load_config, load_defaults, load_investor_targets, load_sources_catalog
from tools.connectors import CONNECTORS
from tools.http import HttpClient

FIX = Path(__file__).parent / "fixtures"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


def fixture_handler(req: httpx.Request) -> httpx.Response:
    u = str(req.url)
    if "geopf.fr" in u:
        return httpx.Response(200, content=fixture("ign_search.json"))
    for idb, f in (("001617112", "insee_ilat.xml"), ("000008630", "insee_icc.xml"), ("001710986", "insee_bt01.xml")):
        if idb in u:
            return httpx.Response(200, content=fixture(f))
    if "immostat" in u:
        return httpx.Response(200, content=fixture("immostat.xlsx"), headers={"content-type": XLSX})
    for ds, f in (("plub_ecm", "paris_sample_plub_ecm.json"), ("plub_surf_pvp", "paris_sample_plub_surf_pvp.json")):
        if ds in u:
            page = json.loads(fixture(f))
            page["total_count"] = len(page["results"])
            return httpx.Response(200, json=page)
    if "ademe" in u:
        return httpx.Response(200, json=json.loads(fixture("ademe_sample.json")) | {"next": None})
    if "data-api.ecb.europa.eu" in u:
        return httpx.Response(200, content=fixture("ecb_euribor.csv"), headers={"content-type": "text/csv"})
    if "api/1/datasets" in u:
        return httpx.Response(200, content=fixture("firmio_dataset.json"))
    if u.endswith(".csv"):
        return httpx.Response(200, content=fixture("firmio_csv.csv"), headers={"content-type": "text/csv"})
    if "apidf-preprod.cerema.fr" in u:
        rows = [f["properties"] for f in json.loads(fixture("dvf_geomut_codtypbien_14.json"))["features"]]
        return httpx.Response(200, json={"count": len(rows), "next": None, "results": rows})
    return httpx.Response(404)


def run_on_fixtures(tmp_path, llm=None, config=None):
    client = HttpClient(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(fixture_handler), max_retries=0)
    d = load_defaults()
    st = build_initial_state(config or load_config(), d, load_investor_targets(), load_sources_catalog(),
                             analysis_date=date(2026, 11, 8))
    return build_graph(CONNECTORS, client, llm).invoke(st, config={"recursion_limit": recursion_limit(2)})
