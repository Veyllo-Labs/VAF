import vaf.tools.search as search
from vaf.tools.search import WebSearchTool
from vaf.core.config import Config


def _raise(*a, **k):
    raise RuntimeError("skip intent filter in tests")


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    # The intent-based site filter calls query_analyzer; skip it (no net/LLM in tests).
    import vaf.core.query_analyzer as qa
    monkeypatch.setattr(qa, "analyze_query", _raise)


def _cache_files(tmp_path):
    return list((tmp_path / "tmp" / "web_search_cache").glob("*.json"))


def test_cache_helpers_roundtrip_and_ttl(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    t = WebSearchTool()
    key = t._ws_cache_key("Berlin Weather", 5, True, False, "Berlin Weather")

    assert t._ws_cache_get(key, 900) is None          # empty
    t._ws_cache_set(key, "Berlin Weather", "RESULT")
    assert t._ws_cache_get(key, 900) == "RESULT"      # hit within ttl
    assert t._ws_cache_get(key, 0) is None            # ttl=0 -> miss

    # normalized: whitespace/case-insensitive -> same key
    assert key == t._ws_cache_key("  berlin weather ", 5, True, False, "berlin weather")
    # max_results is part of the key
    assert key != t._ws_cache_key("Berlin Weather", 6, True, False, "Berlin Weather")


def test_identical_query_served_from_cache(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    calls = {"n": 0}

    def fake_search(q, n):
        calls["n"] += 1
        return ([{"title": "T", "href": "https://x", "body": "snippet"}], "DuckDuckGo", "")

    monkeypatch.setattr(search, "get_web_search_results", fake_search)

    t = WebSearchTool()
    r1 = t.run(query="hermetic cache query", deep=False)
    r2 = t.run(query="hermetic cache query", deep=False)

    assert calls["n"] == 1            # 2nd identical query served from cache, no re-search
    assert r1 == r2 and r1.strip()    # same, non-empty result
    assert _cache_files(tmp_path)     # a cache file was written


def test_return_raw_bypasses_cache(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    calls = {"n": 0}

    def fake_search(q, n):
        calls["n"] += 1
        return ([{"title": "T", "href": "https://x", "body": "b"}], "DuckDuckGo", "")

    monkeypatch.setattr(search, "get_web_search_results", fake_search)
    t = WebSearchTool()
    t.run(query="raw q", return_raw=True)
    t.run(query="raw q", return_raw=True)

    assert calls["n"] == 2            # return_raw is never cached
    assert not _cache_files(tmp_path)


def test_rag_fallback_not_cached(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    calls = {"n": 0}

    def fake_search(q, n):
        calls["n"] += 1
        return ([{"title": "T", "href": "", "body": "knowledge"}], "Internal Knowledge (RAG)", "")

    monkeypatch.setattr(search, "get_web_search_results", fake_search)
    t = WebSearchTool()
    t.run(query="rag q", deep=False)
    t.run(query="rag q", deep=False)

    assert calls["n"] == 2            # RAG fallback is never cached -> retries the web
    assert not _cache_files(tmp_path)
