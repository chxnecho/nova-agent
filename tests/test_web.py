import json

from fastapi.testclient import TestClient

from nova.config import Config
from nova.llm.base import LLMResponse, Message, Usage
from nova.llm.mock import MockProvider
from nova.web.server import create_app


def resp_text(text):
    return LLMResponse(message=Message(role="assistant", content=text),
                       usage=Usage(10, 5), model="mock", finish_reason="stop")


def make_app():
    cfg = Config({"llm": {"provider": "mock"}, "memory": {"enabled": False},
                  "tools": {"shell": {"enabled": False},
                            "python_repl": {"enabled": False},
                            "web": {"enabled": False}}})
    return create_app(cfg)


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

    events = []
    for line in r2.text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    kinds = [e["type"] for e in events]
    assert "final" in kinds
    final = [e for e in events if e["type"] == "final"][0]
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
