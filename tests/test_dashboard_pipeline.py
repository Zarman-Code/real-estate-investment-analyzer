"""Dashboard HTML autonome (specification_dashboard_helios.md) : pipeline, schéma de DATA, fixture, fmt et pastilles,
sections 6.1 à 6.14, simulateur, cas limites (section 11) et checklist de vérification (section 12).
Les tests navigateur tournent sur chaque navigateur Playwright disponible et sont ignorés si aucun ne l'est."""
from __future__ import annotations

import copy
import json
import re

import pytest
from pydantic import ValidationError

from config.schema import PROJECT_ROOT, load_config, load_defaults
from decision.phrase import FORMATS as PY_FORMATS
from finance.engine import run_model
from finance.export import inputs_from_json
from finance.variations import apply_changes
from realdata import run_on_fixtures
from report.dashboard import CONTRACT_GAPS, ENGINE_PATH, TEMPLATES, build_data, render, tokenize
from report.dashboard_schema import TOKEN, validate_data

FIXTURE = PROJECT_ROOT / "fixtures" / "data_sample.json"
D = load_defaults()
BLOCKS = ["meta", "deal", "datapoints", "market", "financials", "sensitivity", "scenarios", "risks", "targets",
          "recommendation", "sources", "assumptions", "missing"]
SECTIONS = ["verdict", "note", "operation", "prix", "loyer", "cout", "rentabilite", "marche", "sensibilites", "scenarios", "risques",
            "annexes", "sources"]
# blocs ouverts à l'ouverture (point A3) : verdict, note go/no-go, Q1 à Q6 ; tout autre bloc est un <details> fermé
OPEN_SECTIONS = {"verdict", "note", "prix", "loyer", "cout", "rentabilite", "sensibilites", "risques"}
FOLDS = {"operation-detail", "marche-detail", "grilles-detail", "simulateur", "flux-detail", "scenarios-detail", "risk-detail",
         "sources", "note-pros", "note-cons", "note-kpi", "note-cond", "note-missing"}


@pytest.fixture()
def sample():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def agent(tmp_path_factory):
    """Exécution complète de l'agent (vraies réponses enregistrées) puis adaptateur -> DATA."""
    final = run_on_fixtures(tmp_path_factory.mktemp("agent"))
    analysis = json.loads(json.dumps({k: final[k] for k in ("financials", "risks", "sensitivity", "scenarios", "recommendation")}
                                     | {"analysis_date": final["analysis_date"], "asset": final["config"].asset.name}, default=str))
    return {"data": build_data(analysis, final["registry"].to_dict(), final["config"], D), "final": final, "analysis": analysis}


# ================================================================== schéma et fixture

def test_fixture_is_complete_valid_and_reproducible(sample):
    validate_data(sample)
    assert set(sample) == set(BLOCKS)
    import importlib.util
    spec = importlib.util.spec_from_file_location("mk", PROJECT_ROOT / "scripts" / "make_dashboard_fixture.py")
    mk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mk)
    assert json.loads(json.dumps(mk.make())) == sample, "la fixture ne correspond plus au générateur : relancer scripts/make_dashboard_fixture.py"


def test_fixture_is_a_fictitious_deal(sample):
    real = load_config()
    assert sample["meta"]["asset_name"] != real.asset.name
    assert sample["deal"]["operation"]["asking_price_eur"] != real.operation.asking_price_eur


@pytest.mark.parametrize("block", BLOCKS)
def test_missing_block_is_refused(sample, block):
    del sample[block]
    with pytest.raises(ValidationError):
        validate_data(sample)
    with pytest.raises(ValidationError):
        render(sample)                                                         # le générateur refuse d'écrire


@pytest.mark.parametrize("mutate,fragment", [
    (lambda d: d["recommendation"].update(rationale="Prix maximum de 27,0 M€."), "chiffre hors jeton"),
    (lambda d: d["recommendation"].update(rationale="Voir {financials.inconnu|pct}."), "datapoint absent"),
    (lambda d: d["recommendation"]["cons"].append("TRI de 12,5 % seulement"), "chiffre hors jeton"),
    (lambda d: d["recommendation"]["conditions"].append("prix de {financials.absent|meur}"), "datapoint absent"),
    (lambda d: d["recommendation"].update(priority_risks=["R99"]), "R99"),
    (lambda d: d["recommendation"].update(decision="PEUT-ÊTRE"), "decision"),
    (lambda d: d["datapoints"]["deal.asking_price_eur"].update(method="devinette"), "method"),
])
def test_inconsistent_data_is_refused(sample, mutate, fragment):
    mutate(sample)
    with pytest.raises(ValidationError, match=fragment):
        validate_data(sample)


