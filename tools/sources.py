"""Importe tous les connecteurs réels : chacun s'enregistre dans tools.connectors.CONNECTORS.

Connecteurs vérifiés sur de vraies réponses (2026-11-08) : IGN, INSEE (ILAT, ICC, BT01), ImmoStat,
PLU Paris (emprises, protections), ADEME DPE tertiaire, BCE (Euribor), DVF Cerema, Firmio.
En attente : Géorisques (réponses de la sonde n°2 à exploiter).
En attente d'une phase dédiée : rapports de marché (recherche web + extraction PDF par LLM).
"""
from tools import ademe_dpe, dvf, ecb_euribor, firmio, ign_geocode, immostat, insee, paris_plu  # noqa: F401
