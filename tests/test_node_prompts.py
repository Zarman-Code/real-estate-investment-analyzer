"""Prompts des quatre nœuds d'analyse (prompts/*_v1.md) : compatibilité avec le code qui les utilise.

1. Contrat : format JSON et noms des entrées identiques à ce que le code injecte et contrôle.
2. Périmètre : ce que chaque prompt dit recevoir correspond aux chiffres réellement transmis par le code.
3. Validateur : des sorties qui suivent les prompts passent ; des sorties qui violent leurs règles sont rejetées
   (calcul, chiffre de source, chiffre obligatoire omis, perte qualifiée favorablement, note de risque modifiée).
Le comportement d'un vrai LLM se vérifie avec scripts/check_node_prompts.py (clé d'API nécessaire).
"""
from __future__ import annotations

import copy
import json
import re

import pytest

from config.schema import PROJECT_ROOT, load_defaults
from report.node_report import NOT_AVAILABLE, make_report, required_citations

from realdata import run_on_fixtures

D = load_defaults()
NODES = {"financial_analysis": "financials", "risk_analysis": "risks", "sensitivity_analysis": "sensitivity",
         "scenario_analysis": "scenarios"}
FORMAT = ('FORMAT : un objet JSON strict, sans texte autour :\n'
          '{"summary": "<analyse de 3 à 6 phrases>", "answers": {"<id de question>": "<réponse de 1 à 3 phrases>"}}\n'
          'Tu réponds à TOUTES les questions fournies, et seulement à elles.\n')
INPUTS = ("« chiffres »", "« chiffres_obligatoires »", "« questions »", "« analyse_de_référence »", "« réponses_de_référence »")


def prompt(node: str) -> str:
    return (PROJECT_ROOT / D.llm.node_prompts[node]).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def final(tmp_path_factory):
    return run_on_fixtures(tmp_path_factory.mktemp("p"))


class FixedLLM:
    """Faux LLM qui renvoie une réponse donnée et garde la consigne et l'entrée reçues."""
    model = "faux-llm"

    def __init__(self, answer):
        self.answer, self.system, self.user = answer, None, None

    def complete(self, system, user):
        self.system, self.user = system, user
        return self.answer if isinstance(self.answer, str) else json.dumps(self.answer, ensure_ascii=False)


def redraft(final, node, answer, mutate_figures=None):
    rep = final[NODES[node]]["report"]
    figures = copy.deepcopy(rep["figures"])
    if mutate_figures:
        mutate_figures(figures)
    llm = FixedLLM(answer)
    out = make_report(node, figures, rep["reference_summary"], dict(rep["reference_answers"]), D, llm)
    return out, llm


def compliant(final, node, extra=""):
    """Sortie d'un LLM qui suit le prompt : chiffres obligatoires repris tels quels, aucun chiffre nouveau."""
    rep = final[NODES[node]]["report"]
    req = required_citations(rep["figures"])
    text = lambda target: " ; ".join(req.get(target, [])) + "." + extra
    return {"summary": "Analyse : " + text("summary"), "answers": {q: "Réponse : " + text(q) for q in rep["questions"]}}


# ------------------------------------------------------------------ 1. contrat d'entrée et de sortie

@pytest.mark.parametrize("node", NODES)
def test_output_format_and_inputs_are_those_of_the_code(node, final):
    p = prompt(node)
    assert p.endswith(FORMAT)                                          # format JSON d'origine, à l'identique
    assert all(k in p for k in INPUTS)                                  # entrées nommées comme dans le code
    llm = redraft(final, node, compliant(final, node))[1]
    assert set(json.loads(llm.user)) == {"nœud", "chiffres", "chiffres_obligatoires", "questions",
                                         "analyse_de_référence", "réponses_de_référence"}
    assert llm.system == p                                              # le prompt envoyé est bien ce fichier


@pytest.mark.parametrize("node", NODES)
def test_every_prompt_forbids_calculation_and_invention(node):
    p = prompt(node)
    for rule in ("AUCUN CALCUL", "Tu n'inventes", NOT_AVAILABLE, "chiffres_obligatoires", "valeur exacte" if node != "risk_analysis"
                 else "libellé exact", "perte", "Tu ne formules pas la recommandation finale"):
        assert rule in p, (node, rule)
    assert not re.search(r"tu peux (effectuer|calculer)", p, re.IGNORECASE)


# ------------------------------------------------------------------ 2. périmètre : ce que le prompt dit recevoir existe

