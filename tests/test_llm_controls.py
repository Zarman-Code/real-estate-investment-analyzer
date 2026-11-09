"""Tests des corrections issues de la critique du premier passage réel avec LLM :
faux rejet, lignes obligatoires de la note, complétude, qualificatifs trompeurs, NO-GO sans « condition » de prix,
conclusions par règles (q1, q4), réserves détaillées, journal lisible. Doublures de LLM uniquement."""
from __future__ import annotations

import json
import re

import pytest

from config.schema import load_config, load_defaults
from decision.llm import cites_value, misleading_qualifiers, missing_citations
from realdata import run_on_fixtures
from report.figures import _price_verdict
from report.node_report import figure, make_report
from report.note_go_nogo import MANDATORY_PREFIXES, is_mandatory, merge_note

D = load_defaults()


class ScriptedLLM:
    """Faux LLM : rejoue la référence de chaque nœud, avec une mutation éventuelle ; LLM final optionnel."""
    model = "faux-llm"

    def __init__(self, mutate=None, final=None):
        self.mutate, self.final = mutate, final

    def complete(self, system, user):
        if user.startswith("DÉCISION :"):
            payload = json.loads(user.split("ENTRÉES (JSON) :\n", 1)[1])
            return json.dumps(self.final(payload), ensure_ascii=False)
        p = json.loads(user)
        ans = {"summary": p["analyse_de_référence"], "answers": dict(p["réponses_de_référence"])}
        return json.dumps(self.mutate(p["nœud"], ans) if self.mutate else ans, ensure_ascii=False)


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    return run_on_fixtures(tmp_path_factory.mktemp("b"))


# ------------------------------------------------------------------ 1. pas de faux rejet

def test_echo_of_reference_is_always_accepted(tmp_path):
    """Un LLM qui recopie exactement les textes de référence ne doit JAMAIS être rejeté (cas du « 40 »)."""
    f = run_on_fixtures(tmp_path, ScriptedLLM())
    for key in ("financials", "risks", "sensitivity", "scenarios"):
        assert f[key]["report"]["drafting"]["status"] == "accepté", (key, f[key]["report"]["drafting"])


def test_number_present_only_in_reference_text_is_allowed():
    """Un nombre présent dans le texte de référence mais pas dans les chiffres (ex. nombre de transactions)."""
    figs = [figure("x", "médiane", "8 744 €/m²", 8744.0)]
    ref = "Médiane de 8 744 €/m² sur 434 transactions."
    llm = type("L", (), {"model": "m", "complete": lambda s, a, b: json.dumps({"summary": ref, "answers": {}})})()
    rep = make_report("financial_analysis", figs, ref, {}, D, llm)
    assert rep["drafting"]["status"] == "accepté"


def test_reference_numbers_are_all_declared_as_figures(base):
    """Garde-fou de cohérence : chaque nombre des textes de référence est aussi une figure du nœud."""
    from decision.llm import allowed_numbers, unknown_numbers
    rep = base["financials"]["report"]
    allowed = allowed_numbers([{"v": f["value"], "r": f["raw"]} for f in rep["figures"]])
    assert unknown_numbers(rep["answers"]["q1"], allowed, D.llm.number_tolerance_rel) == []
    assert any(f["id"] == "dvf_large_n" for f in rep["figures"]) or "grandes surfaces" not in rep["answers"]["q1"]


# ------------------------------------------------------------------ 3. complétude

def test_cites_value_ignores_sign_and_requires_value():
    assert cites_value("une marge de -25,3 % sur coût", "-25,3 %")
    assert cites_value("une perte de 25,3 %", "-25,3 %")
    assert not cites_value("une marge très négative", "-25,3 %")
    assert cites_value("sans chiffre", "n.d.")


def test_missing_citations_by_target():
    figs = [figure("a", "marge", "-25,3 %", -0.253, cite=("q4", "summary")),
            figure("r", "Baisse de la valeur de sortie", "x", 20, cite=("q6",), cite_label=True)]
    assert missing_citations({"q4": "marge de -25,3 %", "summary": "marge -25,3 %",
                              "q6": "Baisse de la valeur de sortie (critique)"}, figs) == []
    gaps = missing_citations({"q4": "marge très négative", "q6": "risques de taux"}, figs)
    assert len(gaps) == 2 and gaps[0].startswith("q4") and gaps[1].startswith("q6")
    assert missing_citations({}, figs) == []                    # réponse absente : repli sur la référence


