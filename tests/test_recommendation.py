"""Tests de la recommandation (phase 7) : règles de décision, conditions, arguments, note go/no-go,
et rédaction par LLM sous contrôle (doublure de LLM : aucun appel réseau)."""
from __future__ import annotations

import copy
import json
from datetime import date

import pytest

from agent.graph import build_graph, recursion_limit
from agent.nodes.investment_recommendation import final_payload
from agent.state import build_initial_state
from config.schema import PROJECT_ROOT, load_config, load_defaults, load_investor_targets, load_sources_catalog
from decision import fmt
from decision.arguments import build_arguments
from decision.llm import allowed_numbers, draft, make_client, parse_numbers, unknown_numbers
from decision.rules import GO, GO_COND, NO_GO, UNDETERMINED, conditions, decide, top_risks
from report.note_go_nogo import render_markdown, template_lines
from tools.connectors import ConnectorRegistry

D = load_defaults()
RULES = D.decision_rules
ILLUSTRATIVE = {"relet_rent_eur_sqm_year": 940, "exit_yield_pct": 0.045, "euribor_3m_pct": 0.0263,
                "acquisition_fees_pct": 0.075}   # valeurs de test, non sourcées


# ------------------------------------------------------------------ faits synthétiques

def risk(rid, level, crit=None, p=None, i=None):
    return {"id": rid, "title": f"Risque {rid}", "level": level, "criticality": crit, "probability": p, "impact": i,
            "mitigations": [f"mesure {rid}"], "seller_requests": [], "financial_effect": None}


def facts(irr=0.18, margin=0.20, spread=200, multiple=1.6, max_price=30e6, asking=28e6, def_margin=0.05,
          risks=(), complete=True):
    f = {"asset": "Test", "asking_price": asking, "complete": complete, "missing_inputs": [] if complete else ["market.average_rent"],
         "required_missing": [], "reports": {},
         "targets": {"levered_irr": 0.15, "margin_on_cost": 0.15, "spread_bps": 150, "equity_multiple": 1.5,
                     "status": {"levered_irr_min": "assumption"}},
         "risks": list(risks), "seller_requests": ["audit technique"], "data_missing": [], "data_rejected": []}
    if complete:
        f.update({"central": {"levered_irr": irr, "margin_on_cost": margin, "spread_bps": spread, "equity_multiple": multiple,
                              "yield_on_cost": 0.06, "unlevered_irr": 0.09, "price_per_sqm_before": 9333,
                              "price_per_sqm_after": 8750, "total_cost_per_sqm": 15000, "gross_exit_value": 60e6},
                  "max_price": {"value": max_price, "binding_target": "levered_irr", "gap_pct": max_price / asking - 1},
                  "scenarios": {"defavorable": {"label": "Défavorable", "levered_irr": 0.02, "margin_on_cost": def_margin,
                                                "max_price": 21e6, "meets_targets": False}},
                  "assumptions": ["x"], "overrides": [], "price_references": [], "market": {}})
    return f


# ------------------------------------------------------------------ règles de décision

def test_go():
    assert decide(facts(), RULES)["decision"] == GO


def test_undetermined_when_incomplete():
    v = decide(facts(complete=False), RULES)
    assert v["decision"] == UNDETERMINED and v["criteria"] is None


@pytest.mark.parametrize("kw,reason", [
    ({"max_price": 28e6 * 0.80}, "prix maximum"),                                       # décote > 15 %
    ({"margin": -0.01}, "marge centrale"),
    ({"risks": [risk("R1", "critique", 20), risk("R2", "critique", 20), risk("R3", "critique", 17)]}, "risques critiques"),
])
def test_no_go(kw, reason):
    v = decide(facts(**kw), RULES)
    assert v["decision"] == NO_GO and any(reason in t for t in v["triggers"])


@pytest.mark.parametrize("kw", [
    {"irr": 0.13, "max_price": 27e6},                     # objectif manqué mais prix négociable
    {"risks": [risk("R2", "critique", 20)]},              # un risque critique, admis sous conditions
    {"def_margin": -0.03},                                # fragile en défavorable
])
def test_go_under_conditions(kw):
    assert decide(facts(**kw), RULES)["decision"] == GO_COND


