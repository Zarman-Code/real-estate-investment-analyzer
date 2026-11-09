"""Connecteurs des rapports et analyses de marché (sources de type « report » du catalogue).

Un même moteur sert deux formats :
  - page HTML (« web scraping ») : source `market_reports` ;
  - rapport PDF : source `cushman_marketbeat`.

Le document est téléchargé à l'URL du catalogue (cache disque du client HTTP). Si le site refuse le
téléchargement, la copie locale `params.local_file` (chemin relatif au projet) est lue. Rien n'est inventé :
un indicateur que le document ne publie pas est renvoyé « missing » avec la raison.

Chaque éditeur a sa mise en page : un extracteur par mise en page, écrit après inspection du vrai document
(règle 5), reconnaît le document par sa signature puis lit les valeurs à leur place.

1. Global Icons, « Marché prime Paris QCA 2026 : état des lieux » (page HTML relevée le 2026-11-08, dernière
   modification annoncée par le serveur : 25/09/2026 ; extrait fidèle dans
   tests/fixtures/globalicons_marche_prime_paris_qca_2026.html) :
   - tableau `table.compare-table`, colonnes « Année | Volume QCA (M€) | Rendement prime bureau | Loyer prime »,
     une ligne par année 2020-2025 puis « 2026 (estim.) » en fourchettes (« 3,75 - 4,00 % ») ;
     volumes précédés de « ~ » (approximations) ; note « Fourchettes indicatives », consolidation par l'éditeur de
     BNP Paribas RE, CBRE, JLL et C&W ;
   - encadré « Synthèse » : « rendement prime bureau 3,50-4,25 % » ;
   - FAQ : « Vacance globale ~5-6 %, mais vacance prime grade A < 3 % », sans date ;
   - absents : loyer moyen, mesures d'accompagnement, offre future, demande placée, offre immédiate.
   Valeurs retenues : dernière année OBSERVÉE du tableau (pas l'estimation) ; la vacance, publiée en fourchette,
   est prise au milieu de la fourchette, statut « partial », confiance faible.

2. Cushman & Wakefield, MarketBeat Bureaux Île-de-France T3 2026 (PDF de 5 pages, fichier fourni le 2026-11-08,
   tests/fixtures/cushman_marketbeat_bureaux_idf_t3_2026.pdf) :
   - page 1 : « 3e TRIMESTRE 2026 » ;
   - page 5 : tableau « DONNÉES DE MARCHÉ », une ligne par sous-marché (« Paris QCA »...) et 11 colonnes :
     demande placée T1-T3 (m²), poids, offre immédiate (m²), poids, taux de vacance, projets en chantier > 5 000 m²,
     offre disponible en chantier > 5 000 m², taux de précommercialisation, loyer prime, loyers moyens 1re et
     2de main (€ HT HC/m²/an). Nombres alignés à droite, milliers séparés par une espace : les cellules sont lues
     par position (écart horizontal entre mots), le texte brut étant ambigu ;
   - absents : taux de rendement prime, mesures d'accompagnement, volume investi (MarketBeat Investissement).

On ne stocke que les valeurs et leur référence (page ou section, ligne, colonne), jamais le texte du document.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from config.schema import PROJECT_ROOT
from tools.base import ParseError
from tools.common import make_point, needs_by_indicator
from tools.connectors import connector
from tools.http import FetchResult, SourceUnavailable
from tools.pdf_text import is_pdf, normalise, parse_fr_number, pdf_pages
from tools.registry import DataPoint

METHOD = "report_extraction"


# ================================================================== documents et résultats

@dataclass
class Doc:
    kind: Literal["pdf", "html"]
    content: bytes
    pages: list[str] = field(default_factory=list)   # PDF : texte par page
    soup: Any = None                                  # HTML : arbre BeautifulSoup


@dataclass
class Found:
    """Valeur extraite d'un document, avec sa référence (sans recopier le document)."""
    value: Any
    unit: str
    data_date: str
    ref: str                     # où la valeur se lit : page ou section, ligne, colonne, texte de la cellule
    what: str                    # définition de la valeur retenue et précisions de l'éditeur
    status: Literal["ok", "partial"] = "ok"
    confidence: Literal["high", "medium", "low"] = "high"