def test_node_answer_emptied_of_figures_is_rejected(tmp_path):
    def vague(node, ans):
        if node == "scenario_analysis":
            ans["answers"]["q7"] = "Les TRI sont négatifs dans tous les scénarios."
        return ans
    f = run_on_fixtures(tmp_path, ScriptedLLM(vague))
    d = f["scenarios"]["report"]["drafting"]
    assert d["mode"] == "template" and "chiffres clés omis" in d["status"]


def test_risk_summary_omitting_a_critical_risk_is_rejected(tmp_path, base):
    crit = [r["title"] for r in base["risks"]["risks"] if r["level"] in ("critique", "élevé")]
    assert crit, "le cas de test doit comporter au moins un risque critique ou élevé"
    assert all(t in base["risks"]["report"]["summary"] for t in crit)      # la référence les cite tous

    def drop(node, ans):
        if node == "risk_analysis":
            ans["summary"] = ans["summary"].replace(crit[-1], "un autre risque")
        return ans
    f = run_on_fixtures(tmp_path, ScriptedLLM(drop))
    d = f["risks"]["report"]["drafting"]
    assert d["mode"] == "template" and crit[-1] in d["status"]


# ------------------------------------------------------------------ 4. qualificatifs

NEG = [figure("m1", "marge, scénario central", "-25,3 %", -0.253), figure("m2", "marge, scénario stress", "-51,0 %", -0.51),
       figure("t", "TRI fonds propres", "-38,0 %", -0.38)]


@pytest.mark.parametrize("text", ["Les marges sont très faibles dans les scénarios dégradés.",
                                  "La marge reste positive.", "Un TRI modeste."])
def test_favourable_qualifier_on_negative_values_is_flagged(text):
    assert misleading_qualifiers([text], NEG)


@pytest.mark.parametrize("text", ["Les marges sont négatives, de -25,3 % à -51,0 %.",
                                  "La marge n'est pas positive.", "Le risque de taux est faible ; la marge est de -25,3 %."])
def test_accurate_wording_is_not_flagged(text):
    assert misleading_qualifiers([text], NEG) == []


def test_qualifier_allowed_when_some_values_positive():
    figs = NEG + [figure("m3", "marge, scénario favorable", "4,0 %", 0.04)]
    assert misleading_qualifiers(["La marge est positive en scénario favorable."], figs) == []


def test_breakeven_and_effect_figures_are_not_counted_as_margins():
    figs = [figure("be", "Loyer (€/m²/an) : marge nulle à", "1 045 €/m²", 1045.0),
            figure("e", "effet sur la marge : Retard", "-3,0 pt", -3.0)]
    assert misleading_qualifiers(["La marge reste positive."], figs) == []


# ------------------------------------------------------------------ 2. lignes obligatoires

def test_merge_note_keeps_every_mandatory_line_verbatim():
    ref = ["**Recommandation : NO-GO.** x.", "Prix maximum : 13,4 M€.", "Scénario central : a.", "Robustesse : b.",
           "Risques principaux : c.", "Pour un réexamen : d.", "À demander au vendeur : e, f, g.", "Réserves : h."]
    llm = ["**Recommandation : NO-GO.** Reformulé.", "Rentabilité négative.", "À demander au vendeur : e."]
    out = merge_note(llm, ref)
    assert out[0] == llm[0] and out[1] == ref[1]
    for line in ref:
        if is_mandatory(line):
            assert line in out
    assert "À demander au vendeur : e." not in out                       # version tronquée du LLM écartée
    assert "Rentabilité négative." in out and len(out) <= 10


def test_merge_note_respects_ten_lines():
    ref = ["**Recommandation : GO.**"] + [f"{p} : z." for p in MANDATORY_PREFIXES]
    out = merge_note(["**Recommandation : GO.**"] + [f"libre {i}" for i in range(9)], ref)
    assert len(out) <= 10 and all(f"{p} : z." in out for p in MANDATORY_PREFIXES)


def test_final_llm_cannot_drop_reserves(tmp_path):
    def final(payload):
        return {"note_lines": [payload["note_de_référence"][0], "Rentabilité négative au prix demandé."],
                "answers": {}, "synthesis": "", "disagreement": {"disagrees": False, "reason": ""}}
    f = run_on_fixtures(tmp_path, ScriptedLLM(final=final))
    rec = f["recommendation"]
    assert rec["drafting"]["mode"] == "llm"
    for line in rec["template_lines"]:
        if is_mandatory(line):
            assert line in rec["note_lines"], line
    assert any(x.startswith("Réserves") for x in rec["note_lines"])


# ------------------------------------------------------------------ 5. NO-GO

