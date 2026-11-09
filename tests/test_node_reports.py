"""Tests des rapports par nœud (un LLM par nœud d'analyse) et des règles de communication.
Doublures de LLM uniquement : aucun appel réseau."""
from __future__ import annotations

import json

import pytest

from config.schema import load_defaults
from realdata import run_on_fixtures
from report.node_report import NOT_AVAILABLE, figure, make_report

D = load_defaults()
NODE_KEYS = {"financial_analysis": "financials", "risk_analysis": "risks",
             "sensitivity_analysis": "sensitivity", "scenario_analysis": "scenarios"}


class NodeLLM:
    """Faux LLM de nœud : renvoie, pour chaque nœud, la réponse fournie (ou rejoue la référence)."""
    model = "faux-llm-noeud"

    def __init__(self, mutate=None):
        self.calls, self.mutate = [], mutate

    def complete(self, system, user):
        payload = json.loads(user)
        node = payload["nœud"]
        self.calls.append(node)
        assert "n'inventes" in system and "Information non disponible" in system
        ans = {"summary": payload["analyse_de_référence"] + " Analyse reformulée.",
               "answers": dict(payload["réponses_de_référence"])}
        return json.dumps(self.mutate(node, ans) if self.mutate else ans, ensure_ascii=False)


@pytest.fixture(scope="module")
def final(tmp_path_factory):
    return run_on_fixtures(tmp_path_factory.mktemp("r"))


# ------------------------------------------------------------------ rapports de référence (sans LLM)

def test_each_analysis_node_publishes_a_report(final):
    for node, key in NODE_KEYS.items():
        rep = final[key]["report"]
        assert rep["node"] == node and rep["summary"] and rep["drafting"]["mode"] == "template"
        assert set(rep["answers"]) == {q for q, v in D.committee_questions.items() if v.node == node}
        assert all(f["value"] for f in rep["figures"])


def test_reference_answers_use_only_their_own_figures(final):
    from decision.llm import allowed_numbers, unknown_numbers
    for key in NODE_KEYS.values():
        rep = final[key]["report"]
        allowed = allowed_numbers([{"v": f["value"], "r": f["raw"]} for f in rep["figures"]])
        for text in [rep["summary"], *rep["answers"].values()]:
            assert unknown_numbers(text, allowed, D.llm.number_tolerance_rel) == [], (key, text[:80])


def test_decision_critical_data_are_sourced(final):
    figs = {f["id"]: f for f in final["financials"]["report"]["figures"]}
    for fid in ("dvf_median", "immostat_rent", "investment_price_sqm", "yoc", "exit_yield"):
        assert fid in figs, fid
    assert "DVF+" in figs["dvf_median"]["source"] and "ImmoStat" in figs["immostat_rent"]["source"]
    assert all(final["financials"]["required_data"].values())


@pytest.fixture(scope="module")
def final_no_report(tmp_path_factory):
    """Même analyse sans les analyses de marché : ni loyer prime, ni taux prime, ni vacance."""
    return run_on_fixtures(tmp_path_factory.mktemp("nr"), without=("market_reports", "cushman_marketbeat"))


def test_exit_yield_is_implied_market_yield_when_no_prime_yield(final_no_report):
    final = final_no_report
    ey = next(p for p in final["financials"]["inputs"] if p["name"] == "exit_yield")
    assert ey["reference"] == "market.average_rent / market.investment_price_per_sqm"
    rent = next(f for f in final["financials"]["report"]["figures"] if f["id"] == "immostat_rent")["raw"]
    price = next(f for f in final["financials"]["report"]["figures"] if f["id"] == "investment_price_sqm")["raw"]
    assert ey["value"] == pytest.approx(rent / price)


def test_exit_yield_is_prime_yield_plus_premium_when_published(final):
    ey = next(p for p in final["financials"]["inputs"] if p["name"] == "exit_yield")
    assert ey["reference"] == "market.prime_yield + prime de risque" and "4,00 %" in ey["note"] and "25 pb" in ey["note"]
    assert final["registry"].get("market.prime_yield").source_name.startswith("Rapports de marché")


def test_relet_rent_is_immostat_rent_when_no_prime_rent(final_no_report):
    r = next(p for p in final_no_report["financials"]["inputs"] if p["name"] == "relet_rent")
    assert r["origin"] == "registry" and r["reference"] == "market.average_rent"


