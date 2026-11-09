"""Tests du dashboard HTML autonome (plan, section 9) :
fichier unique hors ligne, aucune donnée du deal dans le gabarit, données injectées, et — si un navigateur
headless est disponible — affichage sans erreur, aucune requête réseau, simulateur fidèle au moteur Python."""
from __future__ import annotations

import json
import re

import pytest

from config.schema import PROJECT_ROOT, load_config, load_defaults
from finance.engine import run_model
from finance.inputs import resolve_inputs
from finance.variations import apply_changes
from realdata import run_on_fixtures
from report.dashboard_v1 import ENGINE_PATH, TEMPLATE_PATH, build_data, render, write_dashboard

D = load_defaults()


@pytest.fixture(scope="module")
def outputs(tmp_path_factory):
    """Sorties de l'agent sur les vraies réponses enregistrées, écrites comme le fait run.py."""
    out = tmp_path_factory.mktemp("out")
    final = run_on_fixtures(out)
    final["registry"].export_json(out / "data_registry.json")
    analysis = {"analysis_date": final["analysis_date"], "asset": final["config"].asset.name,
                "financials": final["financials"], "risks": final["risks"], "sensitivity": final["sensitivity"],
                "scenarios": final["scenarios"], "recommendation": final["recommendation"]}
    (out / "analysis_results.json").write_text(json.dumps(analysis, ensure_ascii=False, default=str), encoding="utf-8")
    path = write_dashboard(out, final["config"], D)
    return {"dir": out, "html": path, "final": final}


def _data(html: str) -> dict:
    m = re.search(r"window\.DATA = (\{.*?\});</script>", html, re.S)
    assert m, "objet window.DATA absent"
    return json.loads(m.group(1))


def test_single_offline_file(outputs):
    html = outputs["html"].read_text(encoding="utf-8")
    assert "/*__DATA__*/" not in html and "/*__ENGINE__*/" not in html
    assert ENGINE_PATH.read_text(encoding="utf-8") in html                      # moteur testé, inliné tel quel
    assert not re.search(r"<(script|link|img)[^>]+(src|href)=", html)          # aucune ressource externe
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "http://" not in template and "https://" not in template


def test_template_contains_no_deal_data():
    """Règle 4 : aucun élément de asset_config.json dans le gabarit ni dans le générateur."""
    cfg = json.loads((PROJECT_ROOT / "asset_config.json").read_text(encoding="utf-8"))
    leaves = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        else:
            leaves.append(o)
    walk(cfg)
    code = "\n".join(p.read_text(encoding="utf-8") for p in (TEMPLATE_PATH, ENGINE_PATH, PROJECT_ROOT / "report" / "dashboard_v1.py"))
    for v in leaves:
        if isinstance(v, str) and len(v) >= 4 and not re.fullmatch(r"[a-z_]+|\d{4}-\d{2}-\d{2}", v):
            assert v not in code, v
        if isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) >= 1000:
            for form in (str(int(v)), f"{int(v):,}".replace(",", " ")):
                assert form not in code, form


def test_data_comes_from_agent_outputs(outputs):
    html = outputs["html"].read_text(encoding="utf-8")
    data, final = _data(html), outputs["final"]
    assert data["recommendation"]["decision"] == final["recommendation"]["decision"]
    assert data["financials"]["max_price"]["max_price"] == pytest.approx(final["financials"]["max_price"]["max_price"])
    assert data["model_inputs"] == json.loads(json.dumps(final["financials"]["model_inputs"]))
    assert set(data["registry"]) == set(final["registry"].to_dict())


def test_other_deal_is_rendered_without_code_change(outputs, tmp_path):
    raw = json.loads((PROJECT_ROOT / "asset_config.json").read_text(encoding="utf-8"))
    raw["asset"]["name"] = "Immeuble Test"
    p = tmp_path / "autre.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    analysis = json.loads((outputs["dir"] / "analysis_results.json").read_text(encoding="utf-8"))
    analysis["asset"] = "Immeuble Test"
    registry = json.loads((outputs["dir"] / "data_registry.json").read_text(encoding="utf-8"))
    html = render(build_data(analysis, registry, load_config(p), D))
    assert "<title>Immeuble Test" in html and _data(html)["config"]["asset"]["name"] == "Immeuble Test"


# ------------------------------------------------------------------ navigateur headless (facultatif)

def _browser():
    pw = pytest.importorskip("playwright.sync_api")
    ctx = pw.sync_playwright().start()
    try:
        return ctx, ctx.chromium.launch()
    except Exception as exc:  # noqa: BLE001 - navigateur absent sur la machine
        ctx.stop()
        pytest.skip(f"Chromium indisponible : {exc}")


def test_renders_offline_without_errors_and_simulator_matches_python(outputs):
    ctx, browser = _browser()
    try:
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        errors, requests = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("request", lambda r: requests.append(r.url) if not r.url.startswith(("file:", "data:")) else None)
        page.goto(outputs["html"].as_uri())
        page.wait_for_timeout(300)
        assert errors == [] and requests == []
        assert page.locator(".warn:has-text('Section non affichée')").count() == 0
        for sid in ("verdict", "operation", "prix", "loyer", "cout", "rentabilite", "macro", "sensibilites",
                    "scenarios", "risques", "hypotheses", "sources"):
            assert page.locator(f"#{sid} svg, #{sid} table").count() > 0, sid

        final = outputs["final"]
        central = resolve_inputs(final["config"], final["registry"], D).inputs

        def sim():
            return page.evaluate("""() => { const base = HeliosEngine.prepare(DATA.model_inputs);
              const ch = SIM.flatMap(d => { const k = +document.getElementById('s-' + d.id).value; return k === 0 ? [] : d.change(k); });
              const r = HeliosEngine.runModel(ch.length ? HeliosEngine.applyChanges(base, ch) : base);
              return [r.profitability.margin_on_cost, r.returns.levered_irr, r.cost.total_cost]; }""")

        py = run_model(central)
        m, irr, cost = sim()                                   # curseurs au central : résultat Python exact
        assert m == pytest.approx(py["profitability"]["margin_on_cost"], rel=1e-12)
        assert cost == pytest.approx(py["cost"]["total_cost"], rel=1e-12)

        page.eval_on_selector("#s-rent", "e => { e.value = 20; e.dispatchEvent(new Event('input')); }")
        page.eval_on_selector("#s-works", "e => { e.value = 3; e.dispatchEvent(new Event('input')); }")
        page.wait_for_timeout(200)
        C = type("C", (), {})
        changes = []
        for f, op, v in (("relet_rent", "mult", 1.2), ("works_months", "add", 3), ("exit_date", "add_months", 3)):
            c = C(); c.field, c.op, c.value = f, op, v; changes.append(c)
        py2 = run_model(apply_changes(central, changes))
        m2, irr2, cost2 = sim()
        assert m2 == pytest.approx(py2["profitability"]["margin_on_cost"], rel=1e-9)
        assert cost2 == pytest.approx(py2["cost"]["total_cost"], rel=1e-9)
        shown = page.inner_text("#sim-out")
        assert f"{py2['profitability']['margin_on_cost'] * 100:.1f}".replace(".", ",") in shown
    finally:
        browser.close()
        ctx.stop()
