"""TEST-ONLY wrapper that delays selected request paths, used by the browser e2e run to imitate a slow backend.

    E2E_DELAYS="/match=7,/difficulty=40" python -m uvicorn slow_app:app --app-dir extension/e2e   (cwd: backend/)

Not part of the product: it is never imported by the application and is not packaged.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())  # backend/ (for `app.main`)

from app.main import app as inner  # noqa: E402

RULES = [(frag, float(secs)) for frag, secs in (r.split("=") for r in os.environ.get("E2E_DELAYS", "").split(",") if r)]


async def app(scope, receive, send):
    if scope["type"] == "http":
        for fragment, seconds in RULES:
            if fragment in scope["path"]:
                await asyncio.sleep(seconds)
                break
    await inner(scope, receive, send)