# ================================================================== adaptateur (sorties de l'agent -> DATA)

def _render_tokens(text: str, dps: dict) -> str:
    """Rend un texte à jetons comme le ferait fmt() (formats Python équivalents), pour le comparer au texte de l'agent."""
    def one(m):
        dp = dps[m.group(1)]
        v = dp["value"]
        if m.group(2):
            v = v[m.group(2)]
        return PY_FORMATS[m.group(3)](v) if m.group(3) else str(v)
    return TOKEN.sub(one, text)


def test_adapter_output_is_valid(agent):
    data, final = agent["data"], agent["final"]
    validate_data(data)
    assert data["recommendation"]["decision"] == {"NO-GO": "NO_GO", "GO": "GO", "GO SOUS CONDITIONS": "GO_SOUS_CONDITIONS", "GO AVEC CONTRAINTES": "GO_AVEC_CONTRAINTES",
                                                  "NON DÉTERMINÉ": "NON_DETERMINE"}[final["recommendation"]["decision"]]
    assert data["datapoints"]["financials.max_price.central"]["value"] == pytest.approx(final["financials"]["max_price"]["max_price"])


def test_tokens_point_to_the_exact_numbers_of_the_agent_text(agent):
    """Chaque argument et chaque condition, rendus depuis leurs jetons, redonnent EXACTEMENT le texte de l'agent
    (pour les phrases sans variante d'affichage) : les chiffres sont rattachés aux bonnes données."""
    data, rec = agent["data"], agent["final"]["recommendation"]
    pairs = [(a, data["recommendation"]["pros" if side == "for" else "cons"][i])
             for side in ("for", "against") for i, a in enumerate(rec["arguments"][side])]
    pairs += list(zip(rec["conditions_struct"], data["recommendation"]["conditions"]))
    checked = 0
    for src, tok in pairs:
        if src.get("template") and "(" + "{" not in src["template"] and src["template"].count("{") == len(src["slots"]) \
                and src["text"] == src["template"].format(*(PY_FORMATS[s["format"]](s["value"]) for s in src["slots"])):
            assert _render_tokens(tok, data["datapoints"]) == src["text"]
            checked += 1
    assert checked >= 3


def test_numbers_are_linked_to_their_real_source_when_it_exists(agent):
    data = agent["data"]
    joined = " ".join(data["recommendation"]["cons"] + data["recommendation"]["pros"] + data["recommendation"]["conditions"])
    assert "{financials.max_price.central|meur}" in joined and "{deal.asking_price_eur|meur}" in joined


def test_old_outputs_without_templates_are_quoted_whole():
    dps = {}
    tok = tokenize({"text": "prix maximum de 13,4 M€"}, dps, "recommendation.cons.0", "2026-11-08")
    assert tok == "{recommendation.cons.0.text}" and dps["recommendation.cons.0.text"]["value"] == "prix maximum de 13,4 M€"


def test_adapter_never_turns_missing_into_zero(agent):
    data, reg = agent["data"], agent["final"]["registry"]
    for key, (blk, fld) in {"market.prime_rent": ("rents", "prime"), "market.prime_yield": ("yields", "prime"),
                            "market.vacancy": ("supply_demand", "vacancy_pct")}.items():
        if not reg.found(key):
            assert data["market"][blk][fld] is None
    assert data["market"]["transactions"] == [] and "market.transactions" in CONTRACT_GAPS
    assert all(c["noi"] is None and c["capex"] is None for c in data["financials"]["cashflows"])
    assert {k for k in reg.keys() if reg.get(k).status == "missing"} <= {m["key"] for m in data["missing"]}
    for path in ("market.plu.zone", "market.plu.rooftop_extension_notes"):
        assert data["datapoints"][path]["status"] == "missing" and data["datapoints"][path]["notes"] == CONTRACT_GAPS[path]


def test_risk_evidence_is_linked_to_its_registry_source(agent):
    data = agent["data"]
    evid = {k: v for k, v in data["datapoints"].items() if ".evidence." in k}
    assert evid
    reg_sources = {p["source_name"] for pts in agent["final"]["registry"].to_dict().values() for p in pts}
    for k, dp in evid.items():
        assert dp["source_name"] in reg_sources or dp["method"] in ("user_input", "computed"), (k, dp["source_name"])


def test_rationale_restates_the_agent_triggers_without_recomputing(agent):
    text, triggers = agent["data"]["recommendation"]["rationale"], agent["final"]["recommendation"]["triggers"]
    assert ("prix maximum" in text.lower()) == any("prix maximum" in t for t in triggers)
    assert ("marge du scénario central est négative" in text) == any("marge centrale" in t for t in triggers)
    assert ("risques critiques" in text) == any("risques critiques" in t for t in triggers)


