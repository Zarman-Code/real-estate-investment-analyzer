"""Test de non-régression obligatoire (specification_dashboard_helios.md, 7.4) : les sorties du moteur JavaScript du
dashboard doivent égaler celles du moteur Python stockées dans tests/golden_cases.json (tolérance relative 1e-6).
Le moteur Python est lui aussi comparé aux cas de référence : toute dérive de l'un ou de l'autre est signalée."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from config.schema import PROJECT_ROOT
from finance.engine import run_model
from finance.export import inputs_from_json
from test_js_engine import same

GOLDEN = json.loads((Path(__file__).parent / "golden_cases.json").read_text(encoding="utf-8"))
TOL = GOLDEN["tolerance_rel"]
NODE = shutil.which("node")
ENGINE = PROJECT_ROOT / "report" / "js" / "engine.js"
RUNNER = Path(__file__).parent / "js_runner.js"


def close(expected, got, path="") -> list[str]:
    """Comparaison à tolérance relative TOL (les dates sont des chaînes ISO des deux côtés)."""
    if isinstance(expected, (int, float)) and not isinstance(expected, bool) and isinstance(got, (int, float)):
        return [] if abs(expected - got) <= TOL * max(1.0, abs(expected)) else [f"{path}: {expected} != {got}"]
    if isinstance(expected, dict) and isinstance(got, dict):
        if set(expected) != set(got):
            return [f"{path}: clés {sorted(set(expected) ^ set(got))}"]
        return [d for k in expected for d in close(expected[k], got[k], f"{path}.{k}")]
    if isinstance(expected, list) and isinstance(got, list):
        if len(expected) != len(got):
            return [f"{path}: longueur"]
        return [d for i, (a, b) in enumerate(zip(expected, got)) for d in close(a, b, f"{path}[{i}]")]
    return [] if expected == got else [f"{path}: {expected!r} != {got!r}"]


def test_golden_cases_cover_the_required_set():
    names = {c["name"] for c in GOLDEN["cases"]}
    assert {"central", "favorable", "defavorable"} <= names and len(names) >= 6
    assert any(c["expected"]["returns"]["levered_irr"] is None for c in GOLDEN["cases"])          # cas sans TRI
    assert any(c["expected"]["timeline"]["let_at_exit"] is False for c in GOLDEN["cases"])        # cession avant le bail


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda c: c["name"])
def test_python_engine_still_matches_golden(case):
    got = json.loads(json.dumps(run_model(inputs_from_json(case["inputs"])), default=str))
    assert close(case["expected"], got) == []


@pytest.mark.skipif(NODE is None, reason="Node.js absent : moteur JS non testé ici")
def test_js_engine_matches_golden():
    p = subprocess.run([NODE, str(RUNNER), str(ENGINE)], input=json.dumps([{"inputs": c["inputs"]} for c in GOLDEN["cases"]]),
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert p.returncode == 0, p.stderr
    results = json.loads(p.stdout)
    diffs = [f"{c['name']}{d}" for c, r in zip(GOLDEN["cases"], results) for d in close(c["expected"], r["result"])]
    assert diffs == [], "\n".join(diffs[:20])
