"""Nœud validate_research : contrôles automatiques des données collectées et décision de routage.

Contrôles (section 4.3 du plan), tous codés, sans LLM :
  completeness  besoin P1 sans valeur                      -> bloquant (lacune)
  plausibility  valeur hors de la fourchette de garde-fou   -> bloquant si P1, sinon avertissement
  freshness     donnée plus ancienne que max_age_months    -> avertissement
  sample_size   échantillon < min_sample                   -> avertissement
  convergence   une seule source quand 2 sont demandées, ou écart entre sources > seuil -> avertissement
  coherence     règles entre indicateurs (loyer prime >= loyer moyen) -> avertissement

Routage :
  aucune lacune P1                                          -> sufficient
  lacunes P1, retours < max_research_loops, ET au moins une
  lacune a encore une source non essayée                    -> research_again (recherche limitée aux lacunes)
  sinon                                                     -> proceed_with_assumptions : les lacunes qui ont
      une hypothèse de repli dans defaults.yaml deviennent status=assumption ; les autres restent missing.
"""
from __future__ import annotations

import calendar
import re
from datetime import date

from agent.state import AgentState, ValidationIssue, ValidationReport
from config.schema import NeedSpec
from tools.connectors import ConnectorRegistry
from tools.registry import DataPoint, DataRegistry

FALLBACK_SOURCE = "Hypothèse de l'agent (config/defaults.yaml)"


def parse_data_date(s: str | None) -> date | None:
    """Fin de la période désignée : '2026' -> 31/12/2026, '2026-09' -> 30/09/2026,
    '2026-Q3' / '2026-T3' -> 30/09/2026, '2026-09-15' -> 15/09/2026. Sinon None."""
    if not s:
        return None
    s = s.strip()
    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s[:10]):
        return date(int(m[1]), int(m[2]), int(m[3]))
    if m := re.fullmatch(r"(\d{4})-?[QT]([1-4])", s, re.IGNORECASE):
        y, month = int(m[1]), int(m[2]) * 3
        return date(y, month, calendar.monthrange(y, month)[1])
    if m := re.fullmatch(r"(\d{4})-(\d{2})", s):
        y, month = int(m[1]), int(m[2])
        return date(y, month, calendar.monthrange(y, month)[1])
    if m := re.fullmatch(r"(\d{4})", s):
        return date(int(m[1]), 12, 31)
    return None


def months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def numeric(dp: DataPoint, spec: NeedSpec) -> float | None:
    v = dp.value
    if spec.value_field and isinstance(v, dict):
        v = v.get(spec.value_field)
    if isinstance(v, bool):
        return None
    return float(v) if isinstance(v, (int, float)) else None


def implausible(spec: NeedSpec, dp: DataPoint) -> bool:
    if not spec.plausible_range:
        return False
    v = numeric(dp, spec)
    return v is not None and not spec.plausible_range[0] <= v <= spec.plausible_range[1]


def check_need(spec: NeedSpec, registry: DataRegistry, analysis: date, divergence_pct: float
               ) -> tuple[list[ValidationIssue], bool]:
    """Renvoie (problèmes, est_une_lacune_P1)."""
    issues: list[ValidationIssue] = []
    is_p1 = spec.priority == "P1"
    found = registry.found(spec.key)
    if not found:
        why = (registry.get(spec.key).notes if registry.get(spec.key) else None) or "non collectée"
        issues.append(ValidationIssue(key=spec.key, check="completeness", severity="blocking" if is_p1 else "warning",
                                      message=f"donnée {spec.priority} manquante : {why}"))
        return issues, is_p1

    primary, gap = found[0], False
    if spec.plausible_range:
        lo, hi = spec.plausible_range

        def plausible(p: DataPoint) -> bool:
            v = numeric(p, spec)
            return v is None or lo <= v <= hi

        if not plausible(primary):
            alternatives = [p for p in found[1:] if plausible(p)]
            if alternatives:   # une autre source est plausible : elle devient la valeur retenue
                registry.promote(alternatives[0])
                issues.append(ValidationIssue(key=spec.key, check="plausibility", severity="warning",
                                              message=f"valeur {numeric(primary, spec)} de {primary.source_name} hors de "
                                                      f"[{lo} ; {hi}], écartée au profit de {alternatives[0].source_name}"))
                primary = alternatives[0]
                found = registry.found(spec.key)
            else:
                gap = is_p1
                issues.append(ValidationIssue(key=spec.key, check="plausibility",
                                              severity="blocking" if is_p1 else "warning",
                                              message=f"valeur {numeric(primary, spec)} hors de la plage plausible "
                                                      f"[{lo} ; {hi}] ({primary.source_name}) : erreur d'unité ou "
                                                      "d'extraction ?"))
    if spec.max_age_months and primary.method not in ("user_input", "assumption"):
        d = parse_data_date(primary.data_date)
        if d is None:
            issues.append(ValidationIssue(key=spec.key, check="freshness", severity="warning",
                                          message=f"date de la donnée inconnue ou illisible : {primary.data_date!r}"))
        elif (age := months_between(d, analysis)) > spec.max_age_months:
            issues.append(ValidationIssue(key=spec.key, check="freshness", severity="warning",
                                          message=f"donnée de {age} mois (> {spec.max_age_months}) : {primary.data_date}"))
    if spec.min_sample:
        n = primary.value.get(spec.sample_size_field) if isinstance(primary.value, dict) else None
        if not isinstance(n, (int, float)) or n < spec.min_sample:
            issues.append(ValidationIssue(key=spec.key, check="sample_size", severity="warning",
                                          message=f"échantillon de {n} (< {spec.min_sample}) : confiance faible"))
    if len(found) < spec.min_sources:
        issues.append(ValidationIssue(key=spec.key, check="convergence", severity="warning",
                                      message=f"{len(found)} source(s) pour {spec.min_sources} demandées : "
                                              "pas de vérification croisée possible"))
    values = [v for v in (numeric(p, spec) for p in found) if v is not None]
    if len(values) >= 2:
        mean = sum(values) / len(values)
        spread = (max(values) - min(values)) / abs(mean) if mean else float("inf")
        srcs = ", ".join(f"{p.source_name} = {numeric(p, spec)}" for p in found)
        if spread > divergence_pct:
            issues.append(ValidationIssue(key=spec.key, check="convergence", severity="warning",
                                          message=f"écart de {spread:.0%} entre sources (seuil {divergence_pct:.0%}) : {srcs}"))
        else:   # la vérification croisée a eu lieu : on garde la trace, même quand elle est concluante
            issues.append(ValidationIssue(key=spec.key, check="convergence", severity="info",
                                          message=f"recoupement concordant : écart de {spread:.1%} (seuil {divergence_pct:.0%}) : {srcs}"))
    return issues, gap


