# Fixtures : vraies réponses des API

Enregistrées par `scripts/probe_sources.py` le 2026-11-08 (machine de l'utilisateur).
Aucune n'a été modifiée : les tests des connecteurs vérifient le parsing sur ces réponses réelles.
Seule `immostat.xlsx` a été renommée (la sonde v1 l'avait enregistrée en `.xml` par erreur).

Sonde n°2 (`scripts/probe_round2.py`, 2026-11-08, 15 h 24) : `ecb_euribor.csv`, `firmio_dataset.json`,
`firmio_csv.csv`, `dvf_mutations_bbox.json` (tous types), `dvf_geomut_codtypbien_14.json` (locaux d'activité).

Analyses de marché (2026-11-08) :
- `cushman_marketbeat_bureaux_idf_t3_2026.pdf` : PDF fourni par l'utilisateur (MarketBeat Bureaux IDF T3 2026,
  Cushman & Wakefield), non modifié (624 844 octets).
- `globalicons_marche_prime_paris_qca_2026.html` : page « Marché prime Paris QCA 2026 » de Global Icons, relevée dans
  le navigateur de l'utilisateur (HTTP 200, Last-Modified 25/09/2026). L'élément `<main>`, seul lu par l'extracteur,
  est recopié à l'identique (empreinte SHA-256 vérifiée par `tests/test_market_reports.py`) ; scripts, styles et pied
  de page retirés.
Copies de secours lues par l'agent si les sites refusent le téléchargement : `data/reports/`.