def test_negotiable_floor_is_exact():
    floor = 28e6 * (1 - RULES.max_negotiable_discount)
    assert decide(facts(irr=0.10, max_price=floor), RULES)["decision"] == GO_COND
    assert decide(facts(irr=0.10, max_price=floor - 1), RULES)["decision"] == NO_GO


def test_conditions_follow_failed_criteria():
    f = facts(irr=0.13, max_price=27e6, def_margin=-0.02, risks=[risk("R6", "critique", 20), risk("R9", "incertain")])
    c = conditions(f, decide(f, RULES))
    assert any("27,0 M€" in x for x in c) and any("défavorable" in x for x in c)
    assert any("mesure R6" in x for x in c) and any("Risque R9" in x for x in c)
    ok = facts()
    assert not any("ramener le prix" in x for x in conditions(ok, decide(ok, RULES)))


def test_top_risks_order():
    rs = [risk("R1", "modéré", 9), risk("R2", "critique", 20), risk("R3", "incertain", p=4), risk("R4", "incertain", p=2)]
    assert [r["id"] for r in top_risks(facts(risks=rs), 3)] == ["R2", "R1", "R3"]


# ------------------------------------------------------------------ arguments

def test_arguments_are_grounded_and_bounded():
    f = facts(irr=0.13, risks=[risk("R2", "critique", 20)])
    a = build_arguments(f, RULES.max_arguments)
    assert len(a["for"]) <= RULES.max_arguments and len(a["against"]) <= RULES.max_arguments
    assert any("TRI fonds propres de 13,0 %" in x["text"] for x in a["against"])
    assert all(x["refs"] for x in a["for"] + a["against"])
    assert build_arguments(facts(complete=False), 5) == {"for": [], "against": []}


# ------------------------------------------------------------------ contrôle des nombres

def test_parse_french_numbers():
    assert parse_numbers("TRI de 13,9 %, prix 27,6 M€, coût 15 528 €/m², spread 89 pb") == [13.9, 27.6, 15528, 89]


def test_unknown_numbers_detects_invention():
    allowed = allowed_numbers({"tri": 0.139, "prix": 27.56e6, "texte": "15 528 €/m²"})
    assert unknown_numbers("TRI de 13,9 % et prix de 27,6 M€ ; 15 528 €/m²", allowed, 0.005) == []
    assert unknown_numbers("TRI de 16,2 %", allowed, 0.005) == [16.2]
    assert unknown_numbers("3 risques, 6 mois, en 2029", allowed, 0.005) == []      # énumérations et années


# ------------------------------------------------------------------ graphe et note

def cfg_with(tmp_path, **ov):
    data = json.loads((PROJECT_ROOT / "asset_config.json").read_text(encoding="utf-8"))
    data["overrides"] = ov
    p = tmp_path / "c.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return load_config(p)


def run_graph(cfg, llm=None):
    st = build_initial_state(cfg, D, load_investor_targets(), load_sources_catalog(), analysis_date=date(2026, 11, 8))
    return build_graph(ConnectorRegistry(), None, llm).invoke(st, config={"recursion_limit": recursion_limit(2)})


@pytest.fixture
def complete_rec(tmp_path):
    """Graphe complet nourri des VRAIES réponses enregistrées des sources (aucun override)."""
    import sys
    sys.path.insert(0, str(PROJECT_ROOT / "tests"))
    from realdata import run_on_fixtures
    return run_on_fixtures(tmp_path)["recommendation"]


def test_note_content_complete(complete_rec):
    lines = complete_rec["note_lines"]
    text = "\n".join(lines)
    assert 5 <= len(lines) <= 10
    assert lines[0].startswith(f"**Recommandation : {complete_rec['decision']}.**")
    for must in ("Prix maximum", "Risques principaux", "À demander au vendeur"):
        assert must in text
    assert complete_rec["drafting"]["mode"] == "template"