def test_slider_ranges_come_from_configuration(agent):
    sliders = agent["data"]["sensitivity"]["engine_params"]["sliders"]
    assert [s["id"] for s in sliders] == [s.id for s in D.dashboard.sliders]
    assert all(f"engine.inputs.{s['field']}" in agent["data"]["datapoints"] for s in sliders)


# ================================================================== gabarit : réutilisable, autonome

def _deal_strings(cfg: dict) -> list[str]:
    out = []

    def walk(o):
        if isinstance(o, dict):
            [walk(v) for v in o.values()]
        elif isinstance(o, list):
            [walk(v) for v in o]
        elif isinstance(o, str) and len(o) >= 4 and not re.fullmatch(r"[a-z_]+|\d{4}-\d{2}-\d{2}", o):
            out.append(o)
        elif isinstance(o, (int, float)) and not isinstance(o, bool) and abs(o) >= 1000:
            out.extend([str(int(o)), f"{int(o):,}".replace(",", " ")])
    walk(cfg)
    return out


def test_no_deal_value_in_template_engine_or_generator(sample):
    code = "\n".join(p.read_text(encoding="utf-8") for p in (TEMPLATES / "dashboard.html.j2", ENGINE_PATH,
                                                            PROJECT_ROOT / "report" / "dashboard.py",
                                                            PROJECT_ROOT / "report" / "dashboard_schema.py"))
    real = json.loads((PROJECT_ROOT / "asset_config.json").read_text(encoding="utf-8"))
    for s in _deal_strings(real) + _deal_strings(sample["deal"]) + [sample["meta"]["asset_name"]]:
        if s.replace(" ", "").isdigit():      # nombre : occurrence isolée seulement
            assert not re.search(rf"(?<![\w\d]){re.escape(s)}(?!\d)", code), s
        else:
            assert s not in code, s


def test_render_is_single_offline_file_of_reasonable_size(sample):
    html = render(sample)
    assert "window.DATA = " in html and "window.HeliosEngine" in html and "{{" not in html and "{%" not in html
    assert not re.search(r"<(script|link|img|iframe)[^>]+(src|href)\s*=", html)
    assert "https://" not in (TEMPLATES / "dashboard.html.j2").read_text(encoding="utf-8")
    assert len(html.encode()) < 2_000_000                                    # « quelques Mo au plus »


def test_data_cannot_break_out_of_the_script_tag(sample):
    sample["risks"][0]["title"] = "</script><script>alert(1)</script>"
    assert render(sample).count("</script>") == 3                             # données, moteur, application


def test_other_asset_renders_without_any_mention_of_the_real_deal(sample):
    html = render(sample)
    real_name = json.loads((PROJECT_ROOT / "asset_config.json").read_text(encoding="utf-8"))["asset"]["name"]
    assert real_name not in html and sample["meta"]["asset_name"] in html


