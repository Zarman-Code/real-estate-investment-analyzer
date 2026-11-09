"""Connecteurs des analyses de marché, vérifiés sur de vrais documents :
- page HTML Global Icons « Marché prime Paris QCA 2026 » (tests/fixtures/globalicons_marche_prime_paris_qca_2026.html,
  <main> recopié à l'identique, empreinte vérifiée le 2026-11-08) ;
- PDF Cushman & Wakefield MarketBeat Bureaux IDF T3 2026 (tests/fixtures/cushman_marketbeat_bureaux_idf_t3_2026.pdf)."""
from __future__ import annotations

import copy
import hashlib
import re
from datetime import date

import httpx
import pytest

from agent.state import DataNeed
from config.schema import load_config, load_defaults, load_sources_catalog
from tools.base import ParseError, run_connector
from tools.connectors import ConnectorContext
from tools.http import HttpClient
from tools.market_reports import (CW_COLUMNS, detect_layout, fetch_cushman_marketbeat, fetch_market_reports, gi_cell,
                                  load_report, page_lines, read_cw_table, read_doc, split_row)
from tools.pdf_text import normalise, parse_fr_number, pdf_pages
from tools.registry import DataRegistry

from realdata import FIX

PDF = (FIX / "cushman_marketbeat_bureaux_idf_t3_2026.pdf").read_bytes()
HTML = (FIX / "globalicons_marche_prime_paris_qca_2026.html").read_bytes()
CFG, DEFAULTS, CATALOG = load_config(), load_defaults(), load_sources_catalog()


@pytest.fixture(scope="module")
def pages():
    return pdf_pages(PDF)


# ------------------------------------------------------------------ nombres et documents

@pytest.mark.parametrize("raw,expected", [("1 250", 1250.0), ("1 050 321", 1050321.0), ("6,90", 6.9),
                                          ("−14", -14.0), ("+8", 8.0), ("0", 0.0)])
def test_french_numbers(raw, expected):
    assert parse_fr_number(raw) == expected


@pytest.mark.parametrize("raw", ["", "1,2,3", "abc", "12 34"])
def test_unreadable_numbers_are_refused(raw):
    with pytest.raises(ParseError):
        parse_fr_number(raw)


def test_documents_are_recognised_by_content(pages):
    assert read_doc(PDF).kind == "pdf" and read_doc(HTML).kind == "html"
    assert len(pages) == 5 and "3e TRIMESTRE 2026" in normalise(pages[0])
    with pytest.raises(ParseError, match="ni PDF ni HTML"):
        read_doc(b'{"json": true}')
    with pytest.raises(ParseError, match="n'est pas un PDF"):
        pdf_pages(b"<!doctype html><html>cookie wall</html>")


def test_layouts_are_recognised_and_unknown_ones_refused():
    assert detect_layout(read_doc(PDF)).id == "cushman_marketbeat_bureaux_idf"
    assert detect_layout(read_doc(HTML)).id == "globalicons_qca"
    with pytest.raises(ParseError, match="mise en page inconnue"):
        detect_layout(read_doc(b"<!doctype html><html><body><table></table></body></html>"))


def test_html_fixture_main_is_the_real_page():
    """L'élément <main> de la fixture est l'octet près celui de la page relevée (empreinte notée au relevé)."""
    t = HTML.decode("utf-8")
    main = t[t.index("  <main"): t.index("</main>") + 7]
    assert hashlib.sha256(main.encode("utf-8")).hexdigest() == "c892e7cff51f199063329d08a1b7e733aee1a1b33abde2687d517f144b242350"


# ------------------------------------------------------------------ page HTML Global Icons

@pytest.mark.parametrize("raw,unit,low,high,approx", [("2,75 %", "%", 2.75, 2.75, False),
                                                      ("1 150 €/m²/an", "€/m²/an", 1150, 1150, False),
                                                      ("~2 500", "", 2500, 2500, True),
                                                      ("3,75 - 4,00 %", "%", 3.75, 4.0, False),
                                                      ("~2 800 - 3 200", "", 2800, 3200, True)])
def test_html_cells(raw, unit, low, high, approx):
    c = gi_cell(raw, unit)
    assert (c.low, c.high, c.approx) == (low, high, approx)