def test_note_content_undetermined(tmp_path):
    rec = run_graph(cfg_with(tmp_path))["recommendation"]          # aucun connecteur : données exigées absentes
    assert rec["decision"] == UNDETERMINED
    assert 5 <= len(rec["note_lines"]) <= 10
    for lab in ("prix au m² des transactions (DVF)", "loyer moyen du sous-marché", "prix d'investissement bureaux au m²"):
        assert lab in rec["note_lines"][0]
    assert "À demander au vendeur" in "\n".join(rec["note_lines"])


def test_template_note_passes_its_own_number_check(complete_rec):
    """Le contrôle n'est pas trop strict : la note de référence (chiffres du moteur) est acceptée."""
    payload = final_payload(complete_rec)
    allowed = allowed_numbers(payload)
    texts = complete_rec["template_lines"] + list(complete_rec["answers"].values())
    assert all(unknown_numbers(t, allowed, D.llm.number_tolerance_rel) == [] for t in texts)


def test_markdown_render(complete_rec):
    md = render_markdown(complete_rec, complete_rec["note_lines"])
    assert md.startswith("# Note go/no-go") and "sans LLM" in md


# ------------------------------------------------------------------ LLM sous contrôle (doublures)

class FinalOnlyLLM:
    """Répond seulement au nœud final ; les LLM des nœuds d'analyse reçoivent une réponse invalide (repli)."""
    model = "faux-llm"

    def __init__(self, response):
        self.response = response

    def complete(self, system, user):
        if "rapporteur final" in system:
            return json.dumps(self.response, ensure_ascii=False)
        return "pas du json"


class FakeLLM:
    model = "faux-llm"

    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, 0

    def complete(self, system, user):
        self.calls += 1
        assert "n'inventes" in system and "DÉCISION :" in user
        if self.error:
            raise self.error
        return self.response if isinstance(self.response, str) else json.dumps(self.response, ensure_ascii=False)


def good_answer(rec, disagree=None):
    lines = list(rec["template_lines"])
    lines[-1] = lines[-1] + " Reformulé par le LLM."          # reformulation sans nouveau chiffre
    return {"note_lines": lines, "answers": dict(rec["answers"]), "synthesis": "Synthèse sans chiffre nouveau.",
            "disagreement": {"disagrees": bool(disagree), "reason": disagree or ""}}


def test_final_llm_cannot_use_numbers_outside_analyst_reports(complete_rec):
    """Les arguments codés contiennent des chiffres que les analystes n'ont pas publiés : le LLM final
    qui les recopierait est rejeté (il ne doit s'appuyer que sur les quatre rapports)."""
    ans = good_answer(complete_rec)
    ans["synthesis"] = " ".join(a["text"] for a in complete_rec["arguments"]["for"] + complete_rec["arguments"]["against"])
    d = draft(FakeLLM(ans), D.llm, complete_rec["decision"], final_payload(complete_rec), list(complete_rec["questions"]))
    from decision.llm import allowed_numbers, unknown_numbers
    leaked = [x for a in complete_rec["arguments"]["for"] + complete_rec["arguments"]["against"]
              for x in unknown_numbers(a["text"], allowed_numbers(final_payload(complete_rec)), 0.005)]
    assert (d["mode"] == "template") == bool(leaked)


def test_llm_valid_draft_is_accepted(complete_rec):
    llm = FakeLLM(good_answer(complete_rec))
    d = draft(llm, D.llm, complete_rec["decision"], final_payload(complete_rec), list(complete_rec["questions"]))
    assert d["mode"] == "llm" and d["status"] == "accepté" and llm.calls == 1


def test_llm_invented_number_is_rejected(complete_rec):
    ans = good_answer(complete_rec)
    ans["note_lines"] = copy.deepcopy(ans["note_lines"])
    ans["note_lines"][1] = "Prix maximum : 31,4 M€ selon notre analyse."
    d = draft(FakeLLM(ans), D.llm, complete_rec["decision"], final_payload(complete_rec), list(complete_rec["questions"]))
    assert d["mode"] == "template" and "31.4" in d["status"]


