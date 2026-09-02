"""Multi-agent orchestration: Planner -> Executor(s) -> Critic with revision loop."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from nova.llm.base import Message
from nova.log import get_logger

log = get_logger("team")

PLANNER_SYSTEM = """\
You are the Planner of an AI agent team. Decompose the user's task into a short, \
ordered list of concrete subtasks. Each subtask must be independently executable \
by a worker agent that has file/shell/python/web tools and works in one workspace.

Respond with ONLY a JSON array, no markdown fences, no commentary:
[
  {"title": "short title", "detail": "precise instructions for the worker agent"}
]
Keep it to 1-6 subtasks. If the task is trivial, use a single subtask.
"""

CRITIC_SYSTEM = """\
You are the Critic of an AI agent team. Given the original task and the workers' \
results, judge whether the task is genuinely complete and correct.

Respond with ONLY a JSON object, no markdown fences:
{"verdict": "APPROVED", "reasoning": "..."}
or
{"verdict": "NEEDS_REVISION", "reasoning": "...", "feedback": "what exactly to fix"}
Be strict about correctness but pragmatic about style.
"""


@dataclass
class SubtaskResult:
    title: str
    detail: str
    answer: str
    steps_used: int = 0


@dataclass
class TeamResult:
    final_answer: str
    plan: list[dict] = field(default_factory=list)
    subtask_results: list[SubtaskResult] = field(default_factory=list)
    critique: dict = field(default_factory=dict)
    rounds: int = 0
    stopped_reason: str = "completed"


class AgentTeam:
    """Planner / Executor / Critic pipeline over a shared provider + workspace."""

    def __init__(
        self,
        cfg,
        provider,
        *,
        workspace: str | Path = ".",
        max_rounds: int = 2,
        executor_max_steps: int | None = None,
        on_event: Callable[[str, dict], None] | None = None,
    ):
        self.cfg = cfg
        self.provider = provider
        self.workspace = Path(workspace)
        self.max_rounds = max_rounds
        self.executor_max_steps = executor_max_steps or min(int(cfg.get("agent.max_steps", 40)), 15)
        self.on_event = on_event or (lambda t, p: None)

    def _emit(self, event_type: str, payload: dict) -> None:
        log.info("[team:%s] %s", event_type, {k: (str(v)[:100]) for k, v in payload.items()})
        self.on_event(event_type, payload)

    async def _complete(self, system: str, user: str) -> str:
        resp = await self.provider.chat(
            [Message(role="system", content=system), Message(role="user", content=user)],
            tools=None,
        )
        return resp.message.content or ""

    async def _plan(self, task: str) -> list[dict]:
        raw = await self._complete(
            PLANNER_SYSTEM, f"Task:\n{task}\n\nProduce the JSON subtask array."
        )
        return parse_json_array(raw)

    async def _critique(self, task: str, results: list[SubtaskResult]) -> dict:
        digest = "\n\n".join(
            f"### Subtask {i + 1}: {r.title}\nWorker result:\n{r.answer[:2000]}"
            for i, r in enumerate(results)
        )
        raw = await self._complete(
            CRITIC_SYSTEM, f"Original task:\n{task}\n\nWorkers' results:\n{digest}"
        )
        return parse_json_object(raw) or {
            "verdict": "APPROVED",
            "reasoning": "unparseable critic response; accepting",
        }

    async def _execute_subtask(self, subtask: dict, context: str) -> SubtaskResult:
        from nova.agent.core import build_default_agent

        agent = build_default_agent(self.cfg, self.provider, workspace=self.workspace)
        agent.max_steps = self.executor_max_steps
        prompt = f"{subtask.get('detail', '')}"
        if context:
            prompt += "\n\nContext from earlier subtasks (for reference only):\n" + context[:4000]
        result = await agent.run(prompt)
        return SubtaskResult(
            title=subtask.get("title", "?"),
            detail=subtask.get("detail", ""),
            answer=result.final_answer,
            steps_used=result.steps_used,
        )

    async def run(self, task: str) -> TeamResult:
        team_result = TeamResult(final_answer="")

        # ---- plan ---- #
        self._emit("plan_start", {"task": task})
        try:
            team_result.plan = await self._plan(task)
        except Exception as exc:
            log.warning("planner failed (%s); falling back to single subtask", exc)
        if not team_result.plan:
            team_result.plan = [{"title": task[:80], "detail": task}]
        self._emit("plan_ready", {"subtasks": [p.get("title") for p in team_result.plan]})

        # ---- execute + critique rounds ---- #
        feedback = ""
        for round_no in range(1, self.max_rounds + 1):
            team_result.rounds = round_no
            context = ""
            results: list[SubtaskResult] = []
            for i, subtask in enumerate(team_result.plan):
                self._emit(
                    "executor_start",
                    {"round": round_no, "index": i + 1, "title": subtask.get("title", "")},
                )
                r = await self._execute_subtask(subtask, context)
                self._emit("executor_done", {"index": i + 1, "answer_preview": r.answer[:200]})
                results.append(r)
                context += f"\n[{subtask.get('title', '?')}]: {r.answer[:1500]}"

            critique = await self._critique(task, results)
            team_result.critique = critique
            team_result.subtask_results = results
            verdict = str(critique.get("verdict", "APPROVED")).upper()
            self._emit(
                "critique",
                {
                    "round": round_no,
                    "verdict": verdict,
                    "reasoning": str(critique.get("reasoning", ""))[:300],
                },
            )

            if verdict == "APPROVED" or not critique.get("feedback"):
                break
            feedback = str(critique.get("feedback", ""))
            # revise the plan for next round
            revise_task = (
                f"{task}\n\nA previous attempt was rejected by the reviewer. "
                f"Feedback: {feedback}\n"
                f"Previous plan was: {json.dumps(team_result.plan, ensure_ascii=False)}"
            )
            try:
                revised = await self._plan(revise_task)
                if revised:
                    team_result.plan = revised
            except Exception as exc:
                log.warning("re-planning failed (%s)", exc)

        # ---- synthesize ---- #
        synthesis_input = f"Original task:\n{task}\n\nSubtask results:\n" + "\n\n".join(
            f"[{r.title}]\n{r.answer}" for r in team_result.subtask_results
        )
        if feedback:
            synthesis_input += f"\n\nReviewer feedback addressed in this revision:\n{feedback}"
        team_result.final_answer = await self._complete(
            "You are the team lead. Synthesize the workers' results into a concise, "
            "accurate final report for the user: what was done, key artifacts, and any "
            "caveats. Do not invent facts that are not in the results.",
            synthesis_input,
        )
        self._emit("done", {})
        return team_result


def parse_json_array(raw: str) -> list[dict]:
    m = re.search(r"\[.*\]", raw.strip(), re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def parse_json_object(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw.strip(), re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}
