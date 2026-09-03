import json

from fastapi.testclient import TestClient
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


def test_auth_required_when_token_set(monkeypatch):
    """With NOVA_WEB_TOKEN set, /api endpoints demand the bearer token,
    while the page and static assets stay publicly loadable."""
    monkeypatch.setenv("NOVA_WEB_TOKEN", "s3cret")
    app = make_app()
    client = TestClient(app)

    assert client.get("/api/history/nope").status_code == 401
    assert (
        client.get("/api/history/nope", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    # page itself is still public so the browser can render the UI
    assert client.get("/").status_code == 200
    # correct token passes through to normal handling (404: unknown session)
    r = client.get("/api/history/nope", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 404


def test_no_auth_when_token_unset(monkeypatch):
    monkeypatch.delenv("NOVA_WEB_TOKEN", raising=False)
    client = TestClient(make_app())
    r = client.get("/api/history/nope")
    assert r.status_code == 404  # reaches handler: unknown session, not 401


def test_index_page_served():
    client = TestClient(create_app(Config({"memory": {"enabled": False}})))
    r = client.get("/")
    assert r.status_code == 200
    assert "NovaAgent" in r.text


def test_chat_stream_end_to_end():
    app = make_app()
    client = TestClient(app)

    sid = client.post("/api/sessions").json()["session_id"]

    # step 1: start the run
    r = client.post(f"/api/chat/{sid}", json={"message": "hello agent"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]

    # give the background task a moment, then replay the event buffer
    import time

    for _ in range(50):
        if sessions_of(client)[sid].runs[run_id].done:
            break
        time.sleep(0.02)

    r2 = client.get(f"/api/stream/{sid}/{run_id}")
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("text/event-stream")

    events = [json.loads(line[6:]) for line in r2.text.splitlines() if line.startswith("data: ")]
    kinds = [e["type"] for e in events]
    assert "final" in kinds
    final = next(e for e in events if e["type"] == "final")
    assert "[mock] You said: hello agent" in final["text"]
    assert kinds[-1] == "done"


def sessions_of(_client):
    """Reach into the app's session store via the TestClient's app reference."""
    return _client.app.state.sessions


def test_stream_replay_is_stable():
    """The same run can be streamed repeatedly (reconnect/replay support)."""
    app = make_app()
    client = TestClient(app)
    import time

    sid = client.post("/api/sessions").json()["session_id"]
    rid = client.post(f"/api/chat/{sid}", json={"message": "hi"}).json()["run_id"]
    for _ in range(50):
        if sessions_of(client)[sid].runs[rid].done:
            break
        time.sleep(0.02)

    t1 = client.get(f"/api/stream/{sid}/{rid}").text
    t2 = client.get(f"/api/stream/{sid}/{rid}").text
    assert t1 == t2 and "final" in t1


def test_unknown_session_404():
    client = TestClient(make_app())
    r = client.post("/api/chat/nonexistent", json={"message": "hi"})
    assert r.status_code == 404


def test_stats_endpoint():
    client = TestClient(make_app())
    sid = client.post("/api/sessions").json()["session_id"]
    stats = client.get(f"/api/stats/{sid}").json()
    assert stats["model"] == "mock-model"
    assert stats["total_tokens"] == 0


def test_stop_endpoint():
    client = TestClient(make_app())
    sid = client.post("/api/sessions").json()["session_id"]
    r = client.post(f"/api/stop/{sid}")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    r = client.post("/api/stop/nonexistent")
    assert r.status_code == 404


def test_events_polling_endpoint():
    """Polling endpoint returns incremental events + done flag."""
    app = make_app()
    client = TestClient(app)
    import time

    sid = client.post("/api/sessions").json()["session_id"]
    rid = client.post(f"/api/chat/{sid}", json={"message": "poll me"}).json()["run_id"]
    for _ in range(50):
        if client.app.state.sessions[sid].runs[rid].done:
            break
        time.sleep(0.02)

    j = client.get(f"/api/events/{sid}/{rid}?after=0").json()
    types = [e["type"] for e in j["events"]]
    assert "final" in types and j["done"] is True

    # after=1 skips the first event but still returns the rest
    j2 = client.get(f"/api/events/{sid}/{rid}?after=1").json()
    assert len(j2["events"]) == len(j["events"]) - 1

    r = client.get("/api/events/nonexistent/x")
    assert r.status_code == 404


def test_rate_limit_disabled_when_zero():
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
    with TestClient(app) as client:
        # all requests pass through even though the limit is set to "0"
        for _ in range(10):
            r = client.get("/api/history/nope")
            assert r.status_code == 404  # reached handler; not a 429


def test_lifespan_startup_shutdown_hygiene():
    """On TestClient context entry the janitor starts; on exit it is cancelled
    and every session's provider is closed (no open clients are left behind)."""
    app = make_app()
    client = TestClient(app)
    sid = None
    with client:
        assert not app.state.janitor_task.done()
        sid = client.post("/api/sessions").json()["session_id"]
        assert len(app.state.sessions) == 1
    # shutdown ran: sessions drained, provider closed, janitor cancelled
    assert len(app.state.sessions) == 0
    assert app.state.janitor_task.done()
    assert sid is not None


def test_delete_session_endpoint():
    app = make_app()
    client = TestClient(app)
    sid = client.post("/api/sessions").json()["session_id"]

    r = client.delete(f"/api/sessions/{sid}")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # session gone: chat now 404s, delete again also 404s
    assert client.post(f"/api/chat/{sid}", json={"message": "x"}).status_code == 404
    assert client.delete(f"/api/sessions/{sid}").status_code == 404
