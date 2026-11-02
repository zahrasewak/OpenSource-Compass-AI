"""ASGI middleware that gives every HTTP request a RequestContext (cancellation on client disconnect + a time budget).

A single "pump" task is the only reader of the server's `receive` channel. It forwards messages to the application through
a queue and sets the cancel flag the moment the server reports `http.disconnect`. Having one reader avoids two consumers
competing for the same messages (the framework's own middleware also reads them).
"""

import asyncio
import re

from app.core import requestctx

# Endpoints that may legitimately make a dozen GitHub requests on first use get the longer budget.
_SLOW_PATH = re.compile(r"/(repositories/[^/]+/[^/]+/health|issues/[^/]+/[^/]+/\d+/readiness)/?$")


class RequestContextMiddleware:
    def __init__(self, app, *, enabled: bool = True, deadline_seconds: float = 25.0, slow_deadline_seconds: float = 60.0) -> None:
        self.app = app
        self.enabled = enabled
        self.deadline_seconds = deadline_seconds
        self.slow_deadline_seconds = slow_deadline_seconds

    def _budget(self, path: str) -> float:
        return self.slow_deadline_seconds if _SLOW_PATH.search(path) else self.deadline_seconds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        ctx = requestctx.RequestContext(self._budget(scope.get("path", "")))
        token = requestctx.activate(ctx)
        queue: asyncio.Queue = asyncio.Queue()

        async def pump() -> None:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    ctx.cancel.set()
                queue.put_nowait(message)
                if message["type"] == "http.disconnect":
                    return

        async def app_receive():
            message = await queue.get()
            if message["type"] == "http.disconnect":
                queue.put_nowait(message)  # later reads keep reporting the disconnect
            return message

        reader = asyncio.create_task(pump())
        try:
            await self.app(scope, app_receive, send)
        finally:
            reader.cancel()
            await asyncio.wait({reader})  # returns once the reader has stopped; never raises, never swallows OUR cancellation
            requestctx.deactivate(token)
