"""Tests des connecteurs sur les VRAIES réponses enregistrées (tests/fixtures), sans réseau.

Pour chaque connecteur : parsing nominal sur la réponse réelle, puis cas de panne / format inattendu
qui doivent produire des DataPoint 'missing' (via run_connector), jamais une valeur inventée.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

import tools.sources  # noqa: F401
from agent.nodes.identify_data_needs import identify_data_needs
from agent.state import build_initial_state
from config.schema import load_config, load_defaults, load_investor_targets, load_sources_catalog
from tools.base import ParseError, run_connector
from tools.connectors import CONNECTORS, ConnectorContext
from tools.http import HttpClient, SourceUnavailable
from tools.registry import DataPoint, DataRegistry

FIX = Path(__file__).parent / "fixtures"
CFG, DEFAULTS, CATALOG = load_config(), load_defaults(), load_sources_catalog()


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


def needs_for(*keys):
    state = build_initial_state(CFG, DEFAULTS, load_investor_targets(), CATALOG, analysis_date=date(2026, 11, 8))
    all_needs = {n.key: n for n in identify_data_needs(state)["data_needs"]}
    return [all_needs[k] for k in keys]


def ctx_with(tmp_path, handler, registry: DataRegistry | None = None, defaults=DEFAULTS, config=CFG):
    client = HttpClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler), max_retries=0)
    return ConnectorContext(config=config, defaults=defaults, catalog=CATALOG, registry=registry or DataRegistry(),
                            analysis_date="2026-11-08", client=client)


def located_registry() -> DataRegistry:
    """Registre contenant le point de référence tel que le produit le connecteur IGN sur la vraie réponse."""
    reg = DataRegistry()
    loc = json.loads(fixture("ign_search.json"))["features"][0]
    reg.set(DataPoint(key="location.reference_point", source_name="IGN", data_date="2026-11-08", method="api",
                      status="ok", confidence="high",
                      value={"lon": loc["geometry"]["coordinates"][0], "lat": loc["geometry"]["coordinates"][1],
                             "citycode": loc["properties"]["citycode"], "postcode": loc["properties"]["postcode"]}))
    return reg


def call(source_id, ctx, needs):
    return run_connector(CATALOG.by_id(source_id), [n.key for n in needs],
                         lambda: CONNECTORS.get(source_id).fn(ctx, needs))


def respond(body: bytes, ctype="application/json", status=200):
    return lambda req: httpx.Response(status, content=body, headers={"content-type": ctype})


# ------------------------------------------------------------------ registre des connecteurs

def test_registered_connectors_are_verified_sources():
    for sid in CONNECTORS.specs:
        src = CATALOG.by_id(sid)
        assert src.verified and src.verified_on, f"{sid} : connecteur actif sur une source non vérifiée"


# ------------------------------------------------------------------ IGN

def test_ign_real_response(tmp_path):
    seen = {}

    def handler(req):
        seen.update(dict(req.url.params))
        return httpx.Response(200, content=fixture("ign_search.json"))

    [dp] = call("ign_geocodage", ctx_with(tmp_path, handler), needs_for("location.reference_point"))
    assert seen["q"] == CFG.asset.reference_point.address and seen["index"] == "address"
    assert dp.status == "ok" and dp.method == "api" and dp.confidence == "high"
    assert dp.value["lon"] == 2.340481 and dp.value["lat"] == 48.872592
    assert dp.value["citycode"] == "75109" and dp.value["postcode"] == "75009"
    assert dp.source_url.startswith("https://data.geopf.fr/geocodage/search")


def test_ign_low_score_is_partial(tmp_path):
    data = json.loads(fixture("ign_search.json"))
    data["features"][0]["properties"]["score"] = 0.41
    [dp] = call("ign_geocodage", ctx_with(tmp_path, respond(json.dumps(data).encode())),
                needs_for("location.reference_point"))
    assert dp.status == "partial" and dp.confidence == "low" and "INFÉRIEUR" in dp.notes


@pytest.mark.parametrize("body", [b'{"type":"FeatureCollection","features":[]}', b'{"foo": 1}'])
def test_ign_bad_responses_give_missing(tmp_path, body):
    [dp] = call("ign_geocodage", ctx_with(tmp_path, respond(body)), needs_for("location.reference_point"))
    assert dp.status == "missing" and dp.value is None and "format" in dp.notes


def test_ign_http_error_gives_missing(tmp_path):
    [dp] = call("ign_geocodage", ctx_with(tmp_path, respond(b"", status=503)), needs_for("location.reference_point"))
    assert dp.status == "missing" and "indisponible" in dp.notes


# ------------------------------------------------------------------ INSEE

@pytest.mark.parametrize("key,source_id,fixture_name,last,period,freq", [
    ("index.ilat", "insee_ilat", "insee_ilat.xml", 138.01, "2026-Q2", "T"),
    ("index.icc", "insee_icc", "insee_icc.xml", 2103.0, "2026-Q2", "T"),
    ("index.bt01", "insee_bt01", "insee_bt01.xml", 139.4, "2026-08", "M"),
])
def test_insee_real_responses(tmp_path, key, source_id, fixture_name, last, period, freq):
    [dp] = call(source_id, ctx_with(tmp_path, respond(fixture(fixture_name), "application/xml")), needs_for(key))
    assert dp.status == "ok" and dp.data_date == period
    v = dp.value
    assert v["last"] == last and v["period"] == period and v["frequency"] == freq
    assert v["series"] == sorted(v["series"], key=lambda o: o["period"])
    assert v["yoy"] is not None


def test_insee_yoy_is_computed_on_same_period_last_year(tmp_path):
    [dp] = call("insee_ilat", ctx_with(tmp_path, respond(fixture("insee_ilat.xml"), "application/xml")),
                needs_for("index.ilat"))
    s = {o["period"]: o["value"] for o in dp.value["series"]}
    assert dp.value["yoy"] == round(s["2026-Q2"] / s["2025-Q2"] - 1, 6)


def test_insee_wrong_series_rejected(tmp_path):
    """Garde-fou idbank : une série d'un autre indice (ici l'ICC servi pour l'ILAT) est rejetée."""
    [dp] = call("insee_ilat", ctx_with(tmp_path, respond(fixture("insee_icc.xml"), "application/xml")),
                needs_for("index.ilat"))
    assert dp.status == "missing" and "série inattendue" in dp.notes


