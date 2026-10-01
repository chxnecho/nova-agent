import asyncio
import json
from contextlib import asynccontextmanager

import httpx
from nova.config import Config
from nova.llm.base import LLMResponse, Message, Usage
from nova.web.server import create_app


def resp_text(text):
    return LLMResponse(
        message=Message(role="assistant", content=text),
        usage=Usage(10, 5),
        model="mock",
        finish_reason="stop",
    )


def make_app():
    cfg = Config(
        {
            "llm": {"provider": "mock"},
            "memory": {"enabled": False},
            "tools": {
                "shell": {"enabled": False},
                "python_repl": {"enabled": False},
                "web": {"enabled": False},
            },
        }
    )
    return create_app(cfg)


@asynccontextmanager
async def client_for(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@asynccontextmanager
async def app_client(app):
    async with app.router.lifespan_context(app), client_for(app) as client:
        yield client


async def wait_until(predicate, attempts=50, delay=0.02):
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(delay)
    return predicate()


def sessions_of(app):
    return app.state.sessions


async def test_auth_required_when_token_set(monkeypatch):
    """With NOVA_WEB_TOKEN set, /api endpoints demand the bearer token,
    while the page and static assets stay publicly loadable."""
    monkeypatch.setenv("NOVA_WEB_TOKEN", "s3cret")
    app = make_app()
    async with app_client(app) as client:
        assert (await client.get("/api/history/nope")).status_code == 401
        assert (
            await client.get("/api/history/nope", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        # page itself is still public so the browser can render the UI
        assert (await client.get("/")).status_code == 200
        # correct token passes through to normal handling (404: unknown session)
        response = await client.get("/api/history/nope", headers={"Authorization": "Bearer s3cret"})
        assert response.status_code == 404


async def test_no_auth_when_token_unset(monkeypatch):
    monkeypatch.delenv("NOVA_WEB_TOKEN", raising=False)
    app = make_app()
    async with app_client(app) as client:
        response = await client.get("/api/history/nope")
        assert response.status_code == 404  # reaches handler: unknown session, not 401


async def test_index_page_served():
    app = create_app(Config({"memory": {"enabled": False}}))
    async with app_client(app) as client:
        response = await client.get("/")
        assert response.status_code == 200
        assert "NovaAgent" in response.text


async def test_chat_stream_end_to_end():
    app = make_app()
    async with app_client(app) as client:
        sid = (await client.post("/api/sessions")).json()["session_id"]

        # step 1: start the run
        response = await client.post(f"/api/chat/{sid}", json={"message": "hello agent"})
        assert response.status_code == 200
        run_id = response.json()["run_id"]

        # give the background task a moment, then replay the event buffer
        assert await wait_until(lambda: sessions_of(app)[sid].runs[run_id].done)

        stream_response = await client.get(f"/api/stream/{sid}/{run_id}")
        assert stream_response.status_code == 200
        assert stream_response.headers["content-type"].startswith("text/event-stream")

        events = [
            json.loads(line[6:])
            for line in stream_response.text.splitlines()
            if line.startswith("data: ")
        ]
        kinds = [event["type"] for event in events]
        assert "final" in kinds
        final = next(event for event in events if event["type"] == "final")
        assert "[mock] You said: hello agent" in final["text"]
        assert kinds[-1] == "done"


async def test_stream_replay_is_stable():
    """The same run can be streamed repeatedly (reconnect/replay support)."""
    app = make_app()
    async with app_client(app) as client:
        sid = (await client.post("/api/sessions")).json()["session_id"]
        run_id = (await client.post(f"/api/chat/{sid}", json={"message": "hi"})).json()["run_id"]
        assert await wait_until(lambda: sessions_of(app)[sid].runs[run_id].done)

        first = (await client.get(f"/api/stream/{sid}/{run_id}")).text
        second = (await client.get(f"/api/stream/{sid}/{run_id}")).text
        assert first == second and "final" in first


async def test_unknown_session_404():
    app = make_app()
    async with app_client(app) as client:
        response = await client.post("/api/chat/nonexistent", json={"message": "hi"})
        assert response.status_code == 404


async def test_stats_endpoint():
    app = make_app()
    async with app_client(app) as client:
        sid = (await client.post("/api/sessions")).json()["session_id"]
        stats = (await client.get(f"/api/stats/{sid}")).json()
        assert stats["model"] == "mock-model"
        assert stats["total_tokens"] == 0


async def test_stop_endpoint():
    app = make_app()
    async with app_client(app) as client:
        sid = (await client.post("/api/sessions")).json()["session_id"]
        response = await client.post(f"/api/stop/{sid}")
        assert response.status_code == 200
        assert response.json()["ok"] is True
        response = await client.post("/api/stop/nonexistent")
        assert response.status_code == 404


async def test_events_polling_endpoint():
    """Polling endpoint returns incremental events + done flag."""
    app = make_app()
    async with app_client(app) as client:
        sid = (await client.post("/api/sessions")).json()["session_id"]
        run_id = (await client.post(f"/api/chat/{sid}", json={"message": "poll me"})).json()[
            "run_id"
        ]
        assert await wait_until(lambda: sessions_of(app)[sid].runs[run_id].done)

        events = (await client.get(f"/api/events/{sid}/{run_id}?after=0")).json()
        types = [event["type"] for event in events["events"]]
        assert "final" in types and events["done"] is True

        # after=1 skips the first event but still returns the rest
        remaining = (await client.get(f"/api/events/{sid}/{run_id}?after=1")).json()
        assert len(remaining["events"]) == len(events["events"]) - 1

        response = await client.get("/api/events/nonexistent/x")
        assert response.status_code == 404


async def test_rate_limit_disabled_when_zero():
    """rate_limit_per_minute:0 semantics: disabled (not 'deny everything')."""
    app = create_app(
        Config(
            {
                "llm": {"provider": "mock"},
                "memory": {"enabled": False},
                "tools": {
                    "shell": {"enabled": False},
                    "python_repl": {"enabled": False},
                    "web": {"enabled": False},
                },
                "server": {"rate_limit_per_minute": 0},
            }
        )
    )
    async with app_client(app) as client:
        # all requests pass through even though the limit is set to "0"
        for _ in range(10):
            response = await client.get("/api/history/nope")
            assert response.status_code == 404  # reached handler; not a 429


async def test_approval_timeout_does_not_wedge_the_run():
    """A dangerous tool whose approval is never answered must not wedge the
    run forever: the gate times out (treated as denied) and the run completes."""
    from nova.llm.base import ToolCall
    from nova.tools.base import tool

    @tool(
        name="nuke",
        description="dangerous test tool",
        parameters={"type": "object", "properties": {}},
        danger_level="dangerous",
    )
    async def nuke(**_kw) -> str:
        return "boom"

    app = create_app(
        Config(
            {
                "llm": {"provider": "mock"},
                "memory": {"enabled": False},
                "tools": {
                    "shell": {"enabled": False},
                    "python_repl": {"enabled": False},
                    "web": {"enabled": False},
                },
                "server": {"approval_timeout_seconds": 0.2},
            }
        )
    )
    async with app_client(app) as client:
        sid = (await client.post("/api/sessions")).json()["session_id"]
        session = app.state.sessions[sid]
        session.agent.registry.register(nuke)
        session.provider.enqueue(
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCall(id="tc1", name="nuke", arguments={})],
                ),
                usage=Usage(5, 5),
                model="mock",
                finish_reason="tool_calls",
            )
        )

        run_id = (
            await client.post(f"/api/chat/{sid}", json={"message": "go", "confirm_dangerous": True})
        ).json()["run_id"]
        assert await wait_until(lambda: app.state.sessions[sid].runs[run_id].done, 80, 0.05)

        run = app.state.sessions[sid].runs[run_id]
        kinds = [json.loads(event)["type"] for event in run.events]
        assert "approval_request" in kinds
        assert "approval_timeout" in kinds
        assert kinds[-1] == "done"


