"""Libellés lisibles des clés du registre (affichage seulement, pour la note et le dashboard)."""
LABELS = {
    "location.reference_point": "point de référence géocodé",
    "market.dvf_price_per_sqm": "prix au m² des transactions (DVF)",
    "market.prime_rent": "loyer prime du sous-marché",
    "market.average_rent": "loyer moyen du sous-marché",
    "market.prime_yield": "taux de rendement prime",
    "market.vacancy": "taux de vacance",
    "rates.euribor_3m": "Euribor 3 mois",
    "deal.acquisition_fees_pct": "frais d'acquisition",
    "market.incentives": "mesures d'accompagnement",
    "market.future_supply": "offre future",
    "market.take_up": "demande placée",
    "market.immediate_supply": "offre immédiate",
    "market.investment_price_per_sqm": "prix d'investissement bureaux au m²",
    "index.ilat": "indice ILAT",
    "index.icc": "indice ICC",
    "index.bt01": "index BT01",
    "energy.dpe_comparables": "DPE tertiaires voisins",
    "urbanism.plu_constraints": "règles du PLU",
    "urbanism.protected_buildings": "protections patrimoniales",
    "risk.natural_risks": "risques naturels",
    "risk.catnat": "arrêtés de catastrophe naturelle",
}


def label(key: str) -> str:
    return LABELS.get(key, key)


def labels(keys) -> str:
    return ", ".join(label(k) for k in keys)


PARAM_LABELS = {
    "relet_rent": "loyer de relocation", "exit_yield": "taux de sortie", "euribor": "Euribor",
    "acq_fees_pct": "frais d'acquisition", "stabilised_occupancy": "occupation stabilisée",
    "rent_free_months": "franchise", "letting_void_months": "délai de relocation",
    "non_recoverable_pct": "charges non récupérables", "sale_costs_pct": "frais de cession",
    # autres paramètres du modèle (affichage du dashboard)
    "price": "prix d'acquisition", "surface_before": "surface actuelle", "surface_after": "surface après travaux",
    "capex_per_sqm": "coût des travaux au m²", "fees_contingency_pct": "honoraires et aléas (% des travaux)",
    "ltv": "LTV", "margin_bps": "marge bancaire", "current_leases": "baux en place", "exit_quarter": "trimestre de cession",
    "exit_date": "date de cession", "firm_lease_term_months": "durée ferme du nouveau bail",
    "incentives_if_missing": "mesures d'accompagnement (hypothèse)", "incentives": "mesures d'accompagnement",
    "vacant_holding_cost_eur_sqm_year": "coût de portage des surfaces vides",
    "marketing_fees_pct_annual_rent": "honoraires de commercialisation", "debt_arrangement_fee_pct": "commission d'arrangement",
    "euribor_floor": "plancher d'Euribor", "ltv_basis": "base de la LTV", "interest_mode": "traitement des intérêts",
}


def param_labels(names) -> str:
    return ", ".join(PARAM_LABELS.get(n, n) for n in names)