def test_financial_prompt_matches_its_figures(final):
    labels = [f["label"] for f in final["financials"]["report"]["figures"]]
    for ref in ("prix demandé", "prix maximum (objectifs du fonds)", "médiane DVF des locaux d'activité du secteur",
                "loyer de relocation retenu", "loyer moyen de seconde main du sous-marché", "coût total au m² après travaux",
                "valeur de sortie brute au m²", "yield on cost", "taux de sortie", "coût de la dette (Euribor + marge)"):
        assert ref in labels, ref
    assert not any("valeur de marché" in x for x in labels)            # le prompt l'annonce : aucune valeur de marché
    assert "locaux d'activité" in prompt("financial_analysis")


def test_risk_prompt_matches_its_figures(final):
    figs = final["risks"]["report"]["figures"]
    notes = [f for f in figs if f.get("cite_label")]
    assert notes and all(re.fullmatch(r"P \d/5 x I \d/5 = \d{1,2}/25, (critique|élevé|modéré|faible)", f["value"]) for f in notes)
    crit = [int(re.search(r"= (\d+)/25", f["value"])[1]) for f in notes]
    assert crit == sorted(crit, reverse=True)                           # « déjà classée par le moteur »
    for f in notes:
        assert {f"effet sur la marge : {f['label']}", f"effet sur le TRI fonds propres : {f['label']}"} <= {g["label"] for g in figs}
    excluded = {e["title"] for e in final["risks"]["excluded"]}
    assert excluded and not excluded & {f["label"] for f in notes}     # risques exclus jamais transmis


def test_sensitivity_prompt_matches_its_figures(final):
    figs = final["sensitivity"]["report"]["figures"]
    kinds = {re.sub(r"^.*?: ", "", f["label"]) if f["label"].startswith("amplitude") else f["label"].split(" : ")[-1] for f in figs}
    assert kinds <= {"valeur centrale", "marge nulle à", "TRI à l'objectif à"} | {f["label"].split(" : ")[1] for f in figs if f["label"].startswith("amplitude")}
    tested = {r["label"] for r in final["sensitivity"]["tornado"]["rows"]}
    shown = {f["label"].split(" : ", 1)[1] for f in figs if f["label"].startswith("amplitude")}
    assert shown <= tested and len(tested) == 8                        # 8 variables testées, les plus influentes transmises
    assert not any("grille" in f["label"] or "heatmap" in f["id"] for f in figs)   # grilles non transmises : le prompt le dit
    p = prompt("sensitivity_analysis")
    assert "grilles croisées" in p and "prix d'acquisition" in p and "aucun seuil de TRI négatif" in p


def test_scenario_prompt_matches_its_figures(final):
    figs = final["scenarios"]["report"]["figures"]
    names = {f["label"].split("scénario ", 1)[1] for f in figs}
    assert names == {"Favorable", "Central", "Défavorable", "Stress"}
    assert {f["label"].split(", scénario")[0] for f in figs} == {"TRI fonds propres", "marge", "prix maximum"}
    assert not any("multiple" in f["label"] or "valeur de sortie" in f["label"] for f in figs)


# ------------------------------------------------------------------ 3. validateur : sorties conformes acceptées

@pytest.mark.parametrize("node", NODES)
def test_output_following_the_prompt_is_accepted(node, final):
    out = redraft(final, node, compliant(final, node))[0]
    assert out["drafting"]["mode"] == "llm" and out["drafting"]["status"] == "accepté", out["drafting"]


def test_missing_information_wording_is_accepted(final):
    ans = compliant(final, "scenario_analysis", f" Multiple de fonds propres : {NOT_AVAILABLE} (non transmis).")
    assert redraft(final, "scenario_analysis", ans)[0]["drafting"]["mode"] == "llm"


def test_negative_results_written_as_losses_are_accepted(final):
    ans = compliant(final, "scenario_analysis", " Le scénario Stress est déficitaire : c'est une perte.")
    assert redraft(final, "scenario_analysis", ans)[0]["drafting"]["status"] == "accepté"


def test_value_not_available_in_figures_is_accepted(final):
    """Valeur absente (« n.d. ») : le texte qui l'écrit « Information non disponible » passe."""
    def drop(figs):
        f = next(x for x in figs if x["id"] == "max_price")
        f["value"], f["raw"] = "n.d.", None
    ans = compliant(final, "financial_analysis")
    ans["summary"] = ans["summary"].replace("33,4 M€", NOT_AVAILABLE)
    assert redraft(final, "financial_analysis", ans, drop)[0]["drafting"]["mode"] == "llm"


