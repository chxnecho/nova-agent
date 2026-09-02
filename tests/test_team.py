from nova.agent.team import AgentTeam, parse_json_array, parse_json_object
from nova.config import Config
from nova.llm.base import LLMResponse, Message, Usage
from nova.llm.mock import MockProvider


def resp_text(text):
    return LLMResponse(
        message=Message(role="assistant", content=text),
        usage=Usage(10, 5),
        model="mock",
        finish_reason="stop",
    )


def test_parse_json_helpers():
    arr = parse_json_array('junk ```json\n[{"title":"a","detail":"b"}]\n``` tail')
    assert arr == [{"title": "a", "detail": "b"}]
    assert parse_json_array("no json here") == []
    obj = parse_json_object('{"verdict": "APPROVED", "reasoning": "ok"}')
    assert obj["verdict"] == "APPROVED"
    assert parse_json_object("nothing") == {}


async def test_team_full_pipeline(tmp_path):
    cfg = Config({"memory": {"enabled": False}, "agent": {"max_steps": 5}})
    mock = MockProvider()
    mock.enqueue(
        # planner: two subtasks
        resp_text(
            '[{"title": "write file", "detail": "create a.txt"},'
            ' {"title": "verify", "detail": "check a.txt"}]'
        ),
        # executor 1: direct answer (no tools)
        resp_text("created a.txt with content hello"),
        # executor 2
        resp_text("verified a.txt exists"),
        # critic approves
        resp_text('{"verdict": "APPROVED", "reasoning": "all good"}'),
        # synthesis
        resp_text("FINAL REPORT: task done"),
    )

    events = []
    team = AgentTeam(cfg, mock, workspace=tmp_path, on_event=lambda t, p: events.append(t))
    result = await team.run("write and verify a file")

    assert result.final_answer.startswith("FINAL REPORT")
    assert result.rounds == 1
    assert len(result.subtask_results) == 2
    assert result.critique["verdict"] == "APPROVED"
    for expected in (
        "plan_start",
        "plan_ready",
        "executor_start",
        "executor_done",
        "critique",
        "done",
    ):
        assert expected in events


async def test_team_revision_round(tmp_path):
    cfg = Config({"memory": {"enabled": False}, "agent": {"max_steps": 5}})
    mock = MockProvider()
    mock.enqueue(
        resp_text('[{"title": "t1", "detail": "d1"}]'),  # plan v1
        resp_text("bad first attempt"),  # executor r1
        resp_text(
            '{"verdict": "NEEDS_REVISION", "reasoning": "wrong", "feedback": "do it properly"}'
        ),  # critic rejects
        resp_text('[{"title": "t2", "detail": "d2 better"}]'),  # re-plan
        resp_text("good second attempt"),  # executor r2
        resp_text('{"verdict": "APPROVED", "reasoning": "fixed"}'),  # critic approves
        resp_text("FINAL REVISED REPORT"),  # synthesis
    )
    team = AgentTeam(cfg, mock, workspace=tmp_path)
    result = await team.run("tricky task")

    assert result.rounds == 2
    assert result.final_answer.startswith("FINAL REVISED")
    assert result.plan[0]["title"] == "t2"


async def test_team_planner_failure_fallback(tmp_path):
    cfg = Config({"memory": {"enabled": False}, "agent": {"max_steps": 5}})
    mock = MockProvider()
    mock.enqueue(
        resp_text("I cannot produce JSON."),  # planner (unparseable)
        resp_text("did the whole task myself"),  # executor
        resp_text('{"verdict": "APPROVED"}'),  # critic
        resp_text("REPORT"),  # synthesis
    )
    team = AgentTeam(cfg, mock, workspace=tmp_path)
    result = await team.run("simple task")
    assert result.plan[0]["detail"] == "simple task"  # fallback single subtask
    assert len(result.subtask_results) == 1
