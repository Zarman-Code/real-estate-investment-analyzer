"""Dashboard HTML de la phase 8 (version 1), conservé pendant la reconstruction selon
specification_dashboard_helios.md (report/dashboard.py). Écrit output/dashboard_v1.html.

Génération à partir des SORTIES de l'agent uniquement (réutilisable pour tout autre deal) :
  output/analysis_results.json + output/data_registry.json + asset_config.json + config (objectifs, sensibilités).
Tout est injecté dans un seul objet `window.DATA` ; le HTML ne contient aucun chiffre en dur.
Le moteur JavaScript (templates/engine.js, testé contre Python par tests/test_js_engine.py) est inliné
pour que les curseurs recalculent l'opération en direct.

    python -m report.dashboard_v1            # régénère output/dashboard.html à partir des sorties existantes
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.schema import PROJECT_ROOT, AssetConfig, DefaultsConfig, load_config, load_defaults
from report.labels import LABELS, PARAM_LABELS

TEMPLATES = Path(__file__).parent / "templates"
ENGINE_PATH = Path(__file__).parent / "js" / "engine.js"
TEMPLATE_PATH = TEMPLATES / "dashboard_v1.html"


def _slim_rec(rec: dict) -> dict:
    keep = ("decision", "triggers", "conditions", "questions", "answers", "synthesis", "note_lines", "template_lines",
            "seller_requests", "drafting", "node_drafting")
    out = {k: rec.get(k) for k in keep}
    out["top_risks"] = [r["id"] for r in rec.get("top_risks", [])]
    f = rec.get("facts", {})
    out["fallbacks"] = f.get("fallbacks", [])
    out["targets"] = f.get("targets", {})
    return out


def _slim_sensitivity(se: dict) -> dict:
    if se.get("status") != "complete":
        return {"status": se.get("status"), "missing_inputs": se.get("missing_inputs", [])}
    rows = [{k: r[k] for k in ("variable", "label", "delta_irr_pts", "delta_margin_pts", "irr_range_pts", "rank")}
            for r in se["tornado"]["rows"]]
    return {"status": "complete", "tornado": rows, "breakeven": se["breakeven"], "heatmaps": se["heatmaps"],
            "report": se.get("report")}


def build_data(analysis: dict, registry: dict, config: AssetConfig, defaults: DefaultsConfig) -> dict[str, Any]:
    """Objet unique injecté dans le dashboard. Uniquement des sorties de l'agent et de la configuration."""
    fin = analysis.get("financials", {})
    sens_cfg = defaults.sensitivity
    return {
        "meta": {"asset": analysis.get("asset"), "analysis_date": analysis.get("analysis_date"),
                 "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        "config": {"asset": config.asset.model_dump(mode="json"), "operation": config.operation.model_dump(mode="json"),
                   "overrides": config.overrides},
        "financials": {k: fin.get(k) for k in ("status", "missing_inputs", "inputs", "results", "max_price",
                                               "price_comparison", "market_context", "targets_full", "data_quality",
                                               "report")},
        "model_inputs": fin.get("model_inputs"),
        "price_targets": fin.get("targets"),            # objectifs du prix maximum (TRI FP et marge)
        "risks": {k: analysis.get("risks", {}).get(k) for k in ("risks", "excluded", "missing_information", "report")},
        "sensitivity": _slim_sensitivity(analysis.get("sensitivity", {})),
        "sensitivity_config": {"variables": {k: {"label": v.label} for k, v in sens_cfg.variables.items()}} if sens_cfg else {},
        "scenarios": analysis.get("scenarios", {}),
        "recommendation": _slim_rec(analysis.get("recommendation", {})),
        "committee_questions": {q: {"text": v.text, "node": v.node} for q, v in defaults.committee_questions.items()},
        "registry": registry,
        "labels": {"keys": LABELS, "params": PARAM_LABELS},
        "risk_levels": {k: list(v) for k, v in defaults.risk_scoring.criticality_levels.items()},
    }


def render(data: dict) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")   # pas de fin de <script> dans les données
    engine = ENGINE_PATH.read_text(encoding="utf-8")
    for marker in ("/*__DATA__*/", "/*__ENGINE__*/"):
        if template.count(marker) != 1:
            raise ValueError(f"gabarit invalide : marqueur {marker} absent ou dupliqué")
    title = f"{data['meta']['asset'] or 'Actif'} – dashboard d'investissement"
    return (template.replace("/*__DATA__*/", f"window.DATA = {payload};")
            .replace("/*__ENGINE__*/", engine).replace("{{TITLE}}", _escape(title)))


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_dashboard(out_dir: Path, config: AssetConfig, defaults: DefaultsConfig) -> Path:
    analysis = json.loads((out_dir / "analysis_results.json").read_text(encoding="utf-8"))
    registry = json.loads((out_dir / "data_registry.json").read_text(encoding="utf-8"))
    path = out_dir / "dashboard_v1.html"
    path.write_text(render(build_data(analysis, registry, config, defaults)), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    cfg_path = args[0] if args else str(PROJECT_ROOT / "asset_config.json")
    out = Path(args[1]) if len(args) > 1 else PROJECT_ROOT / "output"
    print(write_dashboard(out, load_config(cfg_path), load_defaults()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