@dataclass
class Extraction:
    title: str
    found: dict[str, Found]                                   # indicateur du catalogue -> valeur
    absent: dict[str, str] = field(default_factory=dict)      # indicateur -> raison


@dataclass(frozen=True)
class Layout:
    id: str                      # clé utilisée dans defaults.yaml / submarket_zones
    kind: Literal["pdf", "html"]
    matches: Callable[[Doc], bool]
    extract: Callable[[Doc, str], Extraction]


def read_doc(content: bytes) -> Doc:
    if is_pdf(content):
        return Doc("pdf", content, pages=pdf_pages(content))
    if is_html(content):
        from bs4 import BeautifulSoup
        return Doc("html", content, soup=BeautifulSoup(content.decode("utf-8", "replace"), "html.parser"))
    raise ParseError(f"document ni PDF ni HTML (début : {content[:60]!r})")


def is_html(content: bytes) -> bool:
    head = content[:2048].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<html" in head


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", normalise(text)).strip()


# ================================================================== PDF : lecture des mots positionnés

def page_lines(content: bytes, page_index: int, y_tol: float = 3.0) -> list[list[dict]]:
    """Mots d'une page groupés en lignes (même ordonnée à y_tol près), triés de gauche à droite."""
    import pdfplumber
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        words = pdf.pages[page_index].extract_words(keep_blank_chars=False, use_text_flow=False)
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= y_tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


CELL_TOKEN = re.compile(r"^(?:[-+−–]?\d[\d,.]*%?|-|ND)$")


def split_row(line: list[dict], max_gap: float) -> tuple[str, list[str]]:
    """Sépare une ligne de tableau en (libellé, cellules). Un groupe de 3 chiffres à moins de max_gap points du
    mot précédent (lui-même de 1 à 3 chiffres) prolonge le même nombre (séparateur de milliers)."""
    i = 0
    while i < len(line) and not CELL_TOKEN.match(line[i]["text"]):
        i += 1
    label = " ".join(w["text"] for w in line[:i])
    cells: list[list[dict]] = []
    for w in line[i:]:
        if not CELL_TOKEN.match(w["text"]):
            raise ParseError(f"ligne « {label} » : mot inattendu {w['text']!r} parmi les cellules")
        joinable = (bool(cells) and w["x0"] - cells[-1][-1]["x1"] < max_gap
                    and re.fullmatch(r"\d{3}(?:,\d+)?%?", w["text"]) is not None
                    and re.fullmatch(r"\d{1,3}", cells[-1][-1]["text"]) is not None)
        if joinable:
            cells[-1].append(w)
        else:
            cells.append([w])
    return label, [" ".join(w["text"] for w in c) for c in cells]


def cell_value(raw: str) -> float | None:
    if raw in ("-", "ND"):
        return None
    if raw.endswith("%"):
        return parse_fr_number(raw[:-1]) / 100
    return parse_fr_number(raw)


# ================================================================== 2. Cushman & Wakefield, MarketBeat Bureaux IDF (PDF)

CW_COLUMNS = [
    ("take_up_ytd", "Demande placée depuis janvier (m²)"),
    ("take_up_share", "Poids dans la demande placée"),
    ("immediate_supply", "Offre immédiate (m²)"),
    ("supply_share", "Poids dans l'offre immédiate"),
    ("vacancy", "Taux de vacance"),
    ("pipeline", "Projets en chantier > 5 000 m² (m²)"),
    ("pipeline_available", "Offre disponible en chantier > 5 000 m² (m²)"),
    ("prelet", "Taux de précommercialisation des projets en chantier"),
    ("prime_rent", "Loyers prime (€ HT HC/m²/an)"),
    ("avg_rent_first", "Loyers moyens 1re main (€ HT HC/m²/an)"),
    ("avg_rent_second", "Loyers moyens 2de main (€ HT HC/m²/an)"),
]
CW_HEADERS = ["SOUS-MARCHÉS", "DEMANDE PLACÉE", "OFFRE IMMÉDIATE", "VACANCE", "PRECOMMERCIALISATION",
              "LOYERS PRIME", "1ère main", "2ème main"]
