"""FastAPI + SSE web server for NovaAgent.

Chat protocol (robust against browser/proxy buffering):
- POST /api/chat/{sid}         -> start a run, returns {"run_id": ...}
- GET  /api/stream/{sid}/{rid} -> native EventSource feed (replayable + heartbeat)
- POST /api/stop/{sid}         -> cooperatively stop the active run

The stream is a pure *replay* of a run's event buffer: if the connection
drops, EventSource reconnects and receives every missed event again, so the
UI can never miss the final answer or get stuck in a busy state.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nova.agent.core import build_default_agent
from nova.config import Config, api_key_for, load_config
from nova.llm.provider import create_provider_from_config
from nova.log import setup_logging

STATIC_DIR = Path(__file__).resolve().parent / "static"


class NoCacheStaticFiles(StaticFiles):
    """Static files must never be heuristically cached: we iterate fast."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

HEARTBEAT_SECONDS = 15.0
SERVER_VERSION = "v9"   # surfaced via /api/sessions so stale processes are detectable


class ChatBody(BaseModel):
    message: str
    confirm_dangerous: bool = False   # ask the user before dangerous tools run


class ChatRun:
    """One agent execution whose events are buffered for (re)streaming."""

    def __init__(self):
        self.events: list[str] = []      # serialized SSE payloads
        self.done = False
        self.notify = asyncio.Event()
        self.stop_requested = False
        # pending approval gate (set by the approval_callback in worker)
        self.approval_event: asyncio.Event | None = None
        self.approved = False

    def emit(self, payload: dict) -> None:
        self.events.append(json.dumps(payload, ensure_ascii=False))
        self.notify.set()

    def mark_done(self) -> None:
        self.finished_at = time.monotonic()
        self.emit({"type": "done"})   # terminal event for clients
        self.done = True
        self.notify.set()


class Session:
    def __init__(self, cfg: Config, workspace: str | Path = "."):
        try:
            key = api_key_for(cfg)
        except RuntimeError:
            key = ""  # mock provider needs no key
        self.provider = create_provider_from_config(cfg, key)
        self.agent = build_default_agent(cfg, self.provider, workspace=workspace)
        self.agent.reset()
        self.running = False
        self.runs: dict[str, ChatRun] = {}
        self.active_run: ChatRun | None = None
        self.last_used = time.monotonic()

SESSION_IDLE_TTL = 3600.0      # discard idle sessions after 1 h
RUN_RETENTION = 600.0          # keep a finished run's event buffer for 10 min
MAX_SESSIONS = 200             # hard cap; oldest idle sessions are evicted