async def test_stop_releases_pending_approval():
    """/api/stop must unblock a run that is waiting on approval, instead of
    leaving the worker task hanging on an event nobody will ever set."""
    from nova.llm.base import ToolCall
    from nova.tools.base import tool

    @tool(
        name="nuke",
        description="dangerous test tool",
        parameters={"type": "object", "properties": {}},
        danger_level="dangerous",
    )
    async def nuke(**_kw) -> str:
        return "boom"

    app = create_app(
        Config(
            {
                "llm": {"provider": "mock"},
                "memory": {"enabled": False},
                "tools": {
                    "shell": {"enabled": False},
                    "python_repl": {"enabled": False},
                    "web": {"enabled": False},
                },
                "server": {"approval_timeout_seconds": 60},
            }
        )
    )
    async with app_client(app) as client:
        sid = (await client.post("/api/sessions")).json()["session_id"]
        session = app.state.sessions[sid]
        session.agent.registry.register(nuke)
        session.provider.enqueue(
            LLMResponse(
                message=Message(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCall(id="tc1", name="nuke", arguments={})],
                ),
                usage=Usage(5, 5),
                model="mock",
                finish_reason="tool_calls",
            )
        )

        run_id = (
            await client.post(f"/api/chat/{sid}", json={"message": "go", "confirm_dangerous": True})
        ).json()["run_id"]
        assert await wait_until(
            lambda: session.runs.get(run_id) and session.runs[run_id].approval_event is not None,
            80,
        )
        await client.post(f"/api/stop/{sid}")
        assert await wait_until(lambda: session.runs[run_id].done, 100)

        final = [json.loads(event) for event in session.runs[run_id].events][-2]
        assert final["type"] == "final" and final["reason"] == "user_stopped"