CW_ABSENT = {
    "prime_yield": "le MarketBeat Bureaux ne publie pas de taux de rendement prime (publié dans le MarketBeat Investissement)",
    "incentives": "le MarketBeat Bureaux ne chiffre pas les mesures d'accompagnement",
    "investment_volume": "le MarketBeat Bureaux ne publie pas le volume investi (MarketBeat Investissement)",
    "take_up": ("le rapport publie la demande placée cumulée depuis janvier par sous-marché, pas la valeur du dernier "
                "trimestre : non comparable à l'indicateur trimestriel attendu"),
}


@dataclass
class Cell:
    value: float | None          # None : cellule « - » ou « ND »
    raw: str
    page: int
    row: str
    column: str


@dataclass
class CwTable:
    title: str
    period: str
    zone: str
    cells: dict[str, Cell]


def _cw_matches(doc: Doc) -> bool:
    if doc.kind != "pdf":
        return False
    text = normalise("\n".join(doc.pages))
    return "Cushman & Wakefield" in text and "DONNÉES DE MARCHÉ" in text and "SOUS-MARCHÉS" in text


def read_cw_table(content: bytes, pages: list[str], zone: str) -> CwTable:
    m = re.search(r"(\d)e TRIMESTRE (\d{4})", normalise(pages[0]))
    if not m:
        raise ParseError("trimestre du rapport introuvable en page 1 (attendu « <n>e TRIMESTRE <année> »)")
    idx = next((i for i, t in enumerate(pages) if all(h in normalise(t) for h in CW_HEADERS)), None)
    if idx is None:
        raise ParseError(f"tableau « Données de marché » introuvable (en-têtes attendus : {CW_HEADERS})")
    cells, labels = None, []
    for line in page_lines(content, idx):
        try:
            label, raw = split_row(line, max_gap=6.0)
        except ParseError:
            continue
        if len(raw) == len(CW_COLUMNS):
            labels.append(label)
            if label == zone:
                cells = raw
    if cells is None:
        raise ParseError(f"ligne « {zone} » absente du tableau page {idx + 1} (lignes lues : {labels})")
    return CwTable(title=f"Cushman & Wakefield, MarketBeat Bureaux Île-de-France T{m[1]} {m[2]}",
                   period=f"{m[2]}-T{m[1]}", zone=zone,
                   cells={cid: Cell(cell_value(raw), raw, idx + 1, zone, clabel)
                          for (cid, clabel), raw in zip(CW_COLUMNS, cells)})


def _cw_extract(doc: Doc, zone: str) -> Extraction:
    t = read_cw_table(doc.content, doc.pages, zone)
    c = t.cells

    def ref(*cols):
        return (f"page {c[cols[0]].page}, tableau « Données de marché », ligne « {t.zone} » : "
                + " ; ".join(f"« {c[k].column} » = {c[k].raw}" for k in cols))

    def found(value, unit, what, *cols):
        return None if value is None else Found(value, unit, t.period, ref(*cols), what)

    avg = c["avg_rent_second"].value
    sup = c["immediate_supply"].value
    candidates = {
        "prime_rent": found(c["prime_rent"].value, "€/m²/an", "loyer prime facial (€ HT HC/m²/an)", "prime_rent"),
        "average_rent": found(None if avg is None else {"last": avg, "period": t.period, "first_hand": c["avg_rent_first"].value},
                              "€/m²/an", "loyer moyen de SECONDE main (valeur retenue) ; première main jointe pour information",
                              "avg_rent_second", "avg_rent_first"),
        "vacancy": found(c["vacancy"].value, "fraction", "taux de vacance du sous-marché", "vacancy"),
        "future_supply": found(c["pipeline_available"].value, "m²",
                               "offre future = surfaces encore disponibles dans les projets en chantier > 5 000 m²",
                               "pipeline_available", "pipeline", "prelet"),
        "immediate_supply": found(None if sup is None else {"last": sup, "period": t.period}, "m²",
                                  "offre immédiate du sous-marché", "immediate_supply"),
    }
    absent = dict(CW_ABSENT)
    for ind, f in candidates.items():
        if f is None:
            absent[ind] = f"cellule vide dans le rapport (page {c['prime_rent'].page}, ligne « {t.zone} »)"
    return Extraction(t.title, {k: v for k, v in candidates.items() if v is not None}, absent)


