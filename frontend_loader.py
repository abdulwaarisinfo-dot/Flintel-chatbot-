"""
View-Source hide: browser ke normal page-open (navigation) ko chhota loader milta hai,
loader phir same URL ko fetch() se mangta hai aur asli page milta hai.

- Route sirf EK baar chalta hai (loader wali request route tak pohanchti hi nahi).
- Bots / curl / Googlebot (jin ke paas Sec-Fetch-* headers nahi) ko poora page milta hai -> SEO safe.
- POST / API / stream / OAuth routes ko touch nahi karta.
"""
import os
import re
from fastapi import Request
from fastapi.responses import HTMLResponse

_LOADER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "loader.html")
_PAGE_RE = re.compile(r"^/(?:billing/upgrade|chat/[^/]+)?$")   # "/", "/billing/upgrade", "/chat/<id>"


def install_frontend_loader(app):
    with open(_LOADER_PATH, "r", encoding="utf-8") as f:
        loader_html = f.read()

    @app.middleware("http")
    async def frontend_loader_gate(request: Request, call_next):
        h = request.headers
        if (
            request.method == "GET"
            and _PAGE_RE.match(request.url.path)
            and h.get("sec-fetch-dest") == "document"
            and h.get("sec-fetch-mode") == "navigate"
            and h.get("x-flintel-client") != "1"
            and os.getenv("FLINTEL_DISABLE_LOADER") != "1"
        ):
            return HTMLResponse(
                loader_html,
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                    "Vary": "Sec-Fetch-Dest, Sec-Fetch-Mode",
                },
            )
        return await call_next(request)
