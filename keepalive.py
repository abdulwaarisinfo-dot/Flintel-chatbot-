"""
keepalive.py — pings this app's own GET /ping every KEEPALIVE_INTERVAL_SECONDS
so the host never idles the server into sleep / spin-down.

Touches nothing else: no MongoDB, no session, no LLM, no business logic. The
target is only an HTTP endpoint that returns {"status": "ok"}. Every failure
is caught and logged at DEBUG, so a slow, failing or timed-out ping can never
crash the app, block a request, or spam the logs.

Config (config.py, all from env):
  KEEPALIVE_ENABLED          default true
  KEEPALIVE_URL              public base URL; falls back to RENDER_EXTERNAL_URL
  KEEPALIVE_INTERVAL_SECONDS default 30
"""
import asyncio
import logging

import httpx

import config

log = logging.getLogger("flintel-keepalive")

KEEPALIVE_TIMEOUT_SECONDS = 10.0

_started = False          # one loop per process, even if startup runs twice
_task = None


async def _keepalive_loop(url: str, interval: float) -> None:
    try:
        async with httpx.AsyncClient(timeout=KEEPALIVE_TIMEOUT_SECONDS, follow_redirects=False) as client:
            while True:
                await asyncio.sleep(interval)
                try:
                    response = await client.get(url)
                    log.debug(f"keepalive ping {url} -> {response.status_code}")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:           # timeout, refused, DNS, ... — never fatal
                    log.debug(f"keepalive ping {url} failed: {type(exc).__name__}: {exc}")
    except asyncio.CancelledError:
        log.debug("keepalive loop stopped")
        raise
    except Exception as exc:                       # e.g. the client itself could not be created
        log.debug(f"keepalive loop ended: {type(exc).__name__}: {exc}")


async def start_keepalive():
    """Call from a FastAPI startup hook. Creates ONE background task (fire and
    forget, same style as the trending-keywords scheduler) and returns it, or
    returns None when keep-alive is disabled, has no URL, or already started.
    Never raises."""
    global _started, _task
    try:
        if _started:
            return  
        if not getattr(config, "KEEPALIVE_ENABLED", True):
            log.info("keepalive disabled: KEEPALIVE_ENABLED is off")
            return None
        base = (getattr(config, "KEEPALIVE_URL", "") or "").strip().rstrip("/")
        if not base:
            log.info("keepalive disabled: no URL configured")
            return None
        interval = float(getattr(config, "KEEPALIVE_INTERVAL_SECONDS", 30) or 30)
        url = base + "/ping"
        _task = asyncio.create_task(_keepalive_loop(url, interval))
        _started = True
        log.info(f"keepalive started: GET {url} every {interval:g}s")
        return _task
    except Exception as exc:
        log.warning(f"keepalive could not start: {exc}")
        return None