# ================================================================== 1. Global Icons, marché prime Paris QCA (HTML)

GI_HEADERS = {"year": "Année", "volume": "Volume QCA (M€)", "yield": "Rendement prime bureau", "rent": "Loyer prime"}
GI_ABSENT = {
    "average_rent": "la page ne publie pas de loyer moyen",
    "incentives": "la page ne chiffre pas les mesures d'accompagnement",
    "future_supply": "la page ne chiffre pas l'offre future (mention qualitative seulement)",
    "take_up": "la page ne publie pas la demande placée",
    "immediate_supply": "la page ne publie pas l'offre immédiate",
}
YEAR_CELL = re.compile(r"^(\d{4})(?:\s*\((estim\.)\))?$")
GI_NOTE = ("fourchettes indicatives selon l'éditeur (consolidation BNP Paribas RE, CBRE, JLL, C&W ; source secondaire, "
           "à recouper)")


@dataclass
class GiCell:
    raw: str
    low: float | None
    high: float | None
    approx: bool


def gi_cell(raw: str, unit_suffix: str) -> GiCell:
    """« 2,75 % », « 1 150 €/m²/an », « ~2 500 », « 3,75 - 4,00 % » (fourchette) -> bornes en unité de la colonne."""
    t = clean(raw)
    approx = t.startswith("~")
    t = t.lstrip("~").strip()
    if unit_suffix and t.endswith(unit_suffix):
        t = t[: -len(unit_suffix)].strip()
    parts = [p.strip().lstrip("~") for p in re.split(r"\s+-\s+", t)]
    if not 1 <= len(parts) <= 2:
        raise ParseError(f"cellule illisible : {raw!r}")
    vals = [parse_fr_number(p) for p in parts]
    return GiCell(raw=clean(raw), low=vals[0], high=vals[-1], approx=approx)


def _gi_table(doc: Doc):
    for table in doc.soup.select("table.compare-table"):
        heads = [clean(th.get_text()) for th in table.select("thead th")]
        if all(h in heads for h in GI_HEADERS.values()):
            return table, {k: heads.index(h) for k, h in GI_HEADERS.items()}
    return None, None


def _gi_matches(doc: Doc) -> bool:
    if doc.kind != "html":
        return False
    site = doc.soup.find("meta", attrs={"property": "og:site_name"})
    return bool(site and site.get("content") == "Global Icons") and _gi_table(doc)[0] is not None


