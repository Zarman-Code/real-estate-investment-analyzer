"""Socle commun des connecteurs : transforme tout échec en DataPoint(status="missing").

Usage dans un connecteur :

    def fetch_euribor(client, catalog) -> list[DataPoint]:
        src = catalog.by_id("ecb_euribor")
        return run_connector(src, ["rates.euribor_3m"], lambda: _fetch_and_parse(client, src))

Si _fetch_and_parse lève une exception (source indisponible, format inattendu),
run_connector renvoie un DataPoint missing par clé attendue, avec la raison en notes.
"""
from __future__ import annotations

from typing import Callable

from config.schema import Source
from tools.http import SourceUnavailable
from tools.registry import DataPoint


class ParseError(ValueError):
    """La réponse est arrivée mais ne correspond pas au format documenté / inspecté."""


def run_connector(source: Source, expected_keys: list[str],
                  fn: Callable[[], list[DataPoint]]) -> list[DataPoint]:
    try:
        points = fn()
    except SourceUnavailable as exc:
        reason = f"source indisponible : {exc.reason}"
    except ParseError as exc:
        reason = f"format de réponse inattendu : {exc}"
    else:
        got = {p.key for p in points}
        # une clé attendue que le connecteur n'a pas produite est explicitement manquante
        return points + [DataPoint.missing(k, source.name, "non renvoyée par la source",
                                           source_url=source.url) for k in expected_keys if k not in got]
    return [DataPoint.missing(k, source.name, reason, source_url=source.url) for k in expected_keys]