def test_insee_malformed_xml(tmp_path):
    [dp] = call("insee_ilat", ctx_with(tmp_path, respond(b"<html>maintenance</html>", "text/html")),
                needs_for("index.ilat"))
    assert dp.status == "missing"


# ------------------------------------------------------------------ ImmoStat

IMMOSTAT_KEYS = ("market.average_rent", "market.take_up", "market.immediate_supply", "market.investment_price_per_sqm")


def test_immostat_real_file(tmp_path):
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    pts = {p.key: p for p in call("immostat", ctx_with(tmp_path, respond(fixture("immostat.xlsx"), xlsx)),
                                  needs_for(*IMMOSTAT_KEYS))}
    rent = pts["market.average_rent"]
    assert rent.status == "ok" and rent.data_date == "2026-T3" and rent.unit == "€/m²/an"
    assert round(rent.value["last"], 1) == 779.4                        # T3 2026, Paris Centre Ouest, seconde main
    assert "Paris Centre Ouest" in rent.notes and "SECONDE MAIN" in rent.notes and rent.confidence == "medium"
    assert pts["market.investment_price_per_sqm"].value["last"] == 13812
    assert pts["market.immediate_supply"].value["last"] == 680000
    take_up = pts["market.take_up"].value
    assert take_up["last"] == 99323 and take_up["rolling_12m"] == 111501 + 89155 + 108052 + 99323
    assert pts["market.investment_price_per_sqm"].value["yoy"] == round(13812 / 15289 - 1, 6)


def test_immostat_unmapped_submarket(tmp_path):
    d = DEFAULTS.model_copy(update={"submarket_zones": {}})
    pts = call("immostat", ctx_with(tmp_path, respond(fixture("immostat.xlsx")), defaults=d),
               needs_for("market.average_rent"))
    assert pts[0].status == "missing" and "correspondance" in pts[0].notes


def test_immostat_html_instead_of_excel(tmp_path):
    """Cas réel probable : URL datée périmée -> page HTML au lieu du fichier."""
    pts = call("immostat", ctx_with(tmp_path, respond(b"<html>404</html>", "text/html")), needs_for("market.average_rent"))
    assert pts[0].status == "missing" and "illisible" in pts[0].notes


