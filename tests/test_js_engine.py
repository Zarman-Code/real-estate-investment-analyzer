"""Non-régression : le moteur JavaScript du dashboard (report/templates/engine.js) doit donner EXACTEMENT
les mêmes résultats que le moteur Python (finance/*.py), aux erreurs d'arrondi flottant près, sur un jeu de cas
qui couvre toutes les branches : central, chaque sensibilité, chaque scénario, cession avant le bail,
base LTV « prix », franchise nulle, bail en cours de mois, absence de TRI, arrondis des champs entiers."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from config.schema import PROJECT_ROOT, load_config, load_defaults
from finance.engine import run_model
from finance.export import inputs_to_json
from finance.inputs import Lease, resolve_inputs
from finance.max_price import max_price
from finance.sensitivity import scenario_inputs
from finance.variations import apply_changes
from realdata import run_on_fixtures

NODE = shutil.which("node")
ENGINE = PROJECT_ROOT / "report" / "js" / "engine.js"
RUNNER = Path(__file__).parent / "js_runner.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js absent : test du moteur JS ignoré")
D = load_defaults()
TARGETS = {"levered_irr": 0.15, "margin_on_cost": 0.15}


def run_js(cases: list[dict]) -> list[dict]:
    p = subprocess.run([NODE, str(RUNNER), str(ENGINE)], input=json.dumps(cases), capture_output=True,
                       text=True, encoding="utf-8", timeout=120)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def same(py, js, path="") -> list[str]:
    """Différences entre deux structures (nombres : tolérance relative 1e-9)."""
    if isinstance(py, date):
        py = py.isoformat()
    if py is None or js is None:
        return [] if py is None and js is None else [f"{path}: {py!r} != {js!r}"]
    if isinstance(py, bool) or isinstance(js, bool):
        return [] if py == js else [f"{path}: {py!r} != {js!r}"]
    if isinstance(py, (int, float)) and isinstance(js, (int, float)):
        ok = abs(py - js) <= 1e-9 * max(1.0, abs(py))
        return [] if ok else [f"{path}: {py!r} != {js!r}"]
    if isinstance(py, dict):
        if set(py) != set(js):
            return [f"{path}: clés {sorted(set(py) ^ set(js))}"]
        return [d for k in py for d in same(py[k], js[k], f"{path}.{k}")]
    if isinstance(py, (list, tuple)):
        if len(py) != len(js):
            return [f"{path}: longueur {len(py)} != {len(js)}"]
        return [d for i, (a, b) in enumerate(zip(py, js)) for d in same(a, b, f"{path}[{i}]")]
    return [] if py == js else [f"{path}: {py!r} != {js!r}"]


@pytest.fixture(scope="module")
def central(tmp_path_factory):
    final = run_on_fixtures(tmp_path_factory.mktemp("js"))
    ri = resolve_inputs(final["config"], final["registry"], D)
    assert ri.complete
    return ri.inputs


def _change_cases(inp):
    """(nom, changements, valeur imposée) : toutes les variables de sensibilité + seuils + cas limites."""
    out = []
    for name, var in D.sensitivity.variables.items():
        out += [(f"{name}_opt", var.optimistic, None), (f"{name}_pes", var.pessimistic, None)]
    for name, spec in D.sensitivity.breakeven.items():
        lo, hi = spec.bounds
        out.append((f"be_{name}", spec.changes, lo + (hi - lo) * 0.37))
    class C:  # changement ad hoc
        def __init__(self, field, op, value=None):
            self.field, self.op, self.value = field, op, value
    out += [("void_without_exit_shift", [C("letting_void_months", "add", 12)], None),   # cession avant le bail
            ("half_even_rounding", [C("works_months", "add", 2.5)], None),             # round() de Python
            ("set_field", [C("relet_rent", "set_field", "capex_per_sqm")], None)]
    return [(n, ch, v) for n, ch, v in out]


def test_js_matches_python_on_all_branches(central):
    cases, expected = [], []

    def add(name, inp, targets=None):
        cases.append({"name": name, "inputs": inputs_to_json(inp), "targets": targets})
        expected.append((name, run_model(inp), max_price(inp, targets) if targets else None, None))

    add("central", central, TARGETS)
    for name, spec in D.scenarios.items():
        add(f"scenario_{name}", scenario_inputs(central, spec, D.sensitivity), TARGETS)
    add("ltv_basis_price", replace(central, ltv_basis="price"), TARGETS)
    add("no_rent_free", replace(central, rent_free_months=0.0))
    add("lease_ends_mid_month", replace(central, current_leases=(Lease(1200, 520, date(2027, 3, 15)),
                                                                  Lease(400, 610, date(2027, 9, 30)))))
    add("no_irr_huge_price", replace(central, price=central.price * 10))
    add("euribor_floor_binding", replace(central, euribor=-0.01, euribor_floor=0.0))

    for name, changes, value in _change_cases(central):
        changed = apply_changes(central, changes, value=value)
        cases.append({"name": name, "inputs": inputs_to_json(central),
                      "changes": [{"field": c.field, "op": c.op, "value": getattr(c, "value", None)} for c in changes],
                      "value": value})
        expected.append((name, run_model(changed), None, inputs_to_json(changed)))

    results = run_js(cases)
    assert len(results) == len(expected)
    diffs = []
    for (name, py, py_mp, py_changed), js in zip(expected, results):
        diffs += [f"{name}{d}" for d in same(py, js["result"])]
        if py_mp:
            mp = {k: v for k, v in py_mp.items() if k != "by_target"}
            js_mp = {k: v for k, v in js["max_price"].items() if k != "by_target"}
            diffs += [f"{name}.max_price{d}" for d in same(mp, js_mp)]
            for m in py_mp["by_target"]:
                diffs += [f"{name}.max_price.{m}{d}" for d in same(py_mp["by_target"][m]["price"],
                                                                      js["max_price"]["by_target"][m]["price"])]
        if py_changed:
            diffs += [f"{name}.inputs{d}" for d in same({k: v for k, v in py_changed.items() if k != "_int_fields"},
                                                        js["changed"])]
    assert diffs == [], "\n".join(diffs[:30])


def test_branches_are_really_exercised(central):
    """Le jeu de cas couvre bien les branches annoncées (sinon la comparaison prouverait peu)."""
    late = apply_changes(central, [type("C", (), {"field": "letting_void_months", "op": "add", "value": 12})()])
    assert run_model(late)["timeline"]["let_at_exit"] is False
    assert run_model(replace(central, price=central.price * 10))["returns"]["levered_irr"] is None
    assert apply_changes(central, [type("C", (), {"field": "works_months", "op": "add", "value": 2.5})()]).works_months \
        == central.works_months + 2          # arrondi au pair, comme en Python


def test_dashboard_engine_is_the_tested_file():
    """Le dashboard embarque ce fichier-là, et pas une copie divergente."""
    from report.dashboard_v1 import ENGINE_PATH
    assert ENGINE_PATH == ENGINE


def test_mutated_js_engine_is_caught(central, tmp_path):
    """Contrôle du test lui-même : une erreur volontaire dans le moteur JS doit être détectée."""
    bad = tmp_path / "engine_bad.js"
    bad.write_text(ENGINE.read_text(encoding="utf-8").replace("return gross - dedTotal - sale", "return gross - sale")
                   .replace("net: gross - dedTotal - sale", "net: gross - sale"), encoding="utf-8")
    case = [{"name": "central", "inputs": inputs_to_json(central)}]
    p = subprocess.run([NODE, str(RUNNER), str(bad)], input=json.dumps(case), capture_output=True, text=True, encoding="utf-8")
    js = json.loads(p.stdout)[0]["result"]
    assert same(run_model(central), js) != []
