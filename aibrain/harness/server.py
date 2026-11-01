"""HTTP API for the harness (the web UI talks to this). Local by default: binds to 127.0.0.1."""

from __future__ import annotations

import asyncio
import hmac
import json
import os

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .runtime import Runtime


class NewThread(BaseModel):
    recipe: str = "standard"


class NewMessage(BaseModel):
    text: str


class Decision(BaseModel):
    approved: bool


def create_app(runtime: Runtime, token: str | None = None) -> FastAPI:
    app = FastAPI(title="AI Brain harness", docs_url=None, redoc_url=None)

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
        return [r.summary() for r in runtime.recipes.values()]

    @app.get("/api/threads", dependencies=api)
    def threads():
        return runtime.store.threads()

    @app.post("/api/threads", dependencies=api)
    def new_thread(body: NewThread):
        try:
            t = runtime.new_thread(body.recipe)
        except KeyError as e:
            raise HTTPException(404, str(e))
        return {k: v for k, v in t.items() if k != "state"}

    @app.get("/api/threads/{tid}", dependencies=api)
    def thread(tid: str):
        t = runtime.store.thread(tid)
        if t is None:
            raise HTTPException(404, "unknown thread")
        active = runtime._active.get(tid)
        return {**{k: v for k, v in t.items() if k != "state"}, "items": runtime.store.items(tid),
                "active_run": active}

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

    return app


def serve(runtime: Runtime, host: str = "127.0.0.1", port: int = 8780) -> None:
    import uvicorn

    token = os.environ.get("BRAIN_HARNESS_TOKEN") or None
    if token is None and host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("refusing to serve the harness on a non-loopback address without BRAIN_HARNESS_TOKEN")
    uvicorn.run(create_app(runtime, token), host=host, port=port, log_level="warning")
