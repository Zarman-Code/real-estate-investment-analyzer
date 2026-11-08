"""Tests du client HTTP et du socle connecteur, sans réseau (httpx.MockTransport)."""
import httpx
import pytest

from config.schema import load_sources_catalog
from tools.base import ParseError, run_connector
from tools.http import HttpClient, SourceUnavailable, request_key
from tools.registry import DataPoint


def make_client(tmp_path, handler, **kw):
    sleeps = []
    c = HttpClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw)
    return c, sleeps


def test_get_success_and_cache(tmp_path):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, json={"v": 1})

    c, _ = make_client(tmp_path, handler)
    r1 = c.get("src", "https://x.test/a", {"q": "b"})
    r2 = c.get("src", "https://x.test/a", {"q": "b"})
    assert r1.json() == {"v": 1} and not r1.from_cache
    assert r2.from_cache and r2.content == r1.content
    assert r2.retrieved_at == r1.retrieved_at      # date de la récupération réelle conservée
    assert len(calls) == 1                         # pas de re-requête
    assert list((tmp_path / "src").glob("*.json"))


def test_cache_disabled(tmp_path):
    calls = []
    c, _ = make_client(tmp_path, lambda r: calls.append(1) or httpx.Response(200, text="ok"), use_cache=False)
    c.get("src", "https://x.test/a")
    c.get("src", "https://x.test/a")
    assert len(calls) == 2


def test_retry_then_success(tmp_path):
    seq = iter([503, 429, 200])
    c, sleeps = make_client(tmp_path, lambda r: httpx.Response(next(seq), text="ok"), max_retries=3, backoff_s=1)
    assert c.get("src", "https://x.test/a").status_code == 200
    assert sleeps == [1, 2]                        # backoff exponentiel


def test_retries_exhausted(tmp_path):
    c, sleeps = make_client(tmp_path, lambda r: httpx.Response(500), max_retries=2)
    with pytest.raises(SourceUnavailable, match="3 tentatives"):
        c.get("src", "https://x.test/a")
    assert len(sleeps) == 2


def test_network_error(tmp_path):
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    c, _ = make_client(tmp_path, handler, max_retries=1)
    with pytest.raises(SourceUnavailable, match="erreur réseau"):
        c.get("src", "https://x.test/a")


def test_client_error_not_retried(tmp_path):
    calls = []
    c, _ = make_client(tmp_path, lambda r: calls.append(1) or httpx.Response(404))
    with pytest.raises(SourceUnavailable, match="HTTP 404"):
        c.get("src", "https://x.test/a")
    assert len(calls) == 1


def test_failures_not_cached(tmp_path):
    seq = iter([404, 200])
    c, _ = make_client(tmp_path, lambda r: httpx.Response(next(seq), text="ok"))
    with pytest.raises(SourceUnavailable):
        c.get("src", "https://x.test/a")
    assert c.get("src", "https://x.test/a").status_code == 200


def test_request_key_stable_and_param_order_independent():
    assert request_key("GET", "u", {"a": 1, "b": 2}) == request_key("get", "u", {"b": 2, "a": 1})
    assert request_key("GET", "u", {"a": 1}) != request_key("GET", "u", {"a": 2})


# ------------------------------------------------------------------ socle connecteur

SRC = load_sources_catalog().by_id("ecb_euribor")
KEYS = ["rates.euribor_3m", "rates.euribor_3m_history"]


def test_run_connector_unavailable_gives_missing():
    def fn():
        raise SourceUnavailable("ecb_euribor", "u", "HTTP 503")

    pts = run_connector(SRC, KEYS, fn)
    assert [p.key for p in pts] == KEYS
    assert all(p.status == "missing" and p.value is None and "indisponible" in p.notes for p in pts)


def test_run_connector_parse_error_gives_missing():
    def fn():
        raise ParseError("colonne OBS_VALUE absente")

    pts = run_connector(SRC, KEYS, fn)
    assert all(p.status == "missing" and "OBS_VALUE" in p.notes for p in pts)


def test_run_connector_partial_output_completed_with_missing():
    ok = DataPoint(key="rates.euribor_3m", value=0.02, unit="fraction", source_name=SRC.name,
                   data_date="2026-10", method="api", status="ok", confidence="high")
    pts = run_connector(SRC, KEYS, lambda: [ok])
    assert pts[0] is ok
    assert pts[1].key == "rates.euribor_3m_history" and pts[1].status == "missing"


def test_run_connector_does_not_swallow_bugs():
    """Une vraie erreur de programmation doit remonter, pas devenir une donnée manquante."""
    with pytest.raises(ZeroDivisionError):
        run_connector(SRC, KEYS, lambda: [1 / 0])


def test_offline_serves_cache_only(tmp_path):
    calls = []
    online, _ = make_client(tmp_path, lambda r: calls.append(1) or httpx.Response(200, text="ok"))
    online.get("src", "https://x.test/a")
    offline, _ = make_client(tmp_path, lambda r: calls.append(1) or httpx.Response(200, text="ok"), offline=True)
    assert offline.get("src", "https://x.test/a").from_cache
    with pytest.raises(SourceUnavailable, match="hors ligne"):
        offline.get("src", "https://x.test/autre")
    assert len(calls) == 1