async def test_host_guard_blocks_rebinding_domain():
    """DNS-rebinding defense: a public *domain* Host header is rejected while
    loopback/private IPs, localhost and single-label names pass through."""
    app = make_app()
    async with app_client(app) as client:
        # rebinding-style request: domain Host header -> 403 on API and page alike
        assert (
            await client.get("/api/history/nope", headers={"Host": "evil.com"})
        ).status_code == 403
        assert (await client.get("/", headers={"Host": "evil.com"})).status_code == 403
        # legitimate local access patterns pass
        assert (await client.get("/", headers={"Host": "localhost:8321"})).status_code == 200
        assert (await client.get("/", headers={"Host": "127.0.0.1:8321"})).status_code == 200
        assert (await client.get("/", headers={"Host": "192.168.1.5:8321"})).status_code == 200
        # default ASGI test host (single label) keeps working
        assert (await client.get("/")).status_code == 200


async def test_host_guard_allows_configured_domain():
    app = create_app(
        Config(
            {
                "llm": {"provider": "mock"},
                "memory": {"enabled": False},
                "tools": {
                    "shell": {"enabled": False},
                    "python_repl": {"enabled": False},
                    "web": {"enabled": False},
                },
                "server": {"allowed_hosts": ["nova.example.com"]},
            }
        )
    )
    async with app_client(app) as client:
        response = await client.get("/api/history/nope", headers={"Host": "nova.example.com"})
        assert response.status_code == 404  # passed the guard; unknown session
        response = await client.get("/api/history/nope", headers={"Host": "other.com"})
        assert response.status_code == 403


async def test_lifespan_startup_shutdown_hygiene():
    """On lifespan entry the janitor starts; on exit it is cancelled and every
    session's provider is closed (no open clients are left behind)."""
    app = make_app()
    sid = None
    async with app.router.lifespan_context(app):
        assert not app.state.janitor_task.done()
        async with client_for(app) as client:
            sid = (await client.post("/api/sessions")).json()["session_id"]
            assert len(app.state.sessions) == 1
    # shutdown ran: sessions drained, provider closed, janitor cancelled
    assert len(app.state.sessions) == 0
    assert app.state.janitor_task.done()
    assert sid is not None


async def test_delete_session_endpoint():
    app = make_app()
    async with app_client(app) as client:
        sid = (await client.post("/api/sessions")).json()["session_id"]

        response = await client.delete(f"/api/sessions/{sid}")
        assert response.status_code == 200
        assert response.json()["ok"] is True

        # session gone: chat now 404s, delete again also 404s
        assert (await client.post(f"/api/chat/{sid}", json={"message": "x"})).status_code == 404
        assert (await client.delete(f"/api/sessions/{sid}")).status_code == 404
