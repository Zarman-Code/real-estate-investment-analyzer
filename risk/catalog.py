"""Catalogue des risques et règles de notation de la PROBABILITÉ (plan, section 6.2).

Chaque règle lit des données sourcées (registre) ou du case (asset_config.json), applique des seuils de
defaults.yaml/risk_scoring.probability, et renvoie sa note AVEC la règle appliquée et les preuves.
Donnée nécessaire absente -> probabilité None (risque « incertain ») et information manquante listée.
L'IMPACT est calculé ailleurs (agent/nodes/risk_analysis.py) en rejouant le moteur financier avec le choc
associé au risque (defaults.yaml/risk_scoring.shocks).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from config.schema import AssetConfig, DefaultsConfig, Shock
from finance.inputs import DealInputs, registry_number
from finance.variations import apply_changes
from risk.matrix import EvidenceRef
from tools.registry import DataRegistry

DPE = "ABCDEFG"


@dataclass
class RuleContext:
    config: AssetConfig
    registry: DataRegistry
    defaults: DefaultsConfig

    @property
    def th(self):
        return self.defaults.risk_scoring.probability


@dataclass
class RuleResult:
    probability: int | None
    rationale: str
    evidence: list[EvidenceRef] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    applicable: bool = True


# ------------------------------------------------------------------ preuves

def from_registry(ctx: RuleContext, key: str, label: str, field_name: str | None = None) -> tuple[Any, EvidenceRef | None]:
    found = ctx.registry.found(key)
    if not found:
        return None, None
    dp = found[0]
    v = dp.value.get(field_name) if field_name and isinstance(dp.value, dict) else dp.value
    if v is None:
        return None, None
    return v, EvidenceRef(key=f"{key}{'.' + field_name if field_name else ''}", label=label, value=v,
                          source=dp.source_name, data_date=dp.data_date)


def from_config(path: str, label: str, value: Any) -> EvidenceRef:
    return EvidenceRef(key=f"asset_config.json:{path}", label=label, value=value, source="Dossier de l'opération (asset_config.json)")


def cap(p: int) -> int:
    return max(1, min(5, p))


# ------------------------------------------------------------------ facteurs structurels tirés du case

def has_extension(ctx) -> bool:
    return ctx.config.asset.surface_after_sqm > ctx.config.asset.surface_before_sqm


def built_before_asbestos_ban(ctx) -> bool | None:
    decade = ctx.config.asset.year_built_decade
    return None if decade is None else decade + 9 < ctx.th.asbestos_ban_year


def dpe_gap(ctx) -> int | None:
    a = ctx.config.asset
    if not (a.dpe_current and a.dpe_target):
        return None
    return DPE.index(a.dpe_current) - DPE.index(a.dpe_target)


# ------------------------------------------------------------------ règles

def rule_letting_delay(ctx) -> RuleResult:
    supply, e1 = from_registry(ctx, "market.immediate_supply", "offre immédiate (m²)", "last")
    take, e2 = from_registry(ctx, "market.take_up", "demande placée sur 12 mois (m²)", "rolling_12m")
    if not supply or not take:
        return RuleResult(None, "", [e for e in (e1, e2) if e],
                          missing=["offre immédiate et demande placée du sous-marché (12 mois)"])
    months = supply / (take / 12)
    bands = ctx.th.months_of_supply_bands
    p = 1 + sum(months >= b for b in bands)
    why = [f"{months:.0f} mois d'offre disponible au rythme de la demande (seuils {list(bands)}) : P {p}"]
    ev = [e1, e2, EvidenceRef(key="computed.months_of_supply", label="mois d'offre", value=round(months, 1))]
    missing = []
    vac, e3 = from_registry(ctx, "market.vacancy", "taux de vacance")
    if vac is None:
        missing.append("taux de vacance du sous-marché (rapports de marché)")
    else:
        ev.append(e3)
        if vac > ctx.th.vacancy_high:
            p += 1
            why.append(f"vacance {vac:.1%} > {ctx.th.vacancy_high:.0%} : +1")
    return RuleResult(cap(p), " ; ".join(why), ev, missing)


def rule_rent_level(ctx) -> RuleResult:
    s_yoy, e1 = from_registry(ctx, "market.immediate_supply", "variation annuelle de l'offre", "yoy")
    r_yoy, e2 = from_registry(ctx, "market.average_rent", "variation annuelle du loyer moyen", "yoy")
    if s_yoy is None and r_yoy is None:
        return RuleResult(None, "", [], missing=["évolution de l'offre et des loyers du sous-marché"])
    p, why = 2, ["base 2 : loyer de relocation estimé, pas encore confronté à des offres réelles"]
    if s_yoy is not None and s_yoy > ctx.th.supply_growth_high:
        p += 1
        why.append(f"offre en hausse de {s_yoy:.0%} sur un an (> {ctx.th.supply_growth_high:.0%}) : +1")
    if r_yoy is not None and r_yoy < 0:
        p += 1
        why.append(f"loyer moyen en baisse de {r_yoy:.1%} sur un an : +1")
    missing = [] if (s_yoy is not None and r_yoy is not None) else ["série complète offre / loyers"]
    return RuleResult(cap(p), " ; ".join(why), [e for e in (e1, e2) if e], missing)


def rule_capex_overrun(ctx) -> RuleResult:
    a, op = ctx.config.asset, ctx.config.operation
    ev = [from_config("operation.capex_eur_per_sqm_after", "budget travaux (€/m²)", op.capex_eur_per_sqm_after)]
    p, why = 1, ["base 1"]
    if has_extension(ctx):
        p += 1; why.append("extension de surface : +1")
        ev.append(from_config("asset.surface_after_sqm", "surface après travaux", a.surface_after_sqm))
    gap = dpe_gap(ctx)
    if gap is not None and gap >= ctx.th.dpe_gap_heavy:
        p += 1; why.append(f"saut de {gap} classes DPE ({a.dpe_current} -> {a.dpe_target}) : +1")
        ev.append(from_config("asset.dpe_current", "DPE actuel / cible", f"{a.dpe_current} -> {a.dpe_target}"))
    pre = built_before_asbestos_ban(ctx)
    if pre:
        p += 1; why.append(f"construit dans les années {a.year_built_decade}, avant l'interdiction de l'amiante : +1")
        ev.append(from_config("asset.year_built_decade", "décennie de construction", a.year_built_decade))
    bt, e = from_registry(ctx, "index.bt01", "inflation annuelle BT01", "yoy")
    missing = ["benchmark du coût de restructuration lourde au m² (économiste de la construction, devis)"]
    if bt is None:
        missing.append("indice BT01 (inflation des coûts de travaux)")
    else:
        ev.append(e)
        if bt > ctx.th.bt01_inflation_high:
            p += 1; why.append(f"coûts de travaux +{bt:.1%} sur un an (> {ctx.th.bt01_inflation_high:.0%}) : +1")
    return RuleResult(cap(p), " ; ".join(why), ev, missing)


def rule_works_delay(ctx) -> RuleResult:
    a, t = ctx.config.asset, ctx.config.operation.timeline
    ev = [from_config("operation.timeline.works_duration_months", "durée des travaux (mois)", t.works_duration_months)]
    p, why = 1, ["base 1"]
    if has_extension(ctx):
        p += 1; why.append("extension : permis de construire et délai de recours des tiers : +1")
    if built_before_asbestos_ban(ctx):
        p += 1; why.append("désamiantage probable (construction antérieure à 1997) : +1")
    prot, e = from_registry(ctx, "urbanism.protected_buildings", "bâtiments protégés dans l'arrondissement", "count")
    missing = []
    if prot is None:
        missing.append("protections patrimoniales du secteur (PLU)")
    else:
        ev.append(e)
        if prot >= ctx.th.protected_buildings_many:
            p += 1; why.append(f"{prot} bâtiments protégés dans l'arrondissement (secteur patrimonial, avis ABF possible) : +1")
    leases = ctx.config.operation.current_leases
    if leases:
        margin_days = (t.works_start - max(l.end_date for l in leases)).days
        ev.append(from_config("operation.current_leases.end_date", "fin du bail en place", str(max(l.end_date for l in leases))))
        if margin_days <= 31:
            p += 1; why.append(f"bail en place libéré {margin_days} jour(s) avant le chantier : aucune marge : +1")
    return RuleResult(cap(p), " ; ".join(why), ev, missing)


def rule_rates_up(ctx) -> RuleResult:
    last, e1 = from_registry(ctx, "rates.euribor_3m", "Euribor 3 mois (dernier)", "last")
    avg, e2 = from_registry(ctx, "rates.euribor_3m", "Euribor 3 mois (moyenne 12 mois)", "avg_12m")
    if last is None or avg is None:
        return RuleResult(None, "", [], missing=["Euribor 3 mois (BCE)"])
    ev = [e1, e2, from_config("operation.financing.reference_rate", "taux de référence", ctx.config.operation.financing.reference_rate)]
    p, why = 3, ["base 3 : dette à taux variable, aucune couverture prévue au case"]
    if last >= avg:
        p += 1; why.append(f"Euribor {last:.2%} au-dessus de sa moyenne 12 mois {avg:.2%} : tendance haussière : +1")
    return RuleResult(cap(p), " ; ".join(why), ev, [])


def rule_exit_yield_up(ctx) -> RuleResult:
    yoy, e = from_registry(ctx, "market.investment_price_per_sqm", "variation annuelle du prix d'investissement", "yoy")
    if yoy is None:
        return RuleResult(None, "", [], missing=["évolution des prix d'investissement bureaux du sous-marché"])
    if yoy <= ctx.th.investment_price_drop:
        p, why = 4, f"prix d'investissement {yoy:+.1%} sur un an (<= {ctx.th.investment_price_drop:.0%}) : P 4"
    elif yoy < 0:
        p, why = 3, f"prix d'investissement {yoy:+.1%} sur un an : P 3"
    else:
        p, why = 2, f"prix d'investissement {yoy:+.1%} sur un an : P 2"
    return RuleResult(p, why, [e], ["taux de rendement prime et son évolution (rapports de marché)"])


def rule_planning(ctx) -> RuleResult:
    if not has_extension(ctx):
        return RuleResult(None, "pas d'extension prévue", applicable=False)
    plu, e1 = from_registry(ctx, "urbanism.plu_constraints", "hauteur maximale indiquée au PLU (arrondissement)", "max_height")
    prot, e2 = from_registry(ctx, "urbanism.protected_buildings", "bâtiments protégés dans l'arrondissement", "count")
    if plu is None and prot is None:
        return RuleResult(None, "", [], missing=["règles PLU et protections du secteur"])
    ev = [from_config("asset.surface_after_sqm", "surface après travaux (extension)", ctx.config.asset.surface_after_sqm)]
    ev += [e for e in (e1, e2) if e]
    p, why = 3, ["base 3 : extension et rooftop soumis à autorisation d'urbanisme"]
    if prot is not None and prot >= ctx.th.protected_buildings_many:
        p += 1; why.append(f"{prot} bâtiments protégés dans l'arrondissement : +1")
    missing = ["règle PLU applicable à LA parcelle et étude de faisabilité (architecte)"]
    return RuleResult(cap(p), " ; ".join(why), ev, missing)


def rule_dpe(ctx) -> RuleResult:
    a = ctx.config.asset
    gap = dpe_gap(ctx)
    if gap is None:
        return RuleResult(None, "", [], missing=["DPE actuel et DPE cible"])
    ev = [from_config("asset.dpe_current", "DPE actuel / cible", f"{a.dpe_current} -> {a.dpe_target}")]
    p = 3 if gap >= ctx.th.dpe_gap_heavy else 2
    why = [f"{gap} classes à gagner ({a.dpe_current} -> {a.dpe_target}) : P {p}"]
    share, e = from_registry(ctx, "energy.dpe_comparables", "part des DPE A-B à proximité", "share_b_or_better")
    missing = ["audit énergétique de l'immeuble"]
    if share is None:
        missing.append("DPE tertiaires du voisinage (ADEME)")
    else:
        ev.append(e)
        if share < ctx.th.dpe_low_share_b_or_better:
            p += 1; why.append(f"seulement {share:.0%} des DPE voisins en A-B : objectif rare dans le secteur : +1")
    return RuleResult(cap(p), " ; ".join(why), ev, missing)


def rule_natural(ctx) -> RuleResult:
    found = [k for k in ("risk.natural_risks", "risk.catnat") if ctx.registry.found(k)]
    missing = ["études de sols et diagnostic de pollution (vendeur)"]
    if not found:
        missing.insert(0, "risques naturels et arrêtés CatNat (Géorisques : connecteur à écrire)")
    else:
        missing.insert(0, "règle de notation Géorisques à définir sur des réponses vérifiées")
    return RuleResult(None, "", [], missing)


# ------------------------------------------------------------------ catalogue

@dataclass(frozen=True)
class RiskSpec:
    id: str
    category: str
    title: str
    description: str
    rule: Callable[[RuleContext], RuleResult]
    shock: str | None
    mitigations: tuple[str, ...]
    seller_requests: tuple[str, ...] = ()
    financial_link: tuple[str, ...] = ()


CATALOG: tuple[RiskSpec, ...] = (
    RiskSpec("R1", "marché_locatif", "Relocation retardée",
             "Les surfaces restructurées ne trouvent pas preneur dans le délai prévu après la livraison.",
             rule_letting_delay, "letting_delay",
             ("pré-commercialisation dès le lancement des travaux", "promesse de bail avant l'acquisition définitive",
              "étude d'absorption du sous-marché par un conseil"),
             ("état locatif et calendrier de départ du locataire en place",), ("NOI", "TRI", "marge")),
    RiskSpec("R2", "marché_locatif", "Loyer de relocation inférieur à l'hypothèse",
             "Le loyer obtenu à la relocation est inférieur au loyer retenu dans le scénario central.",
             rule_rent_level, "rent_level",
             ("références locatives récentes d'immeubles restructurés comparables", "pré-commercialisation",
              "fixer le prix d'acquisition sur un loyer prudent"),
             (), ("NOI", "YoC", "valeur de sortie", "TRI", "marge")),
    RiskSpec("R3", "travaux", "Dépassement du budget travaux",
             "Le coût de la restructuration dépasse le budget prévu et sa provision pour aléas.",
             rule_capex_overrun, "capex_overrun",
             ("contrat de travaux à prix global et forfaitaire", "audit technique et repérage amiante avant signature",
              "provision pour aléas maintenue jusqu'à la réception", "économiste de la construction"),
             ("audit technique complet", "diagnostic amiante avant travaux", "plans de l'immeuble"),
             ("coût total", "YoC", "TRI", "marge")),
    RiskSpec("R4", "travaux", "Retard du chantier",
             "Le chantier dépasse la durée prévue (désamiantage, autorisations, aléas).",
             rule_works_delay, "works_delay",
             ("planning avec pénalités de retard", "désamiantage traité en lot préalable",
              "condition suspensive de libération des locaux"),
             ("calendrier de libération du locataire en place", "historique des autorisations d'urbanisme"),
             ("portage", "frais financiers", "TRI")),
    RiskSpec("R5", "financier", "Hausse des taux d'intérêt",
             "La hausse de l'Euribor renchérit la dette à taux variable pendant l'opération.",
             rule_rates_up, "rates_up",
             ("couverture de taux (cap ou swap) dès le tirage", "négociation de la marge et du plancher"),
             (), ("frais financiers", "TRI fonds propres")),
    RiskSpec("R6", "financier", "Baisse de la valeur de sortie",
             "Le taux de rendement attendu par les acquéreurs à la sortie est plus élevé que prévu.",
             rule_exit_yield_up, "exit_yield_up",
             ("prix d'acquisition fixé sur un taux de sortie prudent", "flexibilité sur la date de cession"),
             (), ("valeur de sortie", "marge", "TRI")),
    RiskSpec("R7", "réglementaire", "Extension ou rooftop non autorisés",
             "Les surfaces supplémentaires et le rooftop ne sont pas autorisés par la Ville ou l'ABF.",
             rule_planning, "no_extension",
             ("consultation préalable du service urbanisme et de l'ABF", "condition suspensive d'obtention du permis",
              "étude de faisabilité par un architecte"),
             ("autorisations d'urbanisme existantes", "plans et relevés de géomètre"),
             ("surface louable", "NOI", "valeur de sortie", "marge")),
    RiskSpec("R8", "environnemental", "Objectif DPE non atteint",
             "La restructuration n'atteint pas la classe énergétique visée.",
             rule_dpe, "dpe_target_missed",
             ("audit énergétique et simulation thermique avant acquisition", "engagement de performance dans le marché de travaux"),
             ("DPE actuel et factures d'énergie", "audit énergétique s'il existe"),
             ("loyer", "travaux", "liquidité à la sortie")),
    RiskSpec("R9", "environnemental", "Risques naturels et pollution",
             "Exposition aux risques naturels ou technologiques, ou pollution des sols.",
             rule_natural, None,
             ("consultation de l'état des risques (ERP)", "études de sols si travaux en sous-sol"),
             ("état des risques et pollutions (ERP)", "diagnostics environnementaux"), ("travaux", "assurance")),
)


# ------------------------------------------------------------------ chocs

def apply_shock(inp: DealInputs, shock: Shock) -> DealInputs:
    """Applique le choc d'un risque (implémentation partagée avec les sensibilités : finance/variations.py)."""
    return apply_changes(inp, shock.changes)


def rule_context(config: AssetConfig, registry: DataRegistry, defaults: DefaultsConfig) -> RuleContext:
    return RuleContext(config, registry, defaults)


__all__ = ["CATALOG", "RiskSpec", "RuleContext", "RuleResult", "apply_shock", "rule_context", "registry_number"]