def test_immostat_does_not_provide_prime_rent():
    assert "immostat" not in needs_for("market.prime_rent")[0].target_sources


# ------------------------------------------------------------------ Paris PLU

def test_plu_emprises_paginates_and_filters_arrondissement(tmp_path):
    page = json.loads(fixture("paris_sample_plub_ecm.json"))
    rec = page["results"][0]
    calls = []

    def handler(req):
        params = dict(req.url.params)
        calls.append(params)
        offset = int(params["offset"])
        recs = [dict(rec, hauteur=float(offset // 100 + 1) * 10 if i % 2 else 0.0) for i in range(100)]
        return httpx.Response(200, json={"total_count": 250, "results": recs[: 250 - offset]})

    [dp] = call("paris_plu_emprises", ctx_with(tmp_path, handler, located_registry()),
                needs_for("urbanism.plu_constraints"))
    assert [c["offset"] for c in calls] == ["0", "100", "200"] and calls[0]["where"] == "n_sq_ca=9"
    assert dp.status == "ok" and dp.value["arrondissement"] == 9 and dp.value["n_polygons"] == 250
    assert dp.value["max_height"] == 30.0 and dp.value["n_with_height"] == 125   # les 0 ne sont pas des hauteurs
    assert "PAS la règle applicable à la parcelle" in dp.notes


def test_plu_real_record_fields(tmp_path):
    page = json.loads(fixture("paris_sample_plub_ecm.json"))
    page["total_count"] = len(page["results"])
    [dp] = call("paris_plu_emprises", ctx_with(tmp_path, respond(json.dumps(page).encode()), located_registry()),
                needs_for("urbanism.plu_constraints"))
    assert dp.status == "ok" and dp.value["n_polygons"] == len(page["results"])


def test_plu_protections_real_fields(tmp_path):
    page = json.loads(fixture("paris_sample_plub_surf_pvp.json"))
    page["total_count"] = len(page["results"])
    [dp] = call("paris_plu_protections", ctx_with(tmp_path, respond(json.dumps(page).encode()), located_registry()),
                needs_for("urbanism.protected_buildings"))
    assert dp.value["count"] == len(page["results"]) and dp.value["total_area_sqm"] > 0


def test_plu_outside_paris_is_missing(tmp_path):
    reg = located_registry()
    loc = dict(reg.get("location.reference_point").value, citycode="69382")
    reg.set(reg.get("location.reference_point").model_copy(update={"value": loc}))
    [dp] = call("paris_plu_emprises", ctx_with(tmp_path, respond(b"{}"), reg), needs_for("urbanism.plu_constraints"))
    assert dp.status == "missing" and "Paris uniquement" in dp.notes


def test_plu_without_location_is_missing(tmp_path):
    [dp] = call("paris_plu_emprises", ctx_with(tmp_path, respond(b"{}")), needs_for("urbanism.plu_constraints"))
    assert dp.status == "missing" and "non géocodé" in dp.notes


# ------------------------------------------------------------------ ADEME

def test_ademe_real_records_and_pagination(tmp_path):
    sample = json.loads(fixture("ademe_sample.json"))
    calls = []

    def handler(req):
        calls.append(str(req.url))
        if "after=" in str(req.url):
            return httpx.Response(200, json={"total": 10, "next": None, "results": sample["results"]})
        return httpx.Response(200, json={"total": 10, "next": sample["next"], "results": sample["results"]})

    [dp] = call("ademe_dpe_tertiaire", ctx_with(tmp_path, handler, located_registry()),
                needs_for("energy.dpe_comparables"))
    assert "geo_distance=2.340481%3A48.872592%3A500" in calls[0]
    assert len(calls) == 2                                   # suit `next`, puis s'arrête
    v = dp.value
    assert v["n_dpe"] == 10 and sum(v["by_class"].values()) == v["n_with_label"]
    assert dp.status == "ok" and "non représentative" in dp.notes


def test_ademe_truncated_is_partial(tmp_path):
    sample = json.loads(fixture("ademe_sample.json"))
    body = json.dumps({"total": 99999, "next": None, "results": sample["results"]}).encode()
    [dp] = call("ademe_dpe_tertiaire", ctx_with(tmp_path, respond(body), located_registry()),
                needs_for("energy.dpe_comparables"))
    assert dp.status == "partial" and "tronquée" in dp.notes


def test_ademe_empty_is_missing(tmp_path):
    body = json.dumps({"total": 0, "next": None, "results": []}).encode()
    [dp] = call("ademe_dpe_tertiaire", ctx_with(tmp_path, respond(body), located_registry()),
                needs_for("energy.dpe_comparables"))
    assert dp.status == "missing"


# ------------------------------------------------------------------ bout en bout sur réponses réelles

def test_end_to_end_with_real_fixtures(tmp_path):
    """Le graphe complet, nourri des vraies réponses : les sources vérifiées remplissent le registre."""
    from agent.graph import build_graph, recursion_limit
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ecm, pvp = json.loads(fixture("paris_sample_plub_ecm.json")), json.loads(fixture("paris_sample_plub_surf_pvp.json"))
    ecm["total_count"], pvp["total_count"] = len(ecm["results"]), len(pvp["results"])
    ademe = json.loads(fixture("ademe_sample.json")) | {"next": None}

    def handler(req):
        u = str(req.url)
        if "geopf.fr" in u:
            return httpx.Response(200, content=fixture("ign_search.json"))
        for idb, f in (("001617112", "insee_ilat.xml"), ("000008630", "insee_icc.xml"), ("001710986", "insee_bt01.xml")):
            if idb in u:
                return httpx.Response(200, content=fixture(f))
        if "immostat" in u:
            return httpx.Response(200, content=fixture("immostat.xlsx"), headers={"content-type": xlsx})
        if "plub_ecm" in u:
            return httpx.Response(200, json=ecm)
        if "plub_surf_pvp" in u:
            return httpx.Response(200, json=pvp)
        if "ademe" in u:
            return httpx.Response(200, json=ademe)
        if "data-api.ecb.europa.eu" in u:
            return httpx.Response(200, content=fixture("ecb_euribor.csv"), headers={"content-type": "text/csv"})
        if "data.gouv.fr" in u:
            return firmio_handler(req)
        if "apidf-preprod.cerema.fr" in u:
            return httpx.Response(200, json={"count": 100, "next": None, "results": dvf_activity_results()})
        return httpx.Response(404)

    client = HttpClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler), max_retries=0)
    state = build_initial_state(CFG, DEFAULTS, load_investor_targets(), CATALOG, analysis_date=date(2026, 11, 8))
    final = build_graph(CONNECTORS, client).invoke(state, config={"recursion_limit": recursion_limit(2)})
    reg = final["registry"]
    for key in ("location.reference_point", "index.ilat", "index.icc", "index.bt01", "market.average_rent",
                "market.take_up", "market.immediate_supply", "market.investment_price_per_sqm",
                "urbanism.plu_constraints", "urbanism.protected_buildings", "energy.dpe_comparables",
                "rates.euribor_3m", "market.dvf_price_per_sqm"):
        assert reg.found(key), f"{key} devrait être rempli : {reg.get(key)}"
    # DVF officiel retenu, Firmio conservé comme 2e source pour le recoupement
    srcs = [p.source_name for p in reg.found("market.dvf_price_per_sqm")]
    assert srcs[0].startswith("DVF+") and srcs[1].startswith("Firmio")
    assert reg.value("rates.euribor_3m")["last"] == pytest.approx(0.026346818)
    # analyses de marché : les sites répondent 404 ici, les copies locales sont lues (params.local_file)
    prime = reg.found("market.prime_rent")                      # MarketBeat T3 2026 d'abord, page Global Icons en recoupement
    assert [p.value for p in prime] == [1250.0, 1150.0] and all("copie locale" in p.notes for p in prime)
    assert reg.value("market.vacancy") == pytest.approx(0.069)
    assert reg.value("market.prime_yield") == 0.04 and reg.get("market.prime_yield").method == "report_extraction"
    # absents des deux documents : manquant, raison explicite, jamais inventé
    inc = reg.get_all("market.incentives")
    assert all(p.status == "missing" and p.value is None for p in inc) and any("la page ne" in p.notes for p in inc)
    report = final["validation_report"]
    assert report.verdict == "proceed_with_assumptions"
    assert report.gaps == ["deal.acquisition_fees_pct"]                 # seule lacune P1 : hypothèse de repli
    assert reg.get("deal.acquisition_fees_pct").status == "assumption"
    cc = [i for i in report.issues if i.key == "market.dvf_price_per_sqm" and i.check == "convergence"]
    assert len(cc) == 1 and "DVF+" in cc[0].message and "Firmio" in cc[0].message
    # données exigées présentes : le modèle est complet et une décision est prise
    assert final["financials"]["status"] == "complete"
    assert all(final["financials"]["required_data"].values())
    assert final["recommendation"]["decision"] in ("GO", "GO AVEC CONTRAINTES", "GO SOUS CONDITIONS", "NO-GO")


# ------------------------------------------------------------------ BCE (Euribor)

def test_ecb_real_response_converts_percent_to_fraction(tmp_path):
    seen = {}

    def handler(req):
        seen.update(dict(req.url.params))
        return httpx.Response(200, content=fixture("ecb_euribor.csv"), headers={"content-type": "text/csv"})

    [dp] = call("ecb_euribor", ctx_with(tmp_path, handler), needs_for("rates.euribor_3m"))
    assert seen["format"] == "csvdata"
    assert dp.status == "ok" and dp.unit == "fraction" and dp.data_date == "2026-10"
    assert dp.value["last"] == pytest.approx(0.026346818)          # 2.6346818 % publié en PCPA
    assert len(dp.value["series"]) == 22 and dp.value["series"][0]["period"] == "2025-01"
    assert "convertie en fraction" in dp.notes


def test_ecb_unknown_unit_is_refused(tmp_path):
    body = fixture("ecb_euribor.csv").decode().replace(",PCPA,", ",PURE_NUMB,").encode()
    [dp] = call("ecb_euribor", ctx_with(tmp_path, respond(body, "text/csv")), needs_for("rates.euribor_3m"))
    assert dp.status == "missing" and "unité inconnue" in dp.notes


def test_ecb_value_passes_validation_range():
    """La valeur convertie respecte la plage plausible de defaults.yaml (2.63 ne la respecterait pas)."""
    lo, hi = DEFAULTS.need("rates.euribor_3m").plausible_range
    assert lo <= 0.026346818 <= hi and not lo <= 2.6346818 <= hi


# ------------------------------------------------------------------ Firmio

def firmio_handler(req):
    if "api/1/datasets" in str(req.url):
        return httpx.Response(200, content=fixture("firmio_dataset.json"))
    if str(req.url).endswith(".csv"):
        return httpx.Response(200, content=fixture("firmio_csv.csv"), headers={"content-type": "text/csv"})
    return httpx.Response(404)


def test_firmio_real_file_paris_9(tmp_path):
    import csv as _csv
    rows = [r for r in _csv.DictReader(fixture("firmio_csv.csv").decode().splitlines()) if r["code_commune"] == "75109"]
    main = max(rows, key=lambda r: int(r["nb_transactions"]))                 # recalcul indépendant
    [dp] = call("firmio_dvf_entreprises", ctx_with(tmp_path, firmio_handler, located_registry()),
                needs_for("market.dvf_price_per_sqm"))
    assert dp.status == "ok" and dp.confidence == "low" and dp.data_date == "2026-10-03"
    assert dp.value["median"] == float(main["prix_m2_median"]) == 9059
    assert dp.value["n_variants"] == len(rows) == 4
    assert dp.value["variants_range"] == [8670, 9568]
    assert "MÉLANGE" in dp.notes and "incohérent" in dp.notes
    assert dp.source_url.endswith(".csv")


def test_firmio_unknown_commune_is_missing(tmp_path):
    reg = located_registry()
    reg.set(reg.get("location.reference_point").model_copy(
        update={"value": dict(reg.get("location.reference_point").value, citycode="99999")}))
    [dp] = call("firmio_dvf_entreprises", ctx_with(tmp_path, firmio_handler, reg), needs_for("market.dvf_price_per_sqm"))
    assert dp.status == "missing" and "absente" in dp.notes


def test_firmio_changed_header_is_refused(tmp_path):
    def handler(req):
        if "api/1/datasets" in str(req.url):
            return httpx.Response(200, content=fixture("firmio_dataset.json"))
        return httpx.Response(200, content=b"code,prix\n75109,9000\n")
    [dp] = call("firmio_dvf_entreprises", ctx_with(tmp_path, handler, located_registry()),
                needs_for("market.dvf_price_per_sqm"))
    assert dp.status == "missing" and "en-tête" in dp.notes


# ------------------------------------------------------------------ DVF (Cerema)

def dvf_activity_results() -> list[dict]:
    """Vraies mutations « ACTIVITE » (sonde n°2) ; la route /mutations/ renvoie les mêmes champs dans `results`."""
    return [f["properties"] for f in json.loads(fixture("dvf_geomut_codtypbien_14.json"))["features"]]


def test_dvf_mutations_route_has_same_fields_as_geomutations():
    mut = set(json.loads(fixture("dvf_mutations_bbox.json"))["results"][0])
    geo = set(dvf_activity_results()[0])
    assert geo - {"idmutation"} <= mut                                         # hypothèse du test ci-dessous


def test_dvf_real_activity_transactions(tmp_path):
    from statistics import median, quantiles
    rows = dvf_activity_results()
    calls = []

    def handler(req):
        calls.append(req.url)
        if "page=2" in str(req.url):
            return httpx.Response(200, json={"count": 100, "next": None, "results": rows[50:]})
        nxt = "http://apidf-preprod.cerema.fr/dvf_opendata/mutations/?page=2"
        return httpx.Response(200, json={"count": 100, "next": nxt, "results": rows[:50]})

    [dp] = call("dvf", ctx_with(tmp_path, handler, located_registry()), needs_for("market.dvf_price_per_sqm"))
    assert calls[1].scheme == "https"                                          # lien http réécrit
    p = dict(calls[0].params)
    assert p["codtypbien"] == "14" and p["anneemut_min"] == "2021"
    w = [float(x) for x in p["in_bbox"].split(",")]
    assert w[2] - w[0] <= 0.02 and w[3] - w[1] <= 0.02                         # limite de l'API respectée
    # recalcul indépendant du filtrage du plan : ventes, hors VEFA, activité, puis p5-p95
    ok = [float(r["valeurfonc"]) / float(r["sbati"]) for r in rows
          if r["libnatmut"] == "Vente" and not r["vefa"] and r["codtypbien"] == "14" and float(r["sbati"]) > 0]
    q = quantiles(ok, n=20)
    trimmed = [x for x in ok if q[0] <= x <= q[-1]]
    v = dp.value
    assert v["n"] == len(trimmed) and v["median"] == round(median(trimmed))
    # les 2 VEFA sont écartées par la nature de mutation (« Vente en l'état futur d'achèvement »)
    assert v["dropped"]["nature"] == 2 and v["received"] == 100
    assert v["large_lots"]["n"] >= 1 and v["radius_m"] == 500
    assert dp.status == "ok" and dp.confidence == "medium" and dp.data_date.startswith("2021")
    assert "pas de loyers" in dp.notes


def test_dvf_truncated_pagination_is_partial(tmp_path):
    rows = dvf_activity_results()
    body = json.dumps({"count": 5000, "next": None, "results": rows}).encode()
    [dp] = call("dvf", ctx_with(tmp_path, respond(body), located_registry()), needs_for("market.dvf_price_per_sqm"))
    assert dp.status == "partial" and "TRONQUÉE" in dp.notes


def test_dvf_only_residential_is_missing(tmp_path):
    rows = [dict(r, codtypbien="121", libtypbien="UN APPARTEMENT") for r in dvf_activity_results()]
    body = json.dumps({"count": len(rows), "next": None, "results": rows}).encode()
    [dp] = call("dvf", ctx_with(tmp_path, respond(body), located_registry()), needs_for("market.dvf_price_per_sqm"))
    assert dp.status == "missing" and "aucune transaction exploitable" in dp.notes


def test_dvf_timeout_is_missing(tmp_path):
    def handler(req):
        raise httpx.ReadTimeout("lent", request=req)
    [dp] = call("dvf", ctx_with(tmp_path, handler, located_registry()), needs_for("market.dvf_price_per_sqm"))
    assert dp.status == "missing" and "indisponible" in dp.notes
