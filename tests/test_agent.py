import json

from nova.agent.core import Agent, StepRecord
from nova.llm.base import LLMResponse, Message, ToolCall, Usage
from nova.llm.mock import MockProvider
from nova.tools.base import ToolRegistry, tool


def resp_text(text):
    return LLMResponse(message=Message(role="assistant", content=text),
                       usage=Usage(10, 5), model="mock", finish_reason="stop")


def resp_tool_call(name, args_json):
    return LLMResponse(
        message=Message(role="assistant", content=None,
                        tool_calls=[ToolCall(id="tc1", name=name,
                                             arguments=json.loads(args_json))]),
        usage=Usage(20, 10), model="mock", finish_reason="tool_calls")


@tool(name="add", description="add two ints",
      parameters={"type": "object",
                  "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                  "required": ["a", "b"]})
async def add(a: int, b: int) -> str:
    return str(a + b)


async def test_agent_full_loop(tmp_path):
    mock = MockProvider()
    mock.enqueue(
        resp_tool_call("add", '{"a": 2, "b": 3}'),
        resp_text("The answer is 5."),
    )
    reg = ToolRegistry()
    reg.register(add)

    seen_steps = []
    agent = Agent(mock, reg, workspace=tmp_path,
                  trace_path=tmp_path / "trace.jsonl",
                  on_step=seen_steps.append)
    result = await agent.run("compute 2+3")

    assert result.final_answer == "The answer is 5."
    assert result.stopped_reason == "completed"
    kinds = [s.kind for s in result.steps]
    assert kinds == ["act", "final"]
    # tool observation fed back into history as a tool message
    roles = [m.role for m in agent.history]
    assert "tool" in roles
    # trace file written
    lines = (tmp_path / "trace.jsonl").read_text().strip().splitlines()
    events = [json.loads(l) for l in lines]
    assert any(e["kind"] == "act" and e["observation"] == "5" for e in events)


async def test_agent_reflection_on_error(tmp_path):
    @tool(name="boom", description="always fails",
          parameters={"type": "object", "properties": {}})
    async def boom() -> str:
        raise ValueError("kaput")

    mock = MockProvider()
    mock.enqueue(resp_tool_call("boom", "{}"), resp_text("recovered"))
    reg = ToolRegistry()
    reg.register(boom)

    agent = Agent(mock, reg, workspace=tmp_path)
    result = await agent.run("try the thing")

    assert result.final_answer == "recovered"
    # reflection prompt was injected into history after failure
    contents = [m.content for m in agent.history if m.role == "system"]
    assert any("failed" in c for c in contents)


async def test_agent_max_steps_guard(tmp_path):
    mock = MockProvider()
    for _ in range(10):
        mock.enqueue(resp_tool_call("add", '{"a": 0, "b": 0}'))
    reg = ToolRegistry()
    reg.register(add)

    agent = Agent(mock, reg, workspace=tmp_path, max_steps=3)
    result = await agent.run("loop forever")
    assert result.stopped_reason == "max_steps"
    assert result.steps_used == 3


async def test_agent_cost_guard(tmp_path):
    mock = MockProvider()
    mock.enqueue(resp_text("hi"))
    reg = ToolRegistry()
    agent = Agent(mock, reg, workspace=tmp_path, max_cost_usd=0.0000001)
    result = await agent.run("hello")
    assert result.stopped_reason == "max_cost"


async def test_agent_cooperative_stop_keeps_history_valid(tmp_path):
    """should_stop after a tool_call response must answer dangling calls."""
    mock = MockProvider()
    mock.enqueue(resp_tool_call("add", '{"a": 1, "b": 2}'))
    reg = ToolRegistry()
    reg.register(add)

    agent = Agent(mock, reg, workspace=tmp_path)
    result = await agent.run("compute", should_stop=lambda: True)

    assert result.stopped_reason == "user_stopped"
    # history stays API-valid: every assistant tool_call got a tool reply
    pending = 0
    for m in agent.history:
        if m.role == "assistant" and m.tool_calls:
            pending += len(m.tool_calls)
        elif m.role == "tool":
            pending -= 1
    assert pending == 0
    assert agent.history[-1].role == "tool"
    assert agent.history[-1].content == "(stopped by user)"


async def test_agent_cooperative_stop_between_steps(tmp_path):
    mock = MockProvider()
    mock.enqueue(
        resp_tool_call("add", '{"a": 1, "b": 1}'),
        resp_text("more"),
    )
    reg = ToolRegistry()
    reg.register(add)

    calls = {"n": 0}

    def stop_after_first_step():
        calls["n"] += 1
        return calls["n"] > 1   # False during first check, True after tools ran

    agent = Agent(mock, reg, workspace=tmp_path)
    result = await agent.run("loop", should_stop=stop_after_first_step)

    assert result.stopped_reason == "user_stopped"
    assert "停止" in result.final_answer
