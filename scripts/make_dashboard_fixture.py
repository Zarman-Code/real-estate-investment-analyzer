"""Régénère fixtures/data_sample.json : l'agent complet tourne HORS LIGNE sur un dossier fictif
(fixtures/asset_config_demo.json), avec les réponses réelles enregistrées des sources (tests/fixtures), puis
l'adaptateur du dashboard produit l'objet DATA. Résultat : un jeu de données factice mais complet et cohérent avec
le moteur de calcul (le simulateur du dashboard doit retomber exactement sur le central).

    python scripts/make_dashboard_fixture.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from config.schema import load_config, load_defaults  # noqa: E402
from realdata import run_on_fixtures  # noqa: E402
from report.dashboard import build_data  # noqa: E402
from report.dashboard_schema import validate_data  # noqa: E402

GENERATED_AT = "2026-11-08T10:00:00+00:00"        # fixe : la fixture doit être reproductible


def make() -> dict:
    cfg = load_config(ROOT / "fixtures" / "asset_config_demo.json")
    final = run_on_fixtures(Path(tempfile.mkdtemp()), config=cfg)
    analysis = json.loads(json.dumps({k: final[k] for k in ("financials", "risks", "sensitivity", "scenarios", "recommendation")}
                                     | {"analysis_date": final["analysis_date"], "asset": cfg.asset.name}, default=str))
    data = build_data(analysis, final["registry"].to_dict(), cfg, load_defaults(), generated_at=GENERATED_AT)
    for dp in data["datapoints"].values():                     # horodatages de collecte figés (reproductibilité)
        if dp.get("retrieved_at"):
            dp["retrieved_at"] = GENERATED_AT
    validate_data(data)
    return data


if __name__ == "__main__":
    out = ROOT / "fixtures" / "data_sample.json"
    out.write_text(json.dumps(make(), ensure_ascii=False, indent=1), encoding="utf-8")
    print(out)