def create_app(cfg: Config | None = None, workspace: str | Path = ".") -> FastAPI:
    cfg = cfg or load_config()
    app = FastAPI(title="NovaAgent")

    # Optional bearer-token auth for every /api endpoint (enable by setting
    # NOVA_WEB_TOKEN or server.auth_token). The page and static assets stay
    # public so the browser can load the UI; it then prompts for the token.
    api_token = os.environ.get("NOVA_WEB_TOKEN") or cfg.get("server.auth_token") or ""

    async def check_auth(request: Request):
        if not api_token or not request.url.path.startswith("/api"):
            return
        if request.headers.get("authorization", "") != f"Bearer {api_token}":
            raise HTTPException(401, "unauthorized: set Authorization: Bearer <token>")

    app.state.sessions = {}                # exposed for tests/debugging
    sessions: dict[str, Session] = app.state.sessions
    app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        try:
            await check_auth(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return await call_next(request)

    # ---- coarse per-IP rate limit for the /api surface (in-process) ----
    # Cheap token bucket refreshes every 60s; guards against open abuse when a
    # token is unset / brute-force of the bearer token when it is set.
    RATE_LIMIT_PER_MIN = int(cfg.get("server.rate_limit_per_minute", 300))
    app.state.rate_buckets: dict[str, list] = {}

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        if request.url.path.startswith("/api"):
            ip = request.client.host if request.client else "unknown"
            buckets = app.state.rate_buckets
            now = time.monotonic()
            bucket = buckets.get(ip)
            if bucket is None or now - bucket[1] >= 60:
                bucket = [float(RATE_LIMIT_PER_MIN), now]
                buckets[ip] = bucket
            if bucket[0] <= 0:
                return JSONResponse({"detail": "rate limit exceeded"},
                                    status_code=429)
            bucket[0] -= 1
            if len(buckets) > 4096:          # bound memory
                buckets.pop(next(iter(buckets)), None)
        return await call_next(request)

    # ---- basic security headers (defense-in-depth for the client) ----
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; "
            "base-uri 'self'; form-action 'self'",
        )
        return resp

    @app.on_event("startup")
    async def janitor() -> None:
        """Periodically reap expired sessions/runs so long-running servers
        don't leak memory."""
        async def _loop():
            while True:
                await asyncio.sleep(60)
                now = time.monotonic()
                for sid in list(sessions):
                    s = sessions[sid]
                    for rid in list(s.runs):
                        r = s.runs[rid]
                        if r.done and now - getattr(r, "finished_at", now) > RUN_RETENTION:
                            del s.runs[rid]
                            if s.active_run is r:
                                s.active_run = None
                    if (not s.running and s.active_run is None
                            and now - s.last_used > SESSION_IDLE_TTL):
                        sessions.pop(sid, None)
                        await s.provider.aclose()
                while len(sessions) > MAX_SESSIONS:
                    oldest = min(sessions, key=lambda k: sessions[k].last_used)
                    s = sessions.pop(oldest)
                    await s.provider.aclose()
        asyncio.create_task(_loop())

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.post("/api/sessions")
    async def new_session():
        sid = uuid.uuid4().hex[:12]
        sessions[sid] = Session(cfg, workspace=workspace)
        return {"session_id": sid,
                "model": getattr(sessions[sid].provider, "model", "mock"),
                "server_version": SERVER_VERSION}

    @app.post("/api/chat/{sid}")
    async def chat(sid: str, body: ChatBody):
        session = sessions.get(sid)
        if session is None:
            raise HTTPException(404, "unknown session")
        if session.running:
            raise HTTPException(409, "a task is already running in this session")
        session.last_used = time.monotonic()

        run_id = uuid.uuid4().hex[:12]
        run = ChatRun()
        session.runs[run_id] = run
        session.active_run = run
        session.running = True

        async def worker() -> None:
            agent = session.agent
            run_started_at = time.monotonic()
            try:
                agent.stream_callback = lambda d: run.emit(
                    {"type": "delta", "text": d})
                agent.reasoning_callback = lambda d: run.emit(
                    {"type": "reasoning", "text": d})
                if body.confirm_dangerous:
                    async def approve(tool_name: str, args: dict) -> bool:
                        run.approved = False
                        run.approval_event = asyncio.Event()
                        run.emit({"type": "approval_request",
                                  "tool": tool_name, "args": args})
                        await run.approval_event.wait()
                        return run.approved
                    agent.approval_callback = approve
                else:
                    agent.approval_callback = None

                def on_step(step) -> None:
                    run.emit({
                        "type": "step", "kind": step.kind, "step": step.step,
                        "content": (step.content or "")[:2500],
                        "tool": step.tool_name, "args": step.tool_args,
                        "observation": (step.observation or "")[:1800],
                    })
                agent.on_step = on_step

                result = await agent.run(body.message,
                                         should_stop=lambda: run.stop_requested)
                run.emit({
                    "type": "final",
                    "text": result.final_answer,
                    "steps": result.steps_used,
                    "tokens": result.prompt_tokens + result.completion_tokens,
                    "reason": result.stopped_reason,
                    "cost_usd": round(result.cost_usd, 4),
                    "duration_s": round(time.monotonic() - run_started_at, 1),
                })
            except Exception as exc:
                run.emit({"type": "error", "message": str(exc)})
            finally:
                session.running = False
                if session.active_run is run:
                    session.active_run = None
                run.mark_done()

        run.task = asyncio.create_task(worker())
        return {"run_id": run_id}

    @app.get("/api/stream/{sid}/{rid}")
    async def stream(sid: str, rid: str):
        session = sessions.get(sid)
        if session is None or rid not in session.runs:
            raise HTTPException(404, "unknown session or run")
        run = session.runs[rid]

        async def event_stream():
            # padding comment: flushes intermediate proxies/buffers immediately
            yield ":" + " " * 2048 + "\n\n"
            sent = 0
            while True:
                while sent < len(run.events):
                    yield f"data: {run.events[sent]}\n\n"
                    sent += 1
                    if run.done and sent >= len(run.events):
                        return
                if run.done:
                    return
                try:
                    await asyncio.wait_for(run.notify.wait(),
                                           timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": hb\n\n"          # keep intermediaries from timing out
                run.notify.clear()

        return StreamingResponse(
            event_stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"})

    @app.get("/api/events/{sid}/{rid}")
    async def events(sid: str, rid: str, after: int = 0):
        """Plain JSON polling endpoint — immune to any proxy/browser buffering."""
        session = sessions.get(sid)
        if session is None or rid not in session.runs:
            raise HTTPException(404, "unknown session or run")
        run = session.runs[rid]
        batch = [json.loads(e) for e in run.events[max(after, 0):]]
        return {"events": batch, "done": run.done}

    @app.post("/api/stop/{sid}")
    async def stop(sid: str):
        """Cooperatively stop the active run of a session."""
        session = sessions.get(sid)
        if session is None:
            raise HTTPException(404, "unknown session")
        if session.active_run is None:
            return {"ok": True, "note": "nothing running"}
        session.active_run.stop_requested = True
        return {"ok": True}

    @app.post("/api/approve/{sid}/{rid}")
    async def approve(sid: str, rid: str, body: dict):
        """Answer a pending dangerous-tool approval request."""
        session = sessions.get(sid)
        if session is None or rid not in session.runs:
            raise HTTPException(404, "unknown session or run")
        run = session.runs[rid]
        if run.approval_event is None or run.approval_event.is_set():
            raise HTTPException(409, "no pending approval")
        run.approved = bool(body.get("approved", False))
        run.approval_event.set()
        return {"ok": True}

    @app.delete("/api/sessions/{sid}")
    async def delete_session(sid: str):
        """Discard a session server-side (client keeps its own local copy)."""
        session = sessions.pop(sid, None)
        if session is None:
            raise HTTPException(404, "unknown session")
        if session.active_run is not None:
            session.active_run.stop_requested = True   # stop work before discarding
        return {"ok": True}

    @app.get("/api/stats/{sid}")
    async def stats(sid: str):
        session = sessions.get(sid)
        if session is None:
            raise HTTPException(404, "unknown session")
        u = session.provider.total_usage
        model = getattr(session.provider, "model", "mock")
        return {"model": model, "prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens,
                "total_tokens": u.total_tokens}

    @app.get("/api/history/{sid}")
    async def history(sid: str):
        """Replayable conversation history (user/assistant turns only)."""
        session = sessions.get(sid)
        if session is None:
            raise HTTPException(404, "unknown session")
        out = []
        for m in session.agent.history:
            if m.role == "user":
                out.append({"role": "user", "content": m.content or ""})
            elif m.role == "assistant" and m.content:
                out.append({"role": "assistant", "content": m.content})
        return {"messages": out}

    return app


def main() -> None:
    """Entry point: python -m nova.web"""
    import uvicorn

    cfg = load_config()
    setup_logging(cfg.get("logging.level", "INFO"), cfg.get("logging.dir"))
    uvicorn.run(
        create_app(cfg),
        host=str(cfg.get("server.host", "127.0.0.1")),
        port=int(cfg.get("server.port", 8321)),
        log_level="info",
    )


if __name__ == "__main__":
    main()