def test_relet_rent_sits_between_average_and_prime_rent_of_the_report(final):
    r = next(p for p in final["financials"]["inputs"] if p["name"] == "relet_rent")
    assert r["origin"] == "computed" and not r["fallback"] and "1 250 €/m²/an" in r["note"]
    assert final["registry"].value("market.prime_rent") == 1250.0                 # MarketBeat, ligne Paris QCA


def test_missing_complementary_data_is_said_not_filled(final_no_report):
    q2 = final_no_report["financials"]["report"]["answers"]["q2"]
    assert f"{NOT_AVAILABLE} : loyer prime" in q2


# ------------------------------------------------------------------ règle : risque non caractérisé = non communiqué

def test_uncharacterised_risks_are_not_communicated(final):
    ids = {r["id"] for r in final["risks"]["risks"]}
    excluded = {e["id"] for e in final["risks"]["excluded"]}
    assert "R9" in excluded and "R9" not in ids
    rep = final["risks"]["report"]
    text = rep["summary"] + " ".join(rep["answers"].values())
    assert "Risques naturels" not in text                                   # absent de l'analyse
    assert all(r["probability"] is not None for r in final["risks"]["risks"])
    note = "\n".join(final["recommendation"]["note_lines"])
    assert "Risques naturels" not in note


# ------------------------------------------------------------------ LLM par nœud, sous contrôle

def test_each_node_llm_is_called_and_accepted(tmp_path):
    llm = NodeLLM()
    f = run_on_fixtures(tmp_path, llm)
    assert set(NODE_KEYS) <= set(llm.calls)
    for key in NODE_KEYS.values():
        rep = f[key]["report"]
        assert rep["drafting"]["mode"] == "llm" and rep["summary"].endswith("Analyse reformulée.")
        assert rep["reference_summary"] != rep["summary"]                    # la référence reste conservée


def test_node_llm_inventing_a_number_is_rejected(tmp_path):
    def invent(node, ans):
        if node == "financial_analysis":
            ans["answers"]["q2"] = "Le loyer prime du secteur atteint 1 150 €/m²/an."   # chiffre inventé
        return ans
    f = run_on_fixtures(tmp_path, NodeLLM(invent))
    rep = f["financials"]["report"]
    assert rep["drafting"]["mode"] == "template" and "1150" in rep["drafting"]["status"]
    assert "1 150" not in rep["answers"]["q2"]
    assert f["risks"]["report"]["drafting"]["mode"] == "llm"                 # les autres nœuds ne sont pas affectés


def test_node_llm_cannot_answer_other_nodes_questions(tmp_path):
    def overreach(node, ans):
        if node == "risk_analysis":
            ans["answers"]["q1"] = "Le prix est cohérent."
        return ans
    f = run_on_fixtures(tmp_path, NodeLLM(overreach))
    assert "questions inconnues" in f["risks"]["report"]["drafting"]["status"]


def test_node_llm_sees_only_its_own_results(tmp_path):
    seen = {}

    class Spy(NodeLLM):
        def complete(self, system, user):
            seen[json.loads(user)["nœud"]] = user
            return super().complete(system, user)
    run_on_fixtures(tmp_path, Spy())
    assert "Firmio" in seen["financial_analysis"] and "Firmio" not in seen["risk_analysis"]
    assert "amplitude" in seen["sensitivity_analysis"] and "amplitude" not in seen["scenario_analysis"]


def test_make_report_fills_missing_answers():
    rep = make_report("scenario_analysis", [figure("x", "x", "1,0 %", 0.01)], "résumé", {}, D, None)
    assert rep["answers"] == {"q7": f"{NOT_AVAILABLE}."}


def test_sensitivity_text_matches_margin_sign(final):
    """Erreur trouvée à la relecture : « la marge reste positive » alors qu'elle est négative au central."""
    negative = final["financials"]["results"]["profitability"]["margin_on_cost"] < 0
    q5 = final["sensitivity"]["report"]["answers"]["q5"]
    if negative:
        assert "reste positive" not in q5 and "reste négative" in q5


def test_cost_comparison_states_direction(final):
    q3 = final["financials"]["report"]["answers"]["q3"]
    assert "au-dessus de cette référence" in q3 or "en dessous de cette référence" in q3