def _luminance(hexcol: str) -> float:
    r, g, b = (int(hexcol[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_text_colours_meet_wcag_aa_contrast():
    css = (TEMPLATES / "dashboard.html.j2").read_text(encoding="utf-8")
    tok = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", css))
    pairs = [("ink", "bg"), ("ink", "panel"), ("muted", "panel"), ("muted", "bg"), ("accent", "panel"), ("accent", "accent-soft"),
             ("go", "go-soft"), ("cond", "cond-soft"), ("nogo", "nogo-soft"), ("undet", "undet-soft"), ("pos", "panel"),
             ("neg", "panel"), ("muted", "soft2"), ("lvl-critique", "panel"),
             ("lvl-eleve", "panel"), ("lvl-modere", "panel"), ("lvl-faible", "panel")]
    low = [(a, b, round(contrast(tok[a], tok[b]), 2)) for a, b in pairs if contrast(tok[a], tok[b]) < 4.5]
    assert low == [], low
    # pastille discrète : élément graphique non textuel (WCAG 1.4.11, 3:1), toujours doublée du mot au survol et au focus
    assert contrast(tok["dot"], tok["panel"]) >= 3.0


# ================================================================== navigateur headless

BROWSERS = ["chromium", "firefox", "webkit"]


@pytest.fixture(scope="module")
def pw():
    sync = pytest.importorskip("playwright.sync_api")
    ctx = sync.sync_playwright().start()
    yield ctx
    ctx.stop()


def _launch(pw, name="chromium"):
    try:
        return getattr(pw, name).launch()
    except Exception as exc:  # noqa: BLE001 - navigateur non installé sur cette machine
        pytest.skip(f"{name} indisponible : {str(exc)[:80]}")


@pytest.fixture(scope="module")
def browser(pw):
    b = _launch(pw)
    yield b
    b.close()


def _open(browser, html, tmp_path, width=1360):
    f = tmp_path / "dashboard.html"
    f.write_text(html, encoding="utf-8")
    page = browser.new_page(viewport={"width": width, "height": 900})
    errors, requests = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("request", lambda r: requests.append(r.url) if not r.url.startswith(("file:", "data:")) else None)
    page.goto(f.as_uri())
    page.wait_for_timeout(250)
    return page, errors, requests


# règle 3 : aucun chiffre hors fmt (HTML) ; dans les graphiques, chaque valeur porte une clé dont la pastille est en légende
RULE3 = """() => {
  const bad = [], svgBad = [];
  const w = document.createTreeWalker(document.querySelector('main'), NodeFilter.SHOW_TEXT);
  while (w.nextNode()) { const n = w.currentNode;
    if (/\\d/.test(n.textContent) && !n.parentElement.closest('svg, .v, .pill, .date, .lbl, code, h2, h3, h4, .legend')) bad.push(n.textContent.trim()); }
  document.querySelectorAll('main svg text.val').forEach(t => { const k = t.dataset.key;
    const scope = t.closest('.card, details, section > div, .grid > div');
    if (!k || !(k in DATA.datapoints)) svgBad.push('sans donnée : ' + t.textContent);
    else if (!scope || !scope.querySelector(`.caption .src-name[data-keys~="${CSS.escape(k)}"]`)) svgBad.push('sans source en légende : ' + k); });
  return [...new Set(bad)].concat([...new Set(svgBad)]); }"""


@pytest.mark.parametrize("name", BROWSERS)
def test_opens_offline_in_each_available_browser(pw, sample, tmp_path, name):
    b = _launch(pw, name)
    try:
        page, errors, requests = _open(b, render(sample), tmp_path)
        assert errors == [] and requests == []
        assert page.locator(".section-error").count() == 0
        assert page.inner_text("#verdict .decision .label")
    finally:
        b.close()


@pytest.mark.parametrize("source", ["fixture", "agent"])
def test_every_section_renders_and_every_number_has_a_pill(browser, sample, agent, tmp_path, source):
    data = sample if source == "fixture" else agent["data"]
    page, errors, requests = _open(browser, render(data), tmp_path)
    assert errors == [] and requests == []
    assert page.locator(".section-error").count() == 0
    for sid in SECTIONS:
        assert page.locator(f"#{sid}").count() == 1, sid
        assert page.locator(f"#{sid} .message, #{sid} .decision, #{sid} .note-box, #{sid} details, #{sid}.fold").count() > 0, sid
    assert page.evaluate(RULE3) == []
    labels = {"GO": "GO", "GO_SOUS_CONDITIONS": "GO SOUS CONDITIONS", "GO_AVEC_CONTRAINTES": "GO AVEC CONTRAINTES", "NO_GO": "NO-GO", "NON_DETERMINE": "NON DÉTERMINÉ"}
    assert page.inner_text("#verdict .decision .label") == labels[data["recommendation"]["decision"]]
    page.close()


def test_the_six_case_questions_each_have_a_section(browser, sample, tmp_path):
    page, _, _ = _open(browser, render(sample), tmp_path)
    tags = page.eval_on_selector_all("section .qtag", "els => els.map(e => [e.textContent, e.closest('section').id])")
    assert dict(tags) == {"Q1": "prix", "Q2": "loyer", "Q3": "cout", "Q4": "rentabilite", "Q5": "sensibilites", "Q6": "risques"}
    page.close()


def test_max_price_is_identical_in_dashboard_and_note(browser, agent, tmp_path):
    from decision import fmt as pyfmt
    mp = agent["final"]["recommendation"]["criteria"]["price"]["max_price"]
    note = "\n".join(agent["final"]["recommendation"]["template_lines"])
    page, _, _ = _open(browser, render(agent["data"]), tmp_path)
    shown = page.inner_text("#verdict .bigcard:nth-child(2) .num")
    assert shown.replace(" ", " ").replace("\xa0", " ") == pyfmt.meur(mp) and pyfmt.meur(mp) in note
    page.close()


def test_pills_follow_the_status_code_and_open_the_source_card(browser, sample, tmp_path):
    page, _, _ = _open(browser, render(sample), tmp_path)
    cls = lambda key: page.locator(f'.pill[data-key="{key}"]').first.get_attribute("class")
    assert "pill-user" in cls("deal.asking_price_eur")
    assert "pill-calc" in cls("financials.max_price.central")
    assert "pill-hyp" in cls("targets.min_levered_irr_pct")
    assert "pill-src" in cls("market.average_rent")
    missing = [k for k, dp in sample["datapoints"].items() if dp["status"] == "missing"]
    shown = [k for k in missing if page.locator(f'.pill[data-key="{k}"]').count()]
    assert shown and all("pill-na" in cls(k) for k in shown)                 # donnée manquante : pastille « non disponible »
    page.locator('#verdict .pill[data-key="financials.max_price.central"]').first.click()
    pop = page.inner_text("#src-popover")
    assert "Formule" in pop and "Date de la donnée" in pop
    page.keyboard.press("Escape")
    assert page.locator("#src-popover").is_hidden()
    page.close()


def test_missing_values_are_shown_unavailable_and_listed(browser, sample, tmp_path):
    data = copy.deepcopy(sample)
    data["datapoints"]["financials.max_price.central"].update(value=None, status="missing")
    page, errors, _ = _open(browser, render(data), tmp_path)
    fig = page.inner_text("#verdict .bigcard:nth-child(2) .val")
    assert "non disponible" in fig and not re.search(r"\d", fig) and errors == []
    page.close()


def test_annexes_contain_only_the_sources(browser, sample, tmp_path):
    page, errors, _ = _open(browser, render(sample), tmp_path)
    assert errors == []
    assert page.locator("#annexes details").evaluate_all("els => els.map(e => e.id)") == ["sources"]
    assert page.locator("#hypotheses, #glossaire").count() == 0
    assert page.locator("#sources table tr").count() > 1
    page.close()


def test_edge_cases_of_section_11(browser, sample, tmp_path):
    data = copy.deepcopy(sample)
    data["datapoints"]["market.dvf_price_per_sqm"]["value"]["n"] = 3                     # échantillon faible
    r = data["risks"][-1]
    r.update(probability=None, impact=None, criticality=None, level="incertain", uncertain=True)
    data["datapoints"]["deal.asking_price_eur"]["confidence"] = "low"                     # confiance faible
    page, errors, _ = _open(browser, render(data), tmp_path)
    assert errors == [] and "Échantillon faible" in page.inner_text("#prix")
    assert r["title"] in page.inner_text("#risques .uncertain-box")                       # risque incertain hors matrice
    assert page.locator(f'#risques svg [data-risk="{r["id"]}"]').count() == 0
    assert "recoupement" in page.inner_text("#prix")                                      # sources divergentes : écart affiché
    assert "objectifs fixés par hypothèse" in page.inner_text("#rentabilite")
    assert page.locator('.v.is-low:has(.pill[data-key="deal.asking_price_eur"])').count() >= 1
    page.evaluate("() => safe(() => { throw new Error('test'); }, 'prix-body')")         # un graphique en erreur
    assert page.locator("#prix .section-error").count() == 1 and page.locator("#loyer .section-error").count() == 0
    page.close()


# ------------------------------------------------------------------ simulateur (section 7)

SIM_VALUES = "() => Object.fromEntries(OUTPUTS.map(([id]) => [id, DATA.datapoints['sim.' + id].value]))"


def test_simulator_reproduces_central_exactly_then_follows_python(browser, sample, tmp_path):
    page, errors, _ = _open(browser, render(sample), tmp_path)
    central = page.evaluate(SIM_VALUES)
    for k, v in central.items():                                            # curseurs à 0 : central exact
        ref = sample["datapoints"][f"financials.{k}"]["value"]
        assert v == ref or (v is not None and abs(v - ref) <= 1e-12 * max(1, abs(ref))), k
    assert page.locator("#modified-banner").is_hidden()
    page.click("#simulateur > summary")                                    # le simulateur est replié par défaut

    page.eval_on_selector("#s-rent", "e => { e.value = 10; e.dispatchEvent(new Event('input')); }")
    page.eval_on_selector("#s-works", "e => { e.value = 3; e.dispatchEvent(new Event('input')); }")
    page.wait_for_timeout(150)
    C = type("C", (), {})
    ch = []
    for f, op, v in (("relet_rent", "mult", 1.1), ("works_months", "add", 3), ("exit_date", "add_months", 3)):
        c = C(); c.field, c.op, c.value = f, op, v; ch.append(c)
    py = run_model(apply_changes(inputs_from_json(sample["sensitivity"]["engine_params"]["inputs"]), ch))
    got = page.evaluate(SIM_VALUES)
    assert got["margin_pct"] == pytest.approx(py["profitability"]["margin_on_cost"], rel=1e-9)
    assert got["exit_value"] == pytest.approx(py["exit"]["net_value"], rel=1e-9)
    assert page.locator("#modified-banner").is_visible()
    top = page.eval_on_selector("#modified-banner", "e => getComputedStyle(e).top")
    assert top == page.eval_on_selector(".topbar", "e => e.offsetHeight + 'px'")          # sous la barre, pas dedans
    assert "atteint" in page.text_content("#sim-out")

    page.click("#sim-reset")
    assert page.evaluate(SIM_VALUES) == central and page.locator("#modified-banner").is_hidden()
    assert errors == []
    page.close()


def test_slider_ranges_follow_the_specification(browser, sample, tmp_path):
    page, _, _ = _open(browser, render(sample), tmp_path)
    works = page.eval_on_selector("#s-works", "e => [+e.min, +e.max]")
    central_works = sample["sensitivity"]["engine_params"]["inputs"]["works_months"]
    assert [central_works + works[0], central_works + works[1]] == [12, 30]            # 12 à 30 mois (absolu)
    assert page.eval_on_selector("#s-rent", "e => [+e.min, +e.max]") == [-20, 20]
    assert page.eval_on_selector("#s-price", "e => [+e.min, +e.max]") == [-20, 5]
    page.close()


def test_tornado_bar_click_updates_the_matching_slider(browser, sample, tmp_path):
    page, _, _ = _open(browser, render(sample), tmp_path)
    page.locator('#tornado [data-var="rent"][data-side="pessimistic"]').click()
    page.wait_for_timeout(100)
    assert page.eval_on_selector("#s-rent", "e => +e.value") == -10                    # loyer x 0,90
    assert page.locator("#modified-banner").is_visible()
    assert page.eval_on_selector("#simulateur", "e => e.open")                           # le simulateur se déplie
    page.close()


# ------------------------------------------------------------------ impression, téléphone

def test_print_layout_one_section_per_page(browser, sample, tmp_path):
    page, _, _ = _open(browser, render(sample), tmp_path)
    page.emulate_media(media="print")
    style = lambda sel, prop: page.eval_on_selector(sel, f"e => getComputedStyle(e).{prop}")
    assert style("nav.anchors", "display") == "none" and style(".legend-bar", "display") == "none"
    assert style("#prix", "breakBefore") == "page" and style("#verdict", "breakBefore") != "page"
    assert style("#note", "breakBefore") == "page"                                        # la note s'imprime sur sa page
    assert style("#copy-note", "display") == "none" and style("#note .pill", "display") == "none"
    page.close()


def test_phone_width_has_no_horizontal_scroll(browser, sample, tmp_path):
    page, errors, _ = _open(browser, render(sample), tmp_path, width=390)
    assert page.evaluate("() => document.documentElement.scrollWidth") <= 390 and errors == []
    page.close()


# ------------------------------------------------------------------ refonte : navigation, blocs repliés, note intégrée (points A à H)

def _links(html):
    return re.findall(r"<a\b[^>]*>", html)


def test_no_link_points_to_a_local_or_relative_file(browser, sample, agent, tmp_path):
    """Point 20 : seuls les liens d'ancre (#...) et les sources externes (target=_blank, rel=noopener) sont admis,
    dans le HTML généré comme dans le DOM rendu par le script."""
    for data in (sample, agent["data"]):
        html = render(data)
        page, _, _ = _open(browser, html, tmp_path)
        links = page.eval_on_selector_all("a[href]", "els => els.map(a => [a.getAttribute('href'), a.target, a.rel])")
        assert links, "aucun lien rendu"
        bad = [l for l in links if not (l[0].startswith("#") or (re.match(r"^https?://", l[0]) and l[1] == "_blank" and "noopener" in l[2]))]
        assert bad == [], bad
        assert all(page.locator(h).count() == 1 for h, _, _ in links if h.startswith("#")), "ancre sans cible"
        assert "note_go_nogo.md" not in html and "Lire la suite" not in html
        page.close()


def test_only_the_planned_blocks_are_open_at_load(browser, sample, tmp_path):
    """Point 21 et point 3 : aucun <details open> au chargement ; chaque bloc replié a une ligne-résumé non vide."""
    html = render(sample)
    assert not re.search(r"<details[^>]*\bopen\b", html)
    page, _, _ = _open(browser, html, tmp_path)
    opened = page.eval_on_selector_all("details", "els => els.filter(d => d.open || d.hasAttribute('open')).map(d => d.id)")
    assert opened == []
    ids = set(page.eval_on_selector_all("details.fold", "els => els.map(d => d.id)"))
    assert FOLDS <= ids, FOLDS - ids
    assert all(t.strip() for t in page.eval_on_selector_all("details.fold > summary", "els => els.map(s => s.textContent)"))
    for sid in OPEN_SECTIONS:                                                       # ouverts et visibles
        assert page.locator(f"section#{sid}").is_visible() and not page.locator(f"#{sid}").evaluate("e => !!e.closest('details')"), sid
    page.click("#toggle-all")
    assert page.eval_on_selector_all("details.fold", "els => els.every(d => d.open)")
    assert page.inner_text("#toggle-all") == "Tout replier"
    page.click("#toggle-all")
    assert page.eval_on_selector_all("details.fold", "els => els.every(d => !d.open)")
    page.close()


def test_top_bar_has_five_links_and_highlights_the_active_group(browser, sample, tmp_path):
    page, _, _ = _open(browser, render(sample), tmp_path)
    assert page.eval_on_selector(".topbar", "e => e.offsetHeight") <= 52
    assert page.eval_on_selector(".topbar", "e => getComputedStyle(e).position") == "sticky"
    names = page.eval_on_selector_all("nav.anchors a", "els => els.map(a => a.textContent)")
    assert names == ["Verdict", "Marché", "Rentabilité", "Risques", "Annexes"]
    assert sample["meta"]["asset_name"] in page.inner_text(".topbar")
    assert page.locator("#modified-banner").is_hidden()                                   # point 2 : masqué à l'ouverture
    assert page.get_attribute('nav.anchors a[data-group="verdict"]', "aria-current") == "true"
    page.eval_on_selector("#risques", "e => e.scrollIntoView()")
    page.wait_for_timeout(150)
    assert page.get_attribute('nav.anchors a[data-group="risques"]', "aria-current") == "true"
    page.close()


def test_note_section_reads_data_copies_plain_text_and_has_no_dead_link(browser, sample, tmp_path):
    page, errors, _ = _open(browser, render(sample), tmp_path)
    lines = page.eval_on_selector_all("#note-text p", "els => els.length")
    assert lines == len(sample["recommendation"]["note"]) and 5 <= lines <= 10
    assert page.locator("#verdict a[href='#note']").count() == 1
    assert page.locator("#note .note-box .pill").count() > 0                              # chiffres tracés
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.click("#copy-note")
    page.wait_for_timeout(100)
    copied = page.evaluate("() => navigator.clipboard.readText()")
    assert copied.count("\n") == lines - 1 and "**" not in copied and "{" not in copied
    for bad in ("Source", "Calculé", "Saisi", "Hypothèse", "◆", "●", "■", "▲"):
        assert bad not in copied, bad
    assert errors == []
    page.close()
    data = copy.deepcopy(sample)
    data["recommendation"]["note"] = []
    page, errors, _ = _open(browser, render(data), tmp_path)
    assert "Note non générée" in page.inner_text("#note") and page.locator("#copy-note").count() == 0 and errors == []
    page.close()


def test_verdict_card_price_bar_risk_cards_and_drivers(browser, sample, tmp_path):
    page, _, _ = _open(browser, render(sample), tmp_path)
    assert page.locator("#verdict .decision .margin .pill.word").count() == 1                 # marge du central, mot court
    assert page.locator("#verdict .bigcard").count() == 3
    assert page.eval_on_selector_all("#verdict .bigcard", "els => els.map(e => e.querySelectorAll('.pill').length)") == [1, 1, 1]
    keys = set(page.eval_on_selector_all("#verdict .pricebar text.val", "els => els.map(t => t.dataset.key)"))
    assert keys == {"deal.asking_price_eur", "financials.max_price.central", "rules.negotiable_floor_price"}
    cards = page.locator("#verdict .riskcard")
    assert cards.count() == min(3, len(sample["recommendation"]["priority_risks"]))
    widths = page.eval_on_selector_all("#verdict .riskcard", "els => els.map(e => Math.round(e.getBoundingClientRect().width))")
    assert max(widths) - min(widths) <= 1 and len({round(y) for y in page.eval_on_selector_all("#verdict .riskcard", "els => els.map(e => e.getBoundingClientRect().top)")}) == 1
    assert page.locator("#verdict .riskcard .badge").count() == cards.count()
    top2 = sorted(sample["datapoints"]["sensitivity.tornado.margin_pct"]["value"], key=lambda r: -abs(r["delta_high"] - r["delta_low"]))[:2]
    drivers = page.inner_text("#verdict .drivers")
    assert top2[0]["label"] in drivers and top2[1]["label"] in drivers
    page.close()


def test_legend_word_pills_dots_and_one_line_sources(browser, sample, tmp_path):
    page, _, _ = _open(browser, render(sample), tmp_path)
    legend = page.inner_text(".legend-bar")
    for w in ("Saisi", "Calculé", "Hypothèse", "Source"):
        assert w in legend
    assert page.eval_on_selector(".legend-bar", "e => getComputedStyle(e).position") == "fixed"
    assert page.locator(".tile .pill.word").count() > 0
    dot = page.locator("#prix .message .pill.dot").first
    box = dot.bounding_box()
    assert box["width"] <= 13 and box["height"] <= 13 and dot.get_attribute("data-word")
    caps = page.eval_on_selector_all(".caption", "els => els.map(c => [c.textContent, c.querySelectorAll('.pill').length])")
    assert caps and all(n == 0 for _, n in caps) and all(("Sources : " in t) or not t.strip() or "Sources" not in t for t, _ in caps)
    page.locator("#prix .caption .src-name").first.click()
    assert "Méthode" in page.inner_text("#src-popover") and "Confiance" in page.inner_text("#src-popover")
    page.close()


def test_key_missing_banners_sweeps_heatmap_message_and_scenario_cards(browser, sample, tmp_path):
    page, _, _ = _open(browser, render(sample), tmp_path)
    # un encadré « donnée clé manquante » par donnée absente, en tête de la section concernée, et seulement alors
    absent = lambda k: sample["datapoints"].get(k, {}).get("status", "missing") in ("missing", "rejected")
    assert page.locator("#loyer .box.keymiss").count() == absent("market.prime_rent") + absent("market.vacancy")
    assert page.locator("#rentabilite .box.keymiss").count() == absent("market.prime_yield")
    data = copy.deepcopy(sample)
    for k in ("market.prime_rent", "market.vacancy", "market.prime_yield"):
        data["datapoints"][k].update(value=None, status="missing")
    page.close()
    page, _, _ = _open(browser, render(data), tmp_path)
    assert page.locator("#loyer .box.keymiss").count() == 2 and page.locator("#rentabilite .box.keymiss").count() == 1
    assert "valeur de repli" in page.inner_text("#loyer .box.keymiss >> nth=0")
    for k in ("sensitivity.sweep.relet_rent", "sensitivity.sweep.rent_free_months", "sensitivity.sweep.exit_yield"):
        assert page.locator(f'.minis svg text.val[data-key="{k}"]').count() == 2, k
    tgt = sample["datapoints"]["targets.min_margin_on_cost_pct"]["value"]
    unreached = [h for h in sample["sensitivity"]["heatmaps"] if h["metric"] == "margin_pct"
                 and not any(v is not None and v >= tgt for row in h["z"] for v in row)]
    assert page.locator("#heatmaps .box.warn").count() == len(unreached)                 # un message par grille sans case à l'objectif
    assert page.eval_on_selector_all(".scard", "els => els.map(e => e.querySelectorAll('dd').length)") == [3] * len(sample["scenarios"])
    assert page.locator(".scard .verdict-ico").count() == len(sample["scenarios"])
    page.close()


def test_negative_values_are_red_and_never_by_colour_alone(browser, sample, tmp_path):
    page, _, _ = _open(browser, render(sample), tmp_path)
    neg = page.eval_on_selector_all(".v.neg .num", "els => els.map(e => [e.textContent, getComputedStyle(e).color])")
    assert neg and all(t.lstrip().startswith(("-", "−")) for t, _ in neg)                # le signe accompagne la couleur
    assert {c for _, c in neg} == {"rgb(173, 54, 54)"}
    assert page.eval_on_selector(".v .num", "e => getComputedStyle(e).fontVariantNumeric") == "tabular-nums"
    page.close()


def test_run_no_longer_writes_a_separate_note_file():
    src = (PROJECT_ROOT / "run.py").read_text(encoding="utf-8")
    assert "note_go_nogo.md" not in src and "dashboard_v1" not in src


def test_explanation_is_shown_open_in_the_note_section(browser, sample, tmp_path):
    page, errors, _ = _open(browser, render(sample), tmp_path)
    titles = page.eval_on_selector_all("#explanation .expl-sec h4", "els => els.map(e => e.textContent)")
    assert titles == [s["title"] for s in sample["recommendation"]["explanation"]] and len(titles) >= 3
    assert page.locator("#explanation").is_visible() and not page.locator("#explanation").evaluate("e => !!e.closest('details')")
    assert page.locator("#explanation .pill").count() > 10                            # chiffres tracés
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.click("#copy-expl")
    page.wait_for_timeout(100)
    copied = page.evaluate("() => navigator.clipboard.readText()")
    assert copied.startswith("DÉCISION ET RAISONS") and "{" not in copied and errors == []
    page.close()