def _gi_extract(doc: Doc, zone: str) -> Extraction:
    soup = doc.soup
    h1 = clean(soup.h1.get_text()) if soup.h1 else ""
    if zone not in h1:
        raise ParseError(f"la page ne porte pas sur « {zone} » (titre : {h1!r})")
    table, col = _gi_table(doc)
    rows = []
    for tr in table.select("tbody tr"):
        tds = [td.get_text() for td in tr.find_all("td")]
        m = YEAR_CELL.match(clean(tds[col["year"]]))
        if not m:
            raise ParseError(f"année illisible dans le tableau : {tds[col['year']]!r}")
        rows.append((int(m[1]), bool(m[2]), tds))
    observed = [r for r in rows if not r[1]]
    if not observed:
        raise ParseError("aucune année observée (hors estimation) dans le tableau")
    year, _, last = max(observed, key=lambda r: r[0])
    est = next((r for r in rows if r[1]), None)
    title = f"Global Icons, « {h1} »"
    section = "tableau « Volumes investissement et yield 2020-2026 »"
    heading = table.find_previous("h2")
    if heading:
        section = f"tableau « {clean(heading.get_text())} »"

    def est_txt(key):
        return f" ; estimation {est[0]} publiée : {clean(est[2][col[key]])}" if est else ""

    found: dict[str, Found] = {}
    y = gi_cell(last[col["yield"]], "%")
    syn = soup.select_one(".callout")
    syn_m = re.search(r"rendement prime bureau (\d+,\d+)\s*-\s*(\d+,\d+)\s*%", clean(syn.get_text())) if syn else None
    syn_txt = f" ; encadré « Synthèse » : {syn_m[1]}-{syn_m[2]} %" if syn_m else ""
    found["prime_yield"] = Found(y.low / 100, "fraction", str(year),
                                 f"{section}, ligne {year}, colonne « {GI_HEADERS['yield']} » = {y.raw}",
                                 f"taux de rendement prime bureaux du QCA, dernière année observée{est_txt('yield')}{syn_txt} ; {GI_NOTE}",
                                 confidence="medium")
    r = gi_cell(last[col["rent"]], "€/m²/an")
    found["prime_rent"] = Found(r.low, "€/m²/an", str(year),
                                f"{section}, ligne {year}, colonne « {GI_HEADERS['rent']} » = {r.raw}",
                                f"loyer prime bureaux du QCA, dernière année observée{est_txt('rent')} ; {GI_NOTE}",
                                confidence="medium")
    v = gi_cell(last[col["volume"]], "")
    found["investment_volume"] = Found(v.low * 1e6, "€", str(year),
                                       f"{section}, ligne {year}, colonne « {GI_HEADERS['volume']} » = {v.raw}",
                                       f"volume investi dans le QCA{' (approximation « ~ » de l’éditeur)' if v.approx else ''}"
                                       f"{est_txt('volume')} ; {GI_NOTE}", confidence="low")
    absent = dict(GI_ABSENT)
    # vacance : FAQ, publiée en fourchette approximative et sans date -> milieu de fourchette, statut partiel
    page_year = re.search(r"Analyse marché · (\d{4})", clean(soup.get_text(" ")))
    vac_p = next((p for p in soup.find_all("p") if "Vacance globale" in p.get_text()), None)
    vm = re.search(r"Vacance globale\s*~?\s*(\d+(?:,\d+)?)\s*-\s*(\d+(?:,\d+)?)\s*%", clean(vac_p.get_text())) if vac_p else None
    if vm and page_year:
        lo, hi = parse_fr_number(vm[1]) / 100, parse_fr_number(vm[2]) / 100
        q = vac_p.find_previous("h3")
        grade_a = re.search(r"vacance prime grade A\s*(<|&lt;)\s*(\d+(?:,\d+)?)\s*%", clean(vac_p.get_text()))
        found["vacancy"] = Found(
            round((lo + hi) / 2, 6), "fraction", page_year[1],
            f"section « Questions fréquentes »{f', question « {clean(q.get_text())} »' if q else ''} : "
            f"« Vacance globale ~{vm[1]}-{vm[2]} % »",
            f"taux de vacance global du QCA publié en fourchette approximative ({vm[1]}-{vm[2]} %), sans date : valeur "
            f"retenue = milieu de la fourchette, date = année de l'analyse ({page_year[1]})"
            f"{f' ; vacance prime grade A < {grade_a[2]} % selon la même page' if grade_a else ''}",
            status="partial", confidence="low")
    else:
        absent["vacancy"] = "taux de vacance introuvable sur la page (attendu « Vacance globale ~a-b % »)"
    return Extraction(title, found, absent)


