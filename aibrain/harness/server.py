"""HTTP API for the harness (the web UI talks to this). Local by default: binds to 127.0.0.1."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import options
from .runtime import Runtime


class NewThread(BaseModel):
    recipe: str = "standard"
    options: dict | None = None


class NewMessage(BaseModel):
    text: str


class Decision(BaseModel):
    approved: bool


SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def default_web_dir() -> Path | None:
    env = os.environ.get("BRAIN_WEB_DIR")
    candidates = [Path(env)] if env else [Path(__file__).resolve().parents[2] / "web" / "out"]
    return next((p for p in candidates if (p / "index.html").is_file()), None)


def create_app(runtime: Runtime, token: str | None = None, web_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="AI Brain harness", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    def auth(request: Request) -> None:
        if token is None:
            return
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(401, "missing or invalid token")

    api = [Depends(auth)]

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/recipes", dependencies=api)
    def recipes():
        return [{**r.summary(), "defaults": options.defaults(r, runtime.settings)} for r in runtime.recipes.values()]

    @app.get("/api/providers", dependencies=api)
    def providers():
        chat = runtime.settings.agents.get("chat") or runtime.settings.agents.get("default") or {}
        return [options.provider_info(p, chat.get("model") if chat.get("provider") == p.name else None)
                for p in runtime.settings.providers.values()]

    @app.get("/api/threads", dependencies=api)
    def threads():
        return runtime.store.threads()

    @app.post("/api/threads", dependencies=api)
    def new_thread(body: NewThread):
        try:
            t = runtime.new_thread(body.recipe, body.options)
        except KeyError as e:
            raise HTTPException(404, str(e))
        except options.OptionsError as e:
            raise HTTPException(400, str(e))
        return {**{k: v for k, v in t.items() if k != "state"}, "options": runtime.thread_options(t)}

    @app.get("/api/threads/{tid}", dependencies=api)
    def thread(tid: str):
        t = runtime.store.thread(tid)
        if t is None:
            raise HTTPException(404, "unknown thread")
        active = runtime._active.get(tid)
        try:
            opts = runtime.thread_options(t)
        except KeyError:  # the thread's recipe was deleted
            opts = None
        return {**{k: v for k, v in t.items() if k != "state"}, "items": runtime.store.items(tid),
                "active_run": active, "options": opts}

    @app.patch("/api/threads/{tid}", dependencies=api)
    def update_thread(tid: str, body: dict):
        try:
            return {"options": runtime.set_options(tid, body.get("options") or {})}
        except KeyError as e:
            raise HTTPException(404, str(e))
        except options.OptionsError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/threads/{tid}", dependencies=api)
    def delete_thread(tid: str):
        if tid in runtime._active:
            raise HTTPException(409, "conversation is still answering")
        runtime.store.delete_thread(tid)
        return {"ok": True}

    @app.post("/api/threads/{tid}/messages", dependencies=api)
    def send(tid: str, body: NewMessage):
        if not body.text.strip():
            raise HTTPException(400, "empty message")
        try:
            run = runtime.start(tid, body.text)
        except KeyError as e:
            raise HTTPException(404, str(e))
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"run_id": run.id}

    @app.get("/api/runs/{rid}/events", dependencies=api)
    async def events(rid: str, request: Request, after: int = 0):
        run = runtime.runs.get(rid)
        if run is None:
            raise HTTPException(404, "unknown run")
        if request.headers.get("last-event-id", "").isdigit():
            after = int(request.headers["last-event-id"]) + 1

        async def stream():
            nonlocal after
            while not await request.is_disconnected():
                batch = await asyncio.to_thread(run.wait, after, 15.0)
                if not batch:
                    if run.done:
                        return
                    yield ": keep-alive\n\n"
                    continue
                for e in batch:
                    yield f"id: {e['seq']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
                    after = e["seq"] + 1
                if run.done and after >= len(run.events):
                    return

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/{rid}/cancel", dependencies=api)
    def cancel(rid: str):
        return {"cancelled": runtime.cancel(rid)}

    @app.get("/api/approvals", dependencies=api)
    def approvals():
        return runtime.broker.pending()

    @app.post("/api/approvals/{aid}", dependencies=api)
    def decide(aid: str, body: Decision):
        if not runtime.broker.resolve(aid, body.approved):
            raise HTTPException(404, "no such pending approval")
        return {"ok": True}

    if web_dir is not None:  # the web UI: a static export, served last so /api wins
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


def harness_token() -> str | None:
    return os.environ.get("BRAIN_HARNESS_TOKEN") or os.environ.get("BRAIN_TOKEN") or None


def serve(runtime: Runtime, host: str = "127.0.0.1", port: int = 8780) -> None:
    import uvicorn

    token = harness_token()
    if token is None and host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("refusing to serve the harness without a token: set BRAIN_TOKEN "
                         "(in Docker: in .env, or start with ./docker/up.sh to load it from KeePassXC)")
    web = default_web_dir()
    print(f"harness{' and web UI' if web else ''} on http://{host}:{port}", file=sys.stderr, flush=True)
    uvicorn.run(create_app(runtime, token, web), host=host, port=port, log_level="warning",
                server_header=False)