def test_html_values_are_the_last_observed_year_not_the_estimate():
    ex = detect_layout(d := read_doc(HTML)).extract(d, "Paris QCA")
    y, r, v = ex.found["prime_yield"], ex.found["prime_rent"], ex.found["investment_volume"]
    assert (y.value, y.unit, y.data_date) == (0.04, "fraction", "2025")
    assert "ligne 2025" in y.ref and "4,00 %" in y.ref and "3,75 - 4,00 %" in y.what and "3,50-4,25 %" in y.what
    assert (r.value, r.data_date) == (1150.0, "2025") and "1 150 - 1 200 €/m²/an" in r.what
    assert v.value == 2.5e9 and v.confidence == "low" and "approximation" in v.what
    assert y.confidence == r.confidence == "medium" and "source secondaire" in y.what
    assert set(ex.absent) == {"average_rent", "incentives", "future_supply", "take_up", "immediate_supply"}


def test_html_vacancy_range_is_flagged_partial():
    ex = detect_layout(d := read_doc(HTML)).extract(d, "Paris QCA")
    vac = ex.found["vacancy"]
    assert vac.value == pytest.approx(0.055) and vac.status == "partial" and vac.confidence == "low"
    assert vac.data_date == "2026" and "milieu de la fourchette" in vac.what and "grade A < 3 %" in vac.what


def test_html_page_must_be_about_the_submarket():
    with pytest.raises(ParseError, match="ne porte pas sur « Paris Centre Ouest »"):
        detect_layout(d := read_doc(HTML)).extract(d, "Paris Centre Ouest")


def test_html_layout_change_is_detected():
    broken = HTML.replace("Rendement prime bureau".encode(), "Taux".encode())
    with pytest.raises(ParseError, match="mise en page inconnue"):
        detect_layout(read_doc(broken))


# ------------------------------------------------------------------ PDF Cushman & Wakefield

def test_paris_qca_row_is_read_cell_by_cell(pages):
    t = read_cw_table(PDF, pages, "Paris QCA")
    assert t.period == "2026-T3" and t.zone == "Paris QCA"
    assert {k: c.value for k, c in t.cells.items()} == {
        "take_up_ytd": 234617, "take_up_share": 0.22, "immediate_supply": 487000, "supply_share": 0.07,
        "vacancy": pytest.approx(0.069), "pipeline": 207967, "pipeline_available": 128423, "prelet": 0.38,
        "prime_rent": 1250, "avg_rent_first": 968, "avg_rent_second": 805}
    assert {c.page for c in t.cells.values()} == {5}


def test_table_matches_the_commentary_of_the_report(pages):
    """Recoupement interne : le texte des pages 3 et 4 cite les mêmes valeurs du QCA que le tableau page 5."""
    t = read_cw_table(PDF, pages, "Paris QCA")
    p3 = normalise(pages[2])
    assert "Paris QCA reste à 1 250 €/m²/an" in p3 and t.cells["prime_rent"].value == 1250
    assert "QCA à 805 €/m²/an" in p3 and t.cells["avg_rent_second"].value == 805
    assert "(968 €/m²/an" in p3 and t.cells["avg_rent_first"].value == 968
    assert "le QCA atteint 6,9 %" in normalise(pages[3]) and t.cells["vacancy"].value == pytest.approx(0.069)


def test_thousands_are_joined_but_columns_kept_apart(pages):
    """« 1 250 968 805 » : trois cellules (1 250 / 968 / 805) ; « - » reste une cellule vide."""
    rows = {}
    for line in page_lines(PDF, 4):
        try:
            label, cells = split_row(line, max_gap=6.0)
        except ParseError:
            continue
        rows[label] = cells
    assert rows["TOTAL PARIS"][-3:] == ["1 250", "747", "561"]
    assert rows["TOTAL ILE-DE-FRANCE"][0] == "1 050 321" and rows["TOTAL ILE-DE-FRANCE"][-3:] == ["-", "-", "-"]
    t = read_cw_table(PDF, pages, "Paris Nord-Est")
    assert t.cells["pipeline_available"].value is None and t.cells["prelet"].value == 1.0
    assert len(CW_COLUMNS) == 11


def test_unknown_zone_lists_the_rows_read(pages):
    with pytest.raises(ParseError, match="ligne « Paris 9e » absente.*Paris QCA"):
        read_cw_table(PDF, pages, "Paris 9e")