def test_llm_cannot_change_decision(complete_rec):
    ans = good_answer(complete_rec)
    ans["note_lines"] = ["**Recommandation : GO.** Excellent dossier."] + ans["note_lines"][1:]
    d = draft(FakeLLM(ans), D.llm, complete_rec["decision"], final_payload(complete_rec), list(complete_rec["questions"]))
    assert d["mode"] == "template" and "décision" in d["status"]


@pytest.mark.parametrize("llm,fragment", [
    (FakeLLM("pas du json"), "échoué"),
    (FakeLLM(error=TimeoutError("lent")), "échoué"),
    (FakeLLM({"note_lines": ["**Recommandation : x**"]}), "5 à 10"),
    (FakeLLM([1, 2]), "non structurée"),
])
def test_llm_failures_fall_back_to_template(complete_rec, llm, fragment):
    d = draft(llm, D.llm, complete_rec["decision"], final_payload(complete_rec), list(complete_rec["questions"]))
    assert d["mode"] == "template" and fragment in d["status"]


def test_llm_disagreement_is_shown_not_applied(tmp_path, complete_rec):
    from realdata import run_on_fixtures
    reason = "le loyer retenu semble prudent au regard du loyer moyen publié"
    final = run_on_fixtures(tmp_path, FinalOnlyLLM(good_answer(complete_rec, reason)))
    rec = final["recommendation"]
    assert rec["decision"] == complete_rec["decision"]                       # décision inchangée
    assert rec["drafting"]["mode"] == "llm" and rec["drafting"]["disagreement"] == reason
    assert rec["note_lines"][-1].endswith("Reformulé par le LLM.")           # c'est bien le texte du LLM qui est publié
    assert rec["template_lines"] != rec["note_lines"]                         # la version de référence reste conservée
    assert any("DÉSACCORD DU LLM" in line for line in final["logs"])
    assert reason in render_markdown(rec, rec["note_lines"])


def test_no_api_key_means_no_call(monkeypatch):
    monkeypatch.delenv(D.llm.api_key_env, raising=False)
    client, status = make_client(D.llm)
    assert client is None and D.llm.api_key_env in status


def test_prompt_files_exist_and_forbid_invention():
    text = (PROJECT_ROOT / D.llm.prompt_file).read_text(encoding="utf-8")
    assert "n'inventes" in text and "JSON" in text and "désaccord" in text and "UNIQUEMENT" in text
    for node, path in D.llm.node_prompts.items():
        t = (PROJECT_ROOT / path).read_text(encoding="utf-8")
        assert "n'inventes" in t and "Information non disponible" in t and "JSON" in t, node


# ------------------------------------------------------------------ architecture : le nœud final ne voit que 4 sorties

def test_final_node_reads_only_the_four_analysis_outputs(complete_rec):
    from decision.facts import ANALYSIS_KEYS, analysis_outputs
    assert ANALYSIS_KEYS == ("financials", "risks", "sensitivity", "scenarios")

    class Guard(dict):
        def get(self, k, default=None):
            assert k in ANALYSIS_KEYS, f"le nœud final a lu « {k} »"
            return super().get(k, default)
    analysis_outputs(Guard(financials={}, risks={}, sensitivity={}, scenarios={}, registry="interdit"))
    payload = final_payload(complete_rec)
    assert set(payload["rapports_des_analystes"]) == {"financials", "risks", "sensitivity", "scenarios"}
    assert "registry" not in json.dumps(payload, default=str)


def test_all_committee_questions_answered(complete_rec):
    assert set(complete_rec["answers"]) == set(D.committee_questions)
    assert all(complete_rec["answers"][q] for q in D.committee_questions)


def test_required_data_missing_means_undetermined():
    f = facts()
    f["required_missing"] = ["market.dvf_price_per_sqm"]
    v = decide(f, RULES)
    assert v["decision"] == UNDETERMINED and "prix au m² des transactions" in v["triggers"][0]
