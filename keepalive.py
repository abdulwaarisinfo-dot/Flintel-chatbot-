"""
tests/test_keepalive.py — GET/HEAD /ping and the keepalive.py self-ping loop.

No real Mongo / LLM / network: index is imported against stub modules (every
`database.*` name is a recording MagicMock, so any DB touch shows up as a call),
and the loop pings an httpx.MockTransport. httpx itself is the REAL module here
(TestClient and keepalive.py both need it).
"""
import asyncio
import importlib
import logging
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODS = ("logics", "config", "index", "routes", "keepalive")


@pytest.fixture
def app_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-keepalive")
    db_names = {}
    db = types.ModuleType("database")

    def _db_attr(name):
        if name.startswith("__"):
            raise AttributeError(name)
        return db_names.setdefault(name, MagicMock(name=f"database.{name}"))
    db.__getattr__ = _db_attr
    fi = types.ModuleType("flintel")
    for name in ("ROUTER_UNFILTERED_ADDENDUM", "GENERIC_PAIN_POINT_INFERENCE_ADDENDUM"):
        setattr(fi, name, "")
    fi.__getattr__ = lambda name: MagicMock(name=f"flintel.{name}")
    gg = types.ModuleType("google")
    gg.__getattr__ = lambda name: MagicMock(name=f"google.{name}")
    plc = types.ModuleType("passlib.context")
    plc.CryptContext = MagicMock()
    aus = types.ModuleType("authlib.integrations.starlette_client")
    aus.OAuth = MagicMock()
    sch = types.ModuleType("scheduler")
    sch.start_trending_keywords_scheduler = MagicMock(name="scheduler.start")
    stubs = {"database": db, "flintel": fi, "google": gg,
             "website_intelligence": types.ModuleType("website_intelligence"),
             "passlib": types.ModuleType("passlib"), "passlib.context": plc,
             "authlib": types.ModuleType("authlib"), "authlib.integrations": types.ModuleType("authlib.integrations"),
             "authlib.integrations.starlette_client": aus, "scheduler": sch}
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)
    for name in MODS:
        monkeypatch.delitem(sys.modules, name, raising=False)
    idx = importlib.import_module("index")
    importlib.import_module("routes")
    yield idx, db_names
    for name in MODS:
        sys.modules.pop(name, None)


# ── /ping route ──────────────────────────────────────────────────────────────
def test_ping_get_and_head_touch_nothing(app_env, monkeypatch):
    from fastapi.testclient import TestClient
    idx, db_names = app_env
    rt = sys.modules["routes"]
    forbidden = MagicMock(side_effect=AssertionError("must not be called"))
    for mod in (idx, rt):
        for fn in ("get_owner", "get_anon_id", "get_current_user"):
            if hasattr(mod, fn):
                monkeypatch.setattr(mod, fn, forbidden)
    for m in db_names.values():
        m.reset_mock()
    client = TestClient(idx.app)                      # no `with`: startup hooks not run here
    for _ in range(5):
        r = client.get("/ping")
        assert r.status_code == 200 and r.json() == {"status": "ok"}
        assert "set-cookie" not in r.headers               # never creates a session cookie
        assert "no-store" in r.headers.get("cache-control", "")   # no-cache middleware still applies
    h = client.head("/ping")
    assert h.status_code == 200 and h.content == b"" and "set-cookie" not in h.headers
    assert not client.cookies
    assert forbidden.call_count == 0
    assert [n for n, m in db_names.items() if m.mock_calls] == []   # no database object used


def test_startup_hook_registered_and_scheduler_hook_kept(app_env):
    idx, _ = app_env
    names = [h.__name__ for h in idx.app.router.on_startup]
    assert "_start_trending_keywords_scheduler" in names and "_start_keepalive" in names


def test_config_exports():
    import config
    for name in ("KEEPALIVE_ENABLED", "KEEPALIVE_URL", "KEEPALIVE_INTERVAL_SECONDS"):
        assert name in config.__all__ and hasattr(config, name)


# ── keepalive.py ─────────────────────────────────────────────────────────────
@pytest.fixture
def ka(monkeypatch):
    sys.modules.pop("keepalive", None)
    mod = importlib.import_module("keepalive")
    yield mod
    sys.modules.pop("keepalive", None)