# ------------------------------------------------------------------ 3 bis. validateur : violations rejetées

def test_llm_calculation_is_rejected(final):
    """« L'écart est de 475 €/m² » : un écart calculé par le LLM (9 808 - 9 333) n'est pas dans les chiffres."""
    ans = compliant(final, "financial_analysis")
    ans["answers"]["q1"] += " L'écart est de 475 €/m²."
    out = redraft(final, "financial_analysis", ans)[0]
    assert out["drafting"]["mode"] == "template" and "475" in out["drafting"]["status"]


def test_numbers_copied_from_a_source_field_are_rejected(final):
    """Règle 4 des prompts : le champ « source » d'un chiffre peut contenir des nombres (formule du taux de sortie) ;
    les recopier fait rejeter le texte, d'où la consigne de ne citer que le NOM de la source."""
    ans = compliant(final, "financial_analysis")
    ans["answers"]["q4"] += " Taux prime de 4,00 % plus 25 pb."
    assert redraft(final, "financial_analysis", ans)[0]["drafting"]["mode"] == "template"


@pytest.mark.parametrize("node", NODES)
def test_omitted_mandatory_figure_is_rejected(node, final):
    ans = compliant(final, node)
    ans["summary"] = "Analyse sans les chiffres obligatoires."
    out = redraft(final, node, ans)[0]
    assert out["drafting"]["mode"] == "template" and "omis" in out["drafting"]["status"]


def test_loss_qualified_favourably_is_rejected(final):
    def all_negative(figs):
        for f in figs:
            if f["id"].endswith("_margin"):
                f["raw"] = -abs(f["raw"])
                f["value"] = "-" + f["value"].lstrip("-")
    ans = compliant(final, "scenario_analysis", " La marge reste positive dans l'ensemble.")
    out = redraft(final, "scenario_analysis",
                  {"summary": ans["summary"].replace("70,2 %", "-70,2 %").replace("30,4 %", "-30,4 %").replace("17,6 %", "-17,6 %"),
                   "answers": {q: a.replace("70,2 %", "-70,2 %").replace("30,4 %", "-30,4 %").replace("17,6 %", "-17,6 %")
                               for q, a in ans["answers"].items()}}, all_negative)[0]
    assert out["drafting"]["mode"] == "template" and "qualificatif" in out["drafting"]["status"]


def test_modified_risk_note_is_rejected(final):
    """Les notes /5 sont de petits entiers qui échappent au contrôle général des nombres : contrôle dédié."""
    figs = final["risks"]["report"]["figures"]
    r = next(f for f in figs if f.get("cite_label") and f["cite"])
    ans = compliant(final, "risk_analysis")
    good = redraft(final, "risk_analysis", {"summary": ans["summary"] + f" {r['label']} ({r['value']}).",
                                            "answers": ans["answers"]})[0]
    assert good["drafting"]["status"] == "accepté"
    p = re.search(r"P (\d)/5", r["value"])[1]
    forged = r["value"].replace(f"P {p}/5", f"P {1 if p != '1' else 2}/5")
    bad = redraft(final, "risk_analysis", {"summary": ans["summary"] + f" {r['label']} ({forged}).",
                                           "answers": ans["answers"]})[0]
    assert bad["drafting"]["mode"] == "template" and "note de risque modifiée" in bad["drafting"]["status"]
    level = re.search(r", (\w+)$", r["value"])[1]
    other = "modéré" if level != "modéré" else "critique"
    bad2 = redraft(final, "risk_analysis", {"summary": ans["summary"] + f" {r['label']} ({other}).", "answers": ans["answers"]})[0]
    assert "note de risque modifiée" in bad2["drafting"]["status"]


def test_reference_texts_still_pass_the_new_risk_check(final):
    rep = final["risks"]["report"]
    out = redraft(final, "risk_analysis", {"summary": rep["reference_summary"], "answers": rep["reference_answers"]})[0]
    assert out["drafting"]["status"] == "accepté"


@pytest.mark.parametrize("bad", ["pas du json", {"summary": ""}, {"summary": "x", "answers": {"q99": "y"}}])
def test_malformed_outputs_fall_back_to_reference(final, bad):
    out = redraft(final, "financial_analysis", bad)[0]
    assert out["drafting"]["mode"] == "template" and out["summary"] == final["financials"]["report"]["reference_summary"]