def test_no_go_conditions_are_a_reconsideration_threshold(tmp_path):
    rec = run_on_fixtures(tmp_path, without=("market_reports", "cushman_marketbeat"))["recommendation"]   # NO-GO par le prix
    assert rec["decision"] == "NO-GO"
    assert not any("ramener le prix" in c for c in rec["conditions"])
    assert any("réexaminé" in c for c in rec["conditions"])
    assert any(x.startswith("Pour un réexamen") for x in rec["template_lines"])
    assert not any(x.startswith("Conditions") for x in rec["template_lines"])


def test_final_llm_pushing_price_cut_under_no_go_is_rejected(tmp_path):
    def final(payload):
        return {"note_lines": [payload["note_de_référence"][0]], "answers": {},
                "synthesis": "Il est crucial de ramener le prix au niveau du prix maximum.",
                "disagreement": {"disagrees": False, "reason": ""}}
    rec = run_on_fixtures(tmp_path, ScriptedLLM(final=final), without=("market_reports", "cushman_marketbeat"))["recommendation"]
    assert rec["decision"] == "NO-GO"                                      # sans données de marché : NO-GO par le prix
    assert rec["drafting"]["mode"] == "template" and "NO-GO" in rec["drafting"]["status"]


# ------------------------------------------------------------------ 6. conclusions par règles

@pytest.mark.parametrize("gap,works,word", [(0.067, True, "élevé"), (0.02, True, "en ligne"), (-0.10, True, "cohérent"),
                                            (0.02, False, "en ligne"), (-0.10, False, "inférieur")])
def test_price_verdict(gap, works, word):
    txt = _price_verdict(gap, 0.05, works)
    assert word in txt and (("restructurer" in txt) == works)


def test_q1_and_q4_conclude(base):
    rep = base["financials"]["report"]["answers"]
    assert re.search(r"Le prix demandé (est donc élevé|est en ligne|est inférieur)", rep["q1"])
    c = base["financials"]["results"]
    rate, yoc = c["returns"]["interest_rate"], c["profitability"]["yield_on_cost"]
    assert ("levier est donc négatif" in rep["q4"]) == (yoc < rate)
    assert ("levier est positif" in rep["q4"]) == (yoc >= rate)


def test_no_deal_wording_in_acquisition_note(base):
    note = base["financials"]["price_comparison"]["note"]
    cfg = load_config()
    assert cfg.asset.condition in note and f"DPE {cfg.asset.dpe_current}" in note


# ------------------------------------------------------------------ 7. réserves détaillées

def test_fallback_inputs_are_named_in_reserves(tmp_path):
    base = run_on_fixtures(tmp_path, without=("market_reports", "cushman_marketbeat"))   # sans analyses de marché
    rec = base["recommendation"]
    fb = {x["name"] for x in rec["facts"]["fallbacks"]}
    assert {"relet_rent", "exit_yield"} <= fb                           # pas de loyer ni de taux prime dans les sources
    reserves = next(x for x in rec["template_lines"] if x.startswith("Réserves"))
    assert "loyer moyen de seconde main" in reserves and "implicite" in reserves
    if "rent_free_months" in fb:
        assert "franchise" in reserves
    if "acq_fees_pct" in fb:
        assert "frais d'acquisition" in reserves


def test_override_removes_the_fallback_flag(tmp_path):
    from test_recommendation import cfg_with
    f = run_on_fixtures(tmp_path, config=cfg_with(tmp_path, relet_rent_eur_sqm_year=900, exit_yield_pct=0.045))
    fb = {x["name"] for x in f["recommendation"]["facts"]["fallbacks"]}
    assert not {"relet_rent", "exit_yield"} & fb


# ------------------------------------------------------------------ journal lisible

def test_sensitivity_log_has_no_raw_floats(base):
    lines = [x for x in base["logs"] if x.strip().startswith("seuil")]
    assert lines and not any(re.search(r"\d\.\d{5,}", x) or "None" in x for x in lines)


def test_final_llm_answer_emptied_of_figures_is_rejected(tmp_path):
    def final(payload):
        return {"note_lines": [payload["note_de_référence"][0]],
                "answers": {"q4": "Le yield on cost est inférieur au taux de sortie."},
                "synthesis": "", "disagreement": {"disagrees": False, "reason": ""}}
    rec = run_on_fixtures(tmp_path, ScriptedLLM(final=final))["recommendation"]
    assert rec["drafting"]["mode"] == "template" and "chiffres clés omis" in rec["drafting"]["status"]
    assert "q4" in rec["drafting"]["status"]


def test_market_reports_remove_the_rent_and_yield_fallbacks(base):
    fb = {x["name"] for x in base["recommendation"]["facts"]["fallbacks"]}
    assert "relet_rent" not in fb and "exit_yield" not in fb            # loyer prime et taux prime trouvés