def fallback_point(spec: NeedSpec) -> DataPoint:
    fb = spec.fallback
    return DataPoint(key=spec.key, value=fb.value, unit=fb.unit, source_name=FALLBACK_SOURCE,
                     method="assumption", status="assumption", confidence=fb.confidence,
                     notes=f"{fb.rationale.strip()} Fourchette retenue : [{fb.range[0]} ; {fb.range[1]}]. "
                           "Recherche infructueuse : hypothèse appliquée par validate_research.")


def make_validate_node(connectors: ConnectorRegistry):
    def validate_research(state: AgentState) -> dict:
        defaults, registry = state["defaults"], state["registry"]
        analysis = date.fromisoformat(state["analysis_date"])
        divergence = defaults.quality_checks.source_divergence_alert_pct
        issues: list[ValidationIssue] = []
        gaps: list[str] = []
        for need in state["data_needs"]:
            spec = defaults.need(need.key).model_copy(update={"priority": need.priority})
            need_issues, is_gap = check_need(spec, registry, analysis, divergence)
            issues += need_issues
            if is_gap:
                gaps.append(need.key)

        for rule in defaults.coherence_rules:
            a, b = registry.found(rule.greater_or_equal), registry.found(rule.than)
            if a and b:
                va, vb = numeric(a[0], defaults.need(rule.greater_or_equal)), numeric(b[0], defaults.need(rule.than))
                if va is not None and vb is not None and va < vb:
                    issues.append(ValidationIssue(key=rule.greater_or_equal, check="coherence", severity="warning",
                                                  message=f"{rule.message} ({va} < {vb})"))

        # valeurs aberrantes encore retenues (aucune source alternative plausible)
        suspects = [k for k in (n.key for n in state["data_needs"])
                    if registry.found(k) and implausible(defaults.need(k), registry.found(k)[0])]

        loops_done = state["research_iteration"] - 1
        max_loops = defaults.research.max_research_loops
        attempts = state["research_attempts"]
        needs_by_key = {n.key: n for n in state["data_needs"]}
        retryable = [k for k in gaps if any(s in connectors and s not in attempts.get(k, [])
                                            for s in needs_by_key[k].target_sources)]
        report = ValidationReport(iteration=state["research_iteration"], issues=issues, gaps=gaps)
        n_info = sum(i.severity == "info" for i in issues)
        logs = [f"validate_research (après passe {state['research_iteration']}) : {len(gaps)} lacune(s) P1, "
                f"{len(report.blocking())} bloquant(s), {len(issues) - len(report.blocking()) - n_info} avertissement(s), "
                f"{n_info} information(s)"]

        def reject_suspects(keys):
            for k in keys:
                dp = registry.found(k)[0]
                lo, hi = defaults.need(k).plausible_range
                registry.reject(dp, f"valeur {numeric(dp, defaults.need(k))} hors de la plage plausible [{lo} ; {hi}] "
                                    "et aucune autre source plausible : jamais utilisée dans les calculs")
                logs.append(f"  {k} : valeur aberrante de {dp.source_name} rejetée")

        if not gaps:
            report.verdict = "sufficient"
            reject_suspects(suspects)                      # P2/P3 aberrantes : pas de nouvelle recherche
        elif loops_done < max_loops and retryable:
            report.verdict = "research_again"
            reject_suspects([k for k in suspects if k not in retryable])   # celles qui ne seront pas re-cherchées
            logs.append(f"  -> nouvelle recherche limitée aux lacunes {retryable} "
                        f"(retour {loops_done + 1}/{max_loops})")
        else:
            report.verdict = "proceed_with_assumptions"
            why = "nombre maximal de retours atteint" if retryable else "aucune autre source disponible"
            logs.append(f"  -> analyse avec lacunes ({why})")
            reject_suspects(suspects)
            for k in gaps:
                spec = defaults.need(k)
                if spec.fallback and not registry.found(k):
                    registry.merge(fallback_point(spec))
                    report.assumptions_applied.append(k)
                    logs.append(f"  {k} : hypothèse de repli appliquée ({spec.fallback.value} {spec.fallback.unit})")
                elif not registry.found(k):
                    logs.append(f"  {k} : reste manquant (aucune hypothèse admissible)")
        for i in issues:
            logs.append(f"  [{i.severity}] {i.check} {i.key} : {i.message}")
        return {"validation_report": report, "registry": registry, "logs": logs}

    return validate_research


def route_after_validation(state: AgentState) -> str | list[str]:
    report = state["validation_report"]
    if report is not None and report.verdict == "research_again":
        return "research_market_data"
    return ["financial_analysis", "risk_analysis"]
