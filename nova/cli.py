"""NovaAgent command-line interface.

Usage:
    .venv/bin/python -m nova.cli run "task description" [--workspace DIR]
    .venv/bin/python -m nova.cli chat  [--workspace DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from nova.agent.core import build_default_agent
from nova.config import api_key_for, load_config
from nova.llm.base import Message
from nova.llm.provider import create_provider_from_config
from nova.log import setup_logging


def _print_step(step) -> None:
    if step.kind == "think":
        print(f"\n\033[36m[step {step.step}] thinking\033[0m\n{step.content[:600]}")
    elif step.kind == "act":
        args = ", ".join(f"{k}={str(v)[:80]!r}" for k, v in (step.tool_args or {}).items())
        print(f"\n\033[33m[step {step.step}] {step.tool_name}({args})\033[0m")
        obs = (step.observation or "")[:800]
        print(f"\033[90m{obs}{'...' if len(step.observation or '') > 800 else ''}\033[0m")
    elif step.kind == "final":
        print(f"\n\033[32m[final | {step.duration_s:.1f}s]\033[0m")


async def cmd_run(cfg, task: str, workspace: Path) -> int:
    provider = create_provider_from_config(cfg, api_key_for(cfg))
    try:
        agent = build_default_agent(cfg, provider, workspace=workspace)
        result = await agent.run(task)
    finally:
        await provider.aclose()

    print(f"\n{'=' * 60}\n{result.final_answer}\n{'=' * 60}")
    print(
        f"steps={result.steps_used} "
        f"tokens={result.prompt_tokens}+{result.completion_tokens} "
        f"cost≈${result.cost_usd:.4f} reason={result.stopped_reason}"
    )
    return 0 if result.stopped_reason == "completed" else 1


async def cmd_team(cfg, task: str, workspace: Path) -> int:
    from nova.agent.team import AgentTeam

    provider = create_provider_from_config(cfg, api_key_for(cfg))

    def on_event(event_type: str, payload: dict) -> None:
        if event_type == "plan_ready":
            subs = "\n".join(f"  {i}. {t}" for i, t in enumerate(payload["subtasks"], 1))
            print(f"\n\033[36m[planner]\033[0m {len(payload['subtasks'])} subtasks:\n{subs}")
        elif event_type == "executor_start":
            print(f"\n\033[33m[executor {payload['index']}]\033[0m {payload['title']}")
        elif event_type == "critique":
            color = "\033[32m" if payload["verdict"] == "APPROVED" else "\033[31m"
            print(
                f"{color}[critic round {payload['round']}]\033[0m "
                f"{payload['verdict']} - {payload['reasoning'][:200]}"
            )

    team = AgentTeam(cfg, provider, workspace=workspace, on_event=on_event)
    print(f"\033[1m[team]\033[0m task: {task}")
    try:
        result = await team.run(task)
    finally:
        await provider.aclose()

    print(f"\n{'=' * 60}\n{result.final_answer}\n{'=' * 60}")
    print(
        f"subtasks={len(result.subtask_results)} rounds={result.rounds} "
        f"tokens={provider.total_usage.total_tokens}"
    )
    return 0


async def cmd_chat(cfg, workspace: Path) -> int:
    provider = create_provider_from_config(cfg, api_key_for(cfg))
    agent = build_default_agent(cfg, provider, workspace=workspace)
    agent.reset()
    print("NovaAgent interactive mode. Type 'exit' to quit, '/reset' to clear context.\n")

    while True:
        try:
            user = input("\033[35myou>\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user:
            continue
        if user.lower() in ("exit", "quit", "q"):
            break
        if user == "/reset":
            agent.reset()
            print("(context cleared)\n")
            continue

        # multi-turn: reuse history, but strip the previous user task framing
        agent.history.append(Message(role="user", content=user))
        try:
            while True:
                resp = await provider.chat(
                    agent.history,
                    tools=agent.registry.schemas(),
                    stream_callback=lambda d: print(d, end="", flush=True),
                )
                print()
                msg = resp.message
                agent.history.append(msg)
                if not msg.tool_calls:
                    break
                for tc in msg.tool_calls:
                    observation = await agent.registry.execute(tc)
                    print(f"\033[90m[{tc.name}] {observation[:400]}\033[0m")
                    agent.history.append(
                        Message(role="tool", content=observation, tool_call_id=tc.id, name=tc.name)
                    )
        except KeyboardInterrupt:
            print("\n(interrupted)")
        usage = provider.total_usage
        print(f"\033[90m-- session tokens: {usage.total_tokens} --\033[0m\n")

    await provider.aclose()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="nova", description="NovaAgent CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "chat", "team"):
        p = sub.add_parser(name)
        if name in ("run", "team"):
            p.add_argument("task", help="Task for the agent (or team) to complete")
        p.add_argument("--workspace", default=".", help="Working directory for file/shell tools")
    serve_p = sub.add_parser("serve")
    serve_p.add_argument("--host", default=None)
    serve_p.add_argument("--port", type=int, default=None)
    serve_p.add_argument("--no-open", action="store_true", help="Do not auto-open the browser")
    serve_p.add_argument(
        "--workspace",
        default=".",
        help="Directory the web agent may read/write/execute in. "
        "Use a dedicated sandbox directory in production.",
    )
    args = parser.parse_args()

    cfg = load_config()
    setup_logging(cfg.get("logging.level", "INFO"), cfg.get("logging.dir"))

    workspace = Path(args.workspace).resolve() if hasattr(args, "workspace") else Path(".")
    if args.command == "run":
        code = asyncio.run(cmd_run(cfg, args.task, workspace))
    elif args.command == "team":
        code = asyncio.run(cmd_team(cfg, args.task, workspace))
    elif args.command == "serve":
        import socket
        import threading
        import time
        import webbrowser

        import uvicorn

        from nova.web.server import create_app

        host = args.host or str(cfg.get("server.host", "127.0.0.1"))
        port = args.port or int(cfg.get("server.port", 8321))
        url = f"http://{host}:{port}"
        print(f"NovaAgent web UI: {url}")
        print(f"Agent workspace : {workspace.resolve()}")
        if (workspace.resolve() / ".env").exists():
            print(
                "\033[31mWARNING: the workspace contains a .env file — the agent can "
                "read it. Use --workspace to point at a dedicated sandbox directory.\033[0m"
            )
        if os.environ.get("NOVA_WEB_TOKEN") or cfg.get("server.auth_token"):
            print("API auth        : enabled (Bearer token required)")
        elif host not in ("127.0.0.1", "localhost"):
            print(
                "\033[33mWARNING: no NOVA_WEB_TOKEN set — anyone who can reach this "
                "port can use your API key and run commands. Set NOVA_WEB_TOKEN "
                "or keep the server on 127.0.0.1 behind a reverse proxy.\033[0m"
            )

        if not args.no_open:

            def open_when_ready() -> None:
                """Only launch the browser once the port actually accepts connections."""
                deadline = time.time() + 30
                while time.time() < deadline:
                    try:
                        with socket.create_connection((host, port), timeout=0.5):
                            time.sleep(0.3)
                            webbrowser.open(url)
                            return
                    except OSError:
                        time.sleep(0.25)
                print("(server did not become ready; open the URL manually)")

            threading.Thread(target=open_when_ready, daemon=True).start()
        uvicorn.run(create_app(cfg, workspace=workspace), host=host, port=port, log_level="info")
        code = 0
    else:
        code = asyncio.run(cmd_chat(cfg, workspace))
    sys.exit(code)


if __name__ == "__main__":
    main()
