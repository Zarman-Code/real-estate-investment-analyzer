"""Lecture des rapports PDF : texte page par page et nombres écrits à la française.

Aucune connaissance d'un éditeur ici : les extracteurs propres à chaque rapport (libellés, tableaux)
sont écrits dans leur module après inspection du vrai fichier (règle 5).
"""
from __future__ import annotations

import io
import re

from tools.base import ParseError

# espaces utilisés comme séparateurs de milliers dans les PDF français (espace, insécable, fine insécable)
_SPACES = "     "
NUMBER = rf"[-+−–]?\d{{1,3}}(?:[{_SPACES}]\d{{3}})*(?:[,.]\d+)?|[-+−–]?\d+(?:[,.]\d+)?"
NUMBER_RE = re.compile(NUMBER)


def is_pdf(content: bytes) -> bool:
    return content[:1024].lstrip().startswith(b"%PDF")


def pdf_pages(content: bytes) -> list[str]:
    """Texte de chaque page (index 0 = page 1). Lève ParseError si le contenu n'est pas un PDF lisible."""
    if not is_pdf(content):
        head = content[:80].decode("utf-8", "replace").strip()
        raise ParseError(f"le contenu reçu n'est pas un PDF (début : {head!r})")
    try:
        import pdfplumber
    except ImportError as exc:  # dépendance déclarée dans requirements.txt
        raise ParseError("pdfplumber n'est pas installé (pip install -r requirements.txt)") from exc
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = [(p.extract_text() or "") for p in pdf.pages]
    except Exception as exc:  # noqa: BLE001 - PDF corrompu ou chiffré
        raise ParseError(f"PDF illisible : {type(exc).__name__}: {exc}") from None
    if not any(t.strip() for t in pages):
        raise ParseError("PDF sans texte extractible (document scanné ?)")
    return pages


def parse_fr_number(s: str) -> float:
    """« 1 250 » -> 1250.0 ; « 6,59 » -> 6.59 ; « −14 » -> -14.0. Lève ParseError si ce n'est pas un nombre."""
    t = s.strip()
    for c in _SPACES[1:]:
        t = t.replace(c, " ")
    if " " in t and not re.fullmatch(r"[-+−–]?\d{1,3}(?: \d{3})+(?:,\d+)?", t):
        raise ParseError(f"nombre illisible (groupes de milliers) : {s!r}")
    t = t.replace(" ", "")
    t = t.replace("−", "-").replace("–", "-").replace(",", ".")
    if t.startswith("+"):
        t = t[1:]
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", t):
        raise ParseError(f"nombre illisible : {s!r}")
    return float(t)


def normalise(text: str) -> str:
    """Espaces homogènes (insécables -> espace simple) pour des recherches de libellés robustes."""
    return re.sub(rf"[{_SPACES[1:]}]", " ", text)