def _cfg(monkeypatch, ka, enabled=True, url="https://flintel.test", interval=0.01):
    monkeypatch.setattr(ka.config, "KEEPALIVE_ENABLED", enabled, raising=False)
    monkeypatch.setattr(ka.config, "KEEPALIVE_URL", url, raising=False)
    monkeypatch.setattr(ka.config, "KEEPALIVE_INTERVAL_SECONDS", interval, raising=False)


def _mock_client(monkeypatch, ka, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(ka.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_disabled_starts_nothing(ka, monkeypatch, caplog):
    _cfg(monkeypatch, ka, enabled=False)
    created = MagicMock()
    monkeypatch.setattr(ka.asyncio, "create_task", created)
    with caplog.at_level(logging.DEBUG, logger="flintel-keepalive"):
        assert asyncio.run(ka.start_keepalive()) is None
    assert created.call_count == 0 and ka._task is None and ka._started is False


def test_no_url_logs_one_info_line(ka, monkeypatch, caplog):
    _cfg(monkeypatch, ka, url="")
    with caplog.at_level(logging.DEBUG, logger="flintel-keepalive"):
        assert asyncio.run(ka.start_keepalive()) is None
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert infos == ["keepalive disabled: no URL configured"]


def test_env_parsing(monkeypatch):
    monkeypatch.setenv("KEEPALIVE_ENABLED", "false")
    monkeypatch.delenv("KEEPALIVE_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://x.onrender.com")
    monkeypatch.setenv("KEEPALIVE_INTERVAL_SECONDS", "abc")
    sys.modules.pop("config", None)
    try:
        import config
        assert config.KEEPALIVE_ENABLED is False
        assert config.KEEPALIVE_URL == "https://x.onrender.com"     # RENDER_EXTERNAL_URL fallback
        assert config.KEEPALIVE_INTERVAL_SECONDS == 30
    finally:
        sys.modules.pop("config", None)


def test_pings_survive_errors_logs_debug_only_and_stops_cleanly(ka, monkeypatch, caplog):
    _cfg(monkeypatch, ka, url="https://flintel.test/")
    hits = []

    def handler(request):
        hits.append(str(request.url))
        if len(hits) == 2:
            raise httpx.ConnectTimeout("slow")
        if len(hits) == 3:
            raise RuntimeError("boom")
        return httpx.Response(200 if len(hits) != 4 else 503, json={"status": "ok"})
    _mock_client(monkeypatch, ka, handler)

    async def main():
        t1 = await ka.start_keepalive()
        t2 = await ka.start_keepalive()                    # second start is a no-op
        assert t1 is not None and t2 is None
        while len(hits) < 6:
            await asyncio.sleep(0.01)
        t1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t1
        return t1
    with caplog.at_level(logging.DEBUG, logger="flintel-keepalive"):
        task = asyncio.run(main())
    assert task.cancelled()
    assert set(hits) == {"https://flintel.test/ping"}
    loop_msgs = [r for r in caplog.records if not r.getMessage().startswith("keepalive started")]
    assert len(loop_msgs) >= 6
    assert {r.levelno for r in loop_msgs} == {logging.DEBUG}   # no INFO/WARNING per ping
    assert any("ConnectTimeout" in r.getMessage() for r in loop_msgs)
    infos = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert infos == ["keepalive started: GET https://flintel.test/ping every 0.01s"]


def test_slow_ping_does_not_block_event_loop(ka, monkeypatch):
    _cfg(monkeypatch, ka)

    async def slow_handler(request):
        await asyncio.sleep(5)
        return httpx.Response(200)
    real = httpx.AsyncClient
    monkeypatch.setattr(ka.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(slow_handler), **kw))

    async def main():
        task = await ka.start_keepalive()
        await asyncio.sleep(0.05)                          # ping now in flight
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.sleep(0.01)                          # other work still runs promptly
        lag = loop.time() - t0
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return lag
    assert asyncio.run(main()) < 0.5


def test_start_never_raises(ka, monkeypatch):
    _cfg(monkeypatch, ka)
    def fail(coro):
        coro.close()
        raise RuntimeError("no loop")
    monkeypatch.setattr(ka.asyncio, "create_task", fail)
    assert asyncio.run(ka.start_keepalive()) is None and ka._started is False
