"""Importe tous les connecteurs réels : chacun s'enregistre dans tools.connectors.CONNECTORS.

Connecteurs vérifiés sur de vraies réponses (2026-11-08) : IGN, INSEE (ILAT, ICC, BT01), ImmoStat,
PLU Paris (emprises, protections), ADEME DPE tertiaire, BCE (Euribor), DVF Cerema, Firmio,
analyses de marché : page HTML Global Icons (marché prime Paris QCA) et PDF Cushman & Wakefield
(MarketBeat Bureaux IDF T3 2026, cellules lues par position).
En attente : Géorisques (réponses de la sonde n°2 à exploiter).
"""
from tools import ademe_dpe, dvf, ecb_euribor, firmio, ign_geocode, immostat, insee, market_reports, paris_plu  # noqa: F401