LAYOUTS = [Layout("cushman_marketbeat_bureaux_idf", "pdf", _cw_matches, _cw_extract),
           Layout("globalicons_qca", "html", _gi_matches, _gi_extract)]


def detect_layout(doc: Doc) -> Layout:
    for layout in LAYOUTS:
        if layout.kind == doc.kind and layout.matches(doc):
            return layout
    raise ParseError(f"mise en page inconnue : aucun extracteur ne reconnaît ce document {doc.kind.upper()} "
                     f"(extracteurs disponibles : {[l.id for l in LAYOUTS]})")


# ================================================================== DataPoints

def points_from_extraction(source, ex: Extraction, wanted: dict[str, str], fetched: FetchResult,
                           origin: str) -> list[DataPoint]:
    points = []
    for key, indicator in wanted.items():
        f = ex.found.get(indicator)
        if f is None:
            why = ex.absent.get(indicator, f"aucun extracteur pour « {indicator} »")
            points.append(DataPoint.missing(key, source.name, f"{ex.title} : {why}", source_url=fetched.url, method=METHOD))
            continue
        notes = f"{f.what}. {ex.title}, {f.ref}. Lecture : {origin}."
        points.append(make_point(source, key, f.value, f.unit, f.data_date, fetched, notes,
                                 confidence=f.confidence, status=f.status, method=METHOD))
    return points


# ================================================================== récupération du document

def load_report(ctx, source) -> tuple[FetchResult, str]:
    """(document, origine) : téléchargement d'abord, copie locale (params.local_file) en secours."""
    reasons = []
    if source.url and ctx.client is not None:
        try:
            r = ctx.client.get(source.id, source.url)
            if is_pdf(r.content) or is_html(r.content):
                return r, "téléchargement" + (" (cache)" if r.from_cache else "")
            reasons.append(f"l'URL ne renvoie ni PDF ni page HTML (type {r.content_type!r})")
        except SourceUnavailable as exc:
            reasons.append(exc.reason)
    elif not source.url:
        reasons.append("aucune URL au catalogue")
    else:
        reasons.append("aucun client HTTP")
    local = source.params.get("local_file")
    if local:
        path = Path(local) if Path(local).is_absolute() else PROJECT_ROOT / local
        if path.is_file():
            content = path.read_bytes()
            when = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
            r = FetchResult(source_id=source.id, url=source.url or path.as_uri(), status_code=200, content=content,
                            content_type=None, retrieved_at=when, from_cache=False)
            return r, f"copie locale {local} (téléchargement impossible : {' ; '.join(reasons)})"
        reasons.append(f"copie locale {local!r} introuvable")
    raise SourceUnavailable(source.id, source.url or "", " ; ".join(reasons)
                            + " — enregistrer le document dans le projet et renseigner params.local_file du catalogue")


def fetch_report(ctx, needs, source_id: str) -> list[DataPoint]:
    source = ctx.catalog.by_id(source_id)
    wanted = needs_by_indicator(needs, {i: i for i in source.indicators})
    fetched, origin = load_report(ctx, source)
    doc = read_doc(fetched.content)
    layout = detect_layout(doc)
    submarket = ctx.config.asset.submarket
    zone = ctx.defaults.submarket_zones.get(submarket or "", {}).get(layout.id)
    if zone is None:
        raise ParseError(f"sous-marché {submarket!r} sans correspondance « {layout.id} » dans "
                         "defaults.yaml/submarket_zones")
    return points_from_extraction(source, layout.extract(doc, zone), wanted, fetched, origin)


@connector("market_reports")
def fetch_market_reports(ctx, needs):
    """Page d'analyse de marché (HTML) indiquée dans le catalogue."""
    return fetch_report(ctx, needs, "market_reports")


@connector("cushman_marketbeat")
def fetch_cushman_marketbeat(ctx, needs):
    """Rapport PDF Cushman & Wakefield MarketBeat Bureaux Île-de-France."""
    return fetch_report(ctx, needs, "cushman_marketbeat")
