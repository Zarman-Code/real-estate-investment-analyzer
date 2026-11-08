"""Connecteur Firmio (data.gouv.fr) - prix médian des locaux d'entreprises par commune, d'après DVF.

Vérifié sur de vraies réponses (tests/fixtures/firmio_dataset.json et firmio_csv.csv, millésime 2026-10) :
- fiche data.gouv.fr (API v1) : resources[] avec format « csv », url, last_modified ;
- CSV séparé par des virgules, en-tête exact
  code_commune, commune, code_postal, nb_societes, prix_m2_median, nb_transactions ;
- Paris est découpé par arrondissement (75109), MAIS chaque arrondissement apparaît sur PLUSIEURS lignes
  (libellés « PARIS », « PARIS 09 », « PARIS 9 », code postal vide ou non) avec des médianes différentes ;
- le compteur nb_transactions est incohérent avec les volumes DVF connus (25 036 pour le 9e) ;
- la période de transactions couverte n'est pas indiquée.
Conséquence : valeur retenue = ligne au plus grand nb_transactions, confiance FAIBLE, toutes les variantes
et anomalies écrites dans le DataPoint. Recoupement du DVF officiel, jamais source prioritaire.
"""
from __future__ import annotations

import csv
import io

from tools.base import ParseError
from tools.common import as_float, make_point, require_client, require_location
from tools.connectors import connector

HEADER = ["code_commune", "commune", "code_postal", "nb_societes", "prix_m2_median", "nb_transactions"]


def latest_csv(meta: dict) -> dict:
    csvs = [r for r in meta.get("resources", []) if str(r.get("format", "")).lower() == "csv" and r.get("url")]
    if not csvs:
        raise ParseError("aucune ressource CSV dans la fiche data.gouv.fr")
    return max(csvs, key=lambda r: r.get("last_modified") or "")


def parse_rows(content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames != HEADER:
        raise ParseError(f"en-tête inattendu : {reader.fieldnames} (attendu {HEADER})")
    return list(reader)


@connector("firmio_dvf_entreprises", needs_location=True)
def fetch_firmio(ctx, needs):
    source = ctx.catalog.by_id("firmio_dvf_entreprises")
    client = require_client(ctx, source)
    citycode = str(require_location(ctx, source).get("citycode", ""))
    meta = client.get(source.id, source.url).json()
    res = latest_csv(meta)
    r = client.get(source.id, res["url"])
    rows = [row for row in parse_rows(r.content) if row["code_commune"] == citycode]
    if not rows:
        raise ParseError(f"commune {citycode} absente du fichier (moins de 5 entreprises, ou code inconnu)")
    variants = sorted(({"label": row["commune"], "postcode": row["code_postal"] or None,
                        "median": as_float(row["prix_m2_median"], "prix_m2_median"),
                        "n_transactions": int(row["nb_transactions"]), "n_companies": int(row["nb_societes"])}
                       for row in rows), key=lambda v: -v["n_transactions"])
    main = variants[0]
    medians = [v["median"] for v in variants]
    vintage = (res.get("last_modified") or "")[:10] or None
    value = {"median": main["median"], "n": main["n_transactions"], "n_companies": main["n_companies"],
             "n_variants": len(variants), "variants_range": [min(medians), max(medians)], "variants": variants}
    notes = (f"Firmio, millésime {vintage}, commune {citycode} : {len(variants)} lignes pour le même code "
             f"(médianes de {min(medians):,.0f} à {max(medians):,.0f} €/m²), ligne retenue « {main['label']} » "
             f"({main['n_transactions']} transactions déclarées, compteur incohérent avec les volumes DVF) ; "
             "MÉLANGE bureaux, commerces et entrepôts ; ventes rattachées à un SIREN uniquement ; période non "
             "précisée ; niveau relatif, pas une valeur d'expertise").replace(",", " ")
    return [make_point(source, n.key, value, "€/m²", vintage, r, notes, confidence="low") for n in needs]