# ------------------------------------------------------------------ connecteurs

def _need(key, indicator, source):
    return DataNeed(key=key, indicators=[indicator], priority="P2", scope="submarket", reason="test",
                    target_sources=[source])


def _ctx(tmp_path, handler, catalog=CATALOG):
    client = HttpClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler), max_retries=0)
    return ConnectorContext(config=CFG, defaults=DEFAULTS, catalog=catalog, registry=DataRegistry(),
                            analysis_date=date(2026, 11, 8).isoformat(), client=client)


def _serve(content, ctype):
    return lambda req: httpx.Response(200, content=content, headers={"content-type": ctype})


HTML_NEEDS = [_need(k, i, "market_reports") for k, i in (("market.prime_yield", "prime_yield"), ("market.prime_rent", "prime_rent"),
                                                        ("market.vacancy", "vacancy"), ("market.incentives", "incentives"),
                                                        ("market.average_rent", "average_rent"))]


def test_market_reports_connector_scrapes_the_page(tmp_path):
    pts = {p.key: p for p in fetch_market_reports(_ctx(tmp_path, _serve(HTML, "text/html; charset=utf-8")), HTML_NEEDS)}
    py = pts["market.prime_yield"]
    assert (py.value, py.unit, py.method, py.status, py.data_date) == (0.04, "fraction", "report_extraction", "ok", "2025")
    assert py.source_url == CATALOG.by_id("market_reports").url and "Global Icons" in py.notes and "téléchargement" in py.notes
    assert pts["market.vacancy"].status == "partial" and pts["market.vacancy"].confidence == "low"
    for key in ("market.incentives", "market.average_rent"):
        assert pts[key].status == "missing" and pts[key].value is None and "la page ne" in pts[key].notes


def test_cushman_connector_reads_the_pdf(tmp_path):
    needs = [_need(k, i, "cushman_marketbeat") for k, i in (("market.prime_rent", "prime_rent"), ("market.average_rent", "average_rent"),
                                                             ("market.vacancy", "vacancy"), ("market.future_supply", "future_supply"))]
    pts = {p.key: p for p in fetch_cushman_marketbeat(_ctx(tmp_path, _serve(PDF, "application/pdf")), needs)}
    pr = pts["market.prime_rent"]
    assert (pr.value, pr.data_date, pr.confidence) == (1250.0, "2026-T3", "high") and "page 5" in pr.notes
    assert pts["market.average_rent"].value == {"last": 805.0, "period": "2026-T3", "first_hand": 968.0}
    assert pts["market.vacancy"].value == pytest.approx(0.069)
    assert pts["market.future_supply"].value == 128423.0 and "207 967" in pts["market.future_supply"].notes


@pytest.mark.parametrize("source_id,fetch", [("market_reports", fetch_market_reports), ("cushman_marketbeat", fetch_cushman_marketbeat)])
def test_local_copy_is_used_when_the_site_refuses(tmp_path, source_id, fetch):
    need = _need("market.prime_rent", "prime_rent", source_id)
    pts = fetch(_ctx(tmp_path, lambda req: httpx.Response(200, content=b'{"blocked": 1}', headers={"content-type": "application/json"})), [need])
    assert pts[0].status == "ok" and "copie locale" in pts[0].notes


def test_no_document_anywhere_gives_explicit_missing_points(tmp_path):
    raw = copy.deepcopy(CATALOG.model_dump())
    next(s for s in raw["sources"] if s["id"] == "market_reports")["params"]["local_file"] = "data/reports/absent.html"
    catalog = type(CATALOG).model_validate(raw)
    ctx = _ctx(tmp_path, lambda req: httpx.Response(403), catalog)
    pts = run_connector(catalog.by_id("market_reports"), [n.key for n in HTML_NEEDS],
                        lambda: fetch_market_reports(ctx, HTML_NEEDS))
    assert all(p.status == "missing" and p.value is None for p in pts)
    assert "HTTP 403" in pts[0].notes and "absent.html" in pts[0].notes


def test_load_report_prefers_the_download(tmp_path):
    r, origin = load_report(_ctx(tmp_path, _serve(HTML, "text/html")), CATALOG.by_id("market_reports"))
    assert re.search(rb"<main", r.content) and origin == "téléchargement"
