"""Client HTTP commun à tous les connecteurs (règles de la section 3.1 du plan).

- Cache disque horodaté : cache/<source>/<hash_requête>.json -> rejouabilité, pas de re-requête.
- Timeout, retries avec backoff exponentiel sur erreurs réseau / 429 / 5xx.
- Un échec lève SourceUnavailable : le connecteur le transforme en DataPoint(status="missing"),
  jamais en plantage ni en valeur par défaut silencieuse.

Le client ne sait RIEN du contenu des réponses : le parsing est propre à chaque connecteur
et n'est écrit qu'après inspection d'une vraie réponse (règle 5).
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from config.schema import PROJECT_ROOT

DEFAULT_CACHE_DIR = PROJECT_ROOT / "cache"
RETRY_STATUS = {429, 500, 502, 503, 504}
USER_AGENT = "helios-agent/0.2 (aide a la decision immobiliere)"


class SourceUnavailable(RuntimeError):
    """La source n'a pas répondu de façon exploitable (réseau, HTTP, timeout)."""

    def __init__(self, source_id: str, url: str, reason: str):
        super().__init__(f"[{source_id}] {reason} ({url})")
        self.source_id, self.url, self.reason = source_id, url, reason


@dataclass(frozen=True)
class FetchResult:
    source_id: str
    url: str                      # URL finale, paramètres inclus
    status_code: int
    content: bytes
    content_type: str | None
    retrieved_at: str             # horodatage de la récupération RÉELLE (pas de la lecture du cache)
    from_cache: bool

    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.content)


def request_key(method: str, url: str, params: dict[str, Any] | None) -> str:
    canon = json.dumps({"m": method.upper(), "u": url, "p": sorted((params or {}).items())},
                       ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:24]


class HttpClient:
    def __init__(self, cache_dir: Path | str = DEFAULT_CACHE_DIR, timeout: float = 20.0,
                 max_retries: int = 3, backoff_s: float = 1.0, use_cache: bool = True,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, offline: bool = False):
        self.cache_dir = Path(cache_dir)
        self.offline = offline          # True : ne sert que le cache, aucune requête réseau
        self.max_retries, self.backoff_s, self.use_cache = max_retries, backoff_s, use_cache
        self._sleep = sleep
        self._client = httpx.Client(timeout=timeout, transport=transport, follow_redirects=True,
                                    headers={"User-Agent": USER_AGENT})

    # ------------------------------------------------------------------ cache
    def _cache_path(self, source_id: str, key: str) -> Path:
        return self.cache_dir / source_id / f"{key}.json"

    def _read_cache(self, source_id: str, key: str) -> FetchResult | None:
        p = self._cache_path(source_id, key)
        if not (self.use_cache and p.is_file()):
            return None
        raw = json.loads(p.read_text(encoding="utf-8"))
        return FetchResult(source_id=source_id, url=raw["url"], status_code=raw["status_code"],
                           content=base64.b64decode(raw["content_b64"]), content_type=raw["content_type"],
                           retrieved_at=raw["retrieved_at"], from_cache=True)

    def _write_cache(self, key: str, r: FetchResult) -> None:
        p = self._cache_path(r.source_id, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "url": r.url, "status_code": r.status_code, "content_type": r.content_type,
            "retrieved_at": r.retrieved_at, "content_b64": base64.b64encode(r.content).decode("ascii"),
        }, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------ requête
    def get(self, source_id: str, url: str, params: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None) -> FetchResult:
        key = request_key("GET", url, params)
        cached = self._read_cache(source_id, key)
        if cached is not None:
            return cached
        if self.offline:
            raise SourceUnavailable(source_id, url, "mode hors ligne : réponse absente du cache")

        last_reason = "aucune tentative"
        for attempt in range(self.max_retries + 1):
            if attempt:
                self._sleep(self.backoff_s * 2 ** (attempt - 1))
            try:
                resp = self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last_reason = f"erreur réseau : {type(exc).__name__}: {exc}"
                continue
            if resp.status_code in RETRY_STATUS:
                last_reason = f"HTTP {resp.status_code}"
                continue
            if resp.status_code >= 400:
                raise SourceUnavailable(source_id, str(resp.request.url), f"HTTP {resp.status_code}")
            result = FetchResult(source_id=source_id, url=str(resp.request.url), status_code=resp.status_code,
                                 content=resp.content, content_type=resp.headers.get("content-type"),
                                 retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                 from_cache=False)
            if self.use_cache:
                self._write_cache(key, result)
            return result
        raise SourceUnavailable(source_id, url, f"{last_reason} après {self.max_retries + 1} tentatives")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
