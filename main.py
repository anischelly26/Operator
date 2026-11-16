"""CLI for offline sandbox triage, explicit live mode and evaluation."""

import argparse
import json
import os
import re
import sqlite3
from pathlib import Path

from agent.checkpoint import HumanCheckpoint
from agent.llm import MockDeterministicLLMClient, OpenAILLMClient
from agent.loop import AgentLoop
from agent.preview import preview_ticket
from agent.tools import ToolRegistry
from eval.runner import EvalRunner
from sandbox.environment import SandboxEnvironment


def run_interactive(ticket_id: str, use_live_api: bool = False, db_path=None):
    with SandboxEnvironment(db_path=db_path) as env:
        if use_live_api:
            import dotenv
            dotenv.load_dotenv()
            api_key = os.getenv("OPENAI_API_KEY", "")
            if not api_key or api_key.startswith("your_"):
                raise ValueError("--live requires an OPENAI_API_KEY configured by the operator.")
            print("[LLM] Live OpenAI mode explicitly enabled.")
            llm = OpenAILLMClient()
        else:
            print("[LLM] Offline deterministic simulator; token costs are estimates.")
            llm = MockDeterministicLLMClient()

        checkpoint = HumanCheckpoint(mode="interactive")
        tools = ToolRegistry(env, checkpoint_handler=checkpoint.request_approval, ticket_scope=ticket_id)
        loop = AgentLoop(
            llm=llm, tools=tools,
            max_steps=int(os.getenv("MAX_STEPS", "10")),
            max_budget_usd=float(os.getenv("BUDGET_LIMIT_USD", "0.25")),
        )
        trajectory = loop.run(
            task_id=f"RUN-{ticket_id}",
            prompt=f"Please inspect, triage, and resolve customer support ticket {ticket_id}.",
        )
        print(f"TRAJECTORY COMPLETED: Status = {trajectory.status}")
        print(f"Steps: {trajectory.total_steps}; accounted cost: ${trajectory.total_cost_usd:.5f}")
        print(trajectory.final_answer or trajectory.error_summary or "")
        return trajectory


def main():
    parser = argparse.ArgumentParser(description="The Operator - Customer support sandbox")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--eval", action="store_true", help="Run the offline benchmark in an isolated database")
    mode.add_argument("--reset", action="store_true", help="Reset the selected sandbox database")
    mode.add_argument("--live", action="store_true", help="Explicitly enable live OpenAI calls")
    mode.add_argument("--audit", metavar="TICKET", help="Print persisted approval decisions and committed actions as JSON")
    mode.add_argument("--preview", action="store_true", help="Preview an offline workflow in a disposable database copy")
    parser.add_argument("--ticket", default="TKT-101", help="Ticket ID to triage")
    parser.add_argument("--db", type=Path, help="Optional SQLite sandbox path")
    parser.add_argument("--trace", type=Path, help="Write the triage trajectory as JSON")
    args = parser.parse_args()
    if not re.fullmatch(r"TKT-[A-Za-z0-9_-]+", args.ticket):
        parser.error("--ticket must be a ticket identifier such as TKT-101")
    if args.audit and not re.fullmatch(r"TKT-[A-Za-z0-9_-]+", args.audit):
        parser.error("--audit requires a ticket identifier such as TKT-101")
    try:
        if args.preview:
            report = preview_ticket(args.ticket, db_path=args.db)
            print(json.dumps(report, indent=2))
            if args.trace:
                args.trace.parent.mkdir(parents=True, exist_ok=True)
                args.trace.write_text(json.dumps(report, indent=2), encoding="utf-8")
            return 0 if report["trajectory"]["status"] == "SUCCESS" else 1
        if args.audit:
            with SandboxEnvironment(db_path=args.db) as env:
                print(json.dumps(env.audit_ticket(args.audit), indent=2))
            return 0
        if args.reset:
            with SandboxEnvironment(db_path=args.db) as env:
                env.reset()
            print("[Sandbox] Database reset to baseline.")
            return 0
        if args.eval:
            report = EvalRunner().run_all()
            return 0 if all(row["passed"] for row in report["scenario_results"]) else 1
        trajectory = run_interactive(args.ticket, args.live, db_path=args.db)
        if args.trace:
            args.trace.parent.mkdir(parents=True, exist_ok=True)
            args.trace.write_text(trajectory.model_dump_json(indent=2), encoding="utf-8")
        return 0 if trajectory.status == "SUCCESS" else 1
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(2, f"Configuration or file error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())

