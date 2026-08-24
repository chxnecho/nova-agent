"""The autonomous agent loop: think -> act -> observe, with reflection and budget guards."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from nova.llm.base import LLMResponse, Message
from nova.log import JsonlTraceWriter, get_logger
from nova.tools.base import ToolRegistry

from .prompts import DEFAULT_SYSTEM_PROMPT, REFLECTION_PROMPT

log = get_logger("agent")

# rough USD estimates per 1M tokens (model-agnostic safety net for budget guard)
DEFAULT_COST_PER_MTOK = (0.3, 1.2)


@dataclass
class StepRecord:
    step: int
    kind: str                      # "think" | "act" | "final"
    content: str = ""
    tool_name: str | None = None
    tool_args: dict | None = None
    observation: str | None = None
    duration_s: float = 0.0


@dataclass
class RunResult:
    final_answer: str
    steps: list[StepRecord] = field(default_factory=list)
    steps_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    stopped_reason: str = "completed"   # completed | max_steps | max_cost


StepCallback = Callable[[StepRecord], None]


class Agent:
    """A single-conversation agent bound to one provider + tool registry."""

    def __init__(
        self,
        provider,
        registry: ToolRegistry,
        *,
        workspace: str | Path = ".",
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_steps: int = 40,
        max_cost_usd: float = 2.0,
        reflect_on_error: bool = True,
        trace_path: Path | None = None,
        on_step: StepCallback | None = None,
        stream_callback: Callable[[str], None] | None = None,
        reasoning_callback: Callable[[str], None] | None = None,
        cost_per_mtok: tuple[float, float] = DEFAULT_COST_PER_MTOK,
        system_extra: str | None = None,
    ):
        self.provider = provider
        self.registry = registry
        self.workspace = Path(workspace)
        self.system_prompt = system_prompt
        self.system_extra = system_extra
        self.max_steps = max_steps
        self.max_cost_usd = max_cost_usd
        self.reflect_on_error = reflect_on_error
        self.on_step = on_step or (lambda s: None)
        self.stream_callback = stream_callback
        self.reasoning_callback = reasoning_callback
        self.cost_per_mtok = cost_per_mtok
        self.trace = JsonlTraceWriter(trace_path) if trace_path else None
        self.history: list[Message] = []

    def _estimate_cost(self, usage) -> float:
        p, c = self.cost_per_mtok
        return usage.prompt_tokens / 1e6 * p + usage.completion_tokens / 1e6 * c

    def _emit(self, record: StepRecord) -> None:
        if self.trace:
            self.trace.write("step", {
                "step": record.step,
                "kind": record.kind,
                "content": record.content[:2000],
                "tool": record.tool_name,
                "args": record.tool_args,
                "observation": (record.observation or "")[:2000],
                "duration_s": round(record.duration_s, 3),
            })
        self.on_step(record)

    def reset(self, system_extra: str | None = None) -> None:
        extra = system_extra if system_extra is not None else self.system_extra
        sp = self.system_prompt + ("\n\n" + extra if extra else "")
        self.history = [Message(role="system", content=sp)]

    async def run(self, task: str, should_stop: Callable[[], bool] | None = None) -> RunResult:
        """Execute a task autonomously.

        should_stop: polled between steps; when it returns True the run ends
        gracefully (any dangling tool_calls are answered so the conversation
        history stays API-valid).
        """
        if not self.history or self.history[0].role != "system":
            self.reset()
        self.history.append(Message(role="user", content=task))

        result = RunResult(final_answer="")
        consecutive_errors = 0

        for step_no in range(1, self.max_steps + 1):
            result.steps_used = step_no
            t0 = time.monotonic()

            resp: LLMResponse = await self.provider.chat(
                self.history,
                tools=self.registry.schemas(),
                stream_callback=self.stream_callback,
                reasoning_callback=self.reasoning_callback,
            )
            result.prompt_tokens += resp.usage.prompt_tokens
            result.completion_tokens += resp.usage.completion_tokens
            result.cost_usd += self._estimate_cost(resp.usage)

            # ---- budget guard: check immediately after every LLM call ---- #
            if result.cost_usd >= self.max_cost_usd:
                result.stopped_reason = "max_cost"
                result.final_answer = (
                    f"Stopped: cost budget ${self.max_cost_usd} exceeded "
                    f"(used {result.prompt_tokens}+{result.completion_tokens} tokens)."
                )
                return result

            msg = resp.message
            log.info("[step %d] finish=%s tool_calls=%s tokens=%dt",
                     step_no, resp.finish_reason,
                     [tc.name for tc in msg.tool_calls], resp.usage.total_tokens)
            self.history.append(msg)

            # ---- cooperative stop: finalize cleanly, keep history valid ---- #
            if should_stop is not None and should_stop():
                for tc in msg.tool_calls:
                    self.history.append(Message(
                        role="tool", content="(stopped by user)",
                        tool_call_id=tc.id, name=tc.name))
                answer = (msg.content or "").strip() or "(已由用户停止)"
                rec = StepRecord(step=step_no, kind="final", content=answer,
                                 duration_s=time.monotonic() - t0)
                self._emit(rec)
                result.steps.append(rec)
                result.final_answer = answer
                result.stopped_reason = "user_stopped"
                log.info("run stopped by user at step %d", step_no)
                return result

            # ---- no tool calls => the model believes it is done ---- #
            if not msg.tool_calls:
                answer = (msg.content or "").strip() or "(empty response)"
                rec = StepRecord(step=step_no, kind="final", content=answer,
                                 duration_s=time.monotonic() - t0)
                self._emit(rec)
                result.steps.append(rec)
                result.final_answer = answer
                return result

            # ---- think/announce step ---- #
            if msg.content:
                rec = StepRecord(step=step_no, kind="think", content=msg.content,
                                 duration_s=time.monotonic() - t0)
                self._emit(rec)
                result.steps.append(rec)

            # ---- act: execute every requested tool call ---- #
            had_error = False
            for tc in msg.tool_calls:
                t1 = time.monotonic()
                observation = await self.registry.execute(tc)
                if observation.startswith("ERROR"):
                    had_error = True
                    consecutive_errors += 1
                else:
                    consecutive_errors = 0
                rec = StepRecord(
                    step=step_no, kind="act",
                    tool_name=tc.name, tool_args=tc.arguments,
                    observation=observation,
                    duration_s=time.monotonic() - t1,
                )
                self._emit(rec)
                result.steps.append(rec)
                self.history.append(Message(
                    role="tool",
                    content=observation,
                    tool_call_id=tc.id,
                    name=tc.name,
                ))

            # ---- reflect after failures ---- #
            if had_error and self.reflect_on_error:
                self.history.append(Message(role="system", content=REFLECTION_PROMPT))
                consecutive_errors = 0  # one fresh chance per reflection

            # ---- cooperative stop between steps ---- #
            if should_stop is not None and should_stop():
                rec = StepRecord(step=step_no, kind="final",
                                 content="(任务已由用户停止)",
                                 duration_s=time.monotonic() - t0)
                self._emit(rec)
                result.steps.append(rec)
                result.final_answer = rec.content
                result.stopped_reason = "user_stopped"
                log.info("run stopped by user after step %d", step_no)
                return result

        result.stopped_reason = "max_steps"
        result.final_answer = (
            f"Stopped: reached max steps ({self.max_steps}). Last message: "
            + (self.history[-1].content or "")[:500]
        )
        return result


def build_default_agent(cfg, provider, workspace: str | Path = ".") -> Agent:
    """Wire a fully-equipped agent from Config + provider."""
    from nova.tools.filesystem import FilesystemTools
    from nova.tools.memory_tool import MemoryTools
    from nova.tools.python_repl import PythonReplTool
    from nova.tools.shell import ShellTool
    from nova.tools.web import WebTools

    registry = ToolRegistry()
    FilesystemTools(workspace).register(registry)
    if cfg.get("tools.shell.enabled", True):
        ShellTool(str(workspace), int(cfg.get("tools.shell.timeout_seconds", 60))).register(registry)
    if cfg.get("tools.python_repl.enabled", True):
        PythonReplTool(str(workspace), int(cfg.get("tools.python_repl.timeout_seconds", 30))).register(registry)
    if cfg.get("tools.web.enabled", True):
        WebTools().register(registry)

    memory_system_prompt = ""
    if cfg.get("memory.enabled", True):
        from nova.config import PROJECT_ROOT
        db_path = Path(cfg.get("memory.db_path", ".nova/memory.sqlite3"))
        if not db_path.is_absolute():
            db_path = PROJECT_ROOT / db_path
        from nova.memory.embeddings import HashEmbedder
        from nova.memory.store import MemoryStore
        store = MemoryStore(db_path, embedder=HashEmbedder(int(cfg.get("memory.embedding.dim", 512))))
        MemoryTools(store, workspace=workspace).register(registry)
        memory_system_prompt = (
            "## Long-term memory\n"
            "You have persistent memory across sessions. Use `remember` to store "
            "important facts/decisions/lessons as you discover them; use `recall` and "
            "`search_knowledge` when past context might help; use `ingest_document` "
            "to index files before answering questions grounded in them."
        )

    trace_dir = cfg.get("logging.dir", ".nova/logs")
    trace_path = Path(trace_dir) / "trace.jsonl"
    return Agent(
        provider,
        registry,
        workspace=workspace,
        max_steps=int(cfg.get("agent.max_steps", 40)),
        max_cost_usd=float(cfg.get("agent.max_cost_usd", 2.0)),
        reflect_on_error=bool(cfg.get("agent.reflect_on_error", True)),
        trace_path=trace_path,
        system_extra=memory_system_prompt or None,
    )
