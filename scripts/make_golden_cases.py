"""Régénère tests/golden_cases.json (specification_dashboard_helios.md, 7.4) : sorties du moteur PYTHON sur des
cas de référence — central, favorable, défavorable (fixture du dashboard) et 3 cas limites. Le moteur JavaScript du
dashboard doit reproduire ces sorties (tests/test_golden_cases.py). À relancer seulement après une modification
VOULUE du moteur : le test signale toute dérive non voulue.

    python scripts/make_golden_cases.py
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.schema import load_defaults  # noqa: E402
from finance.engine import run_model  # noqa: E402
from finance.export import inputs_from_json, inputs_to_json  # noqa: E402
from finance.sensitivity import scenario_inputs  # noqa: E402
from finance.variations import apply_changes  # noqa: E402


class _C:
    def __init__(self, field, op, value):
        self.field, self.op, self.value = field, op, value


def cases() -> list[dict]:
    data = json.loads((ROOT / "fixtures" / "data_sample.json").read_text(encoding="utf-8"))
    central = inputs_from_json(data["sensitivity"]["engine_params"]["inputs"])
    d = load_defaults()
    out = [("central", "scénario central de la fixture", central)]
    for name in ("favorable", "defavorable"):
        out.append((name, f"scénario {name} (defaults.yaml)", scenario_inputs(central, d.scenarios[name], d.sensitivity)))
    out += [
        ("limite_cession_avant_bail", "relocation retardée de 12 mois sans décaler la cession : immeuble vide à la sortie",
         apply_changes(central, [_C("letting_void_months", "add", 12)])),
        ("limite_ltv_sur_prix", "dette calculée sur le seul prix d'acquisition", replace(central, ltv_basis="price")),
        ("limite_sans_tri", "prix décuplé : les flux des fonds propres ne changent pas de signe, TRI non défini",
         replace(central, price=central.price * 10)),
    ]
    return [{"name": n, "description": desc, "inputs": inputs_to_json(i), "expected": run_model(i)} for n, desc, i in out]


if __name__ == "__main__":
    path = ROOT / "tests" / "golden_cases.json"
    path.write_text(json.dumps({"tolerance_rel": 1e-6, "cases": cases()}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(path)
