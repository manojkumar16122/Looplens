"""Demo: a simulated agent gets stuck in a loop, and LoopLens blocks it.

Start the server first:   uvicorn api:app --reload
Then run the demo:        python demo_agent.py
"""

from __future__ import annotations

import json
import os
import sys

import httpx

BASE_URL = os.getenv("LOOPLENS_URL", "http://127.0.0.1:8000").rstrip("/")
TOOL = "search_web"


def print_step(number: int, label: str, body: dict) -> None:
    status = body["status"].upper()
    print(f"[{number}] {label:<38} -> {status}")
    if body.get("reason"):
        print(f"      reason: {body['reason']}")
    print()


def main() -> int:
    with httpx.Client(base_url=BASE_URL, timeout=20.0) as client:
        try:
            client.get("/health").raise_for_status()
        except httpx.HTTPError as exc:
            print(f"Could not reach LoopLens at {BASE_URL} ({exc}).")
            print("Start the server with: uvicorn api:app --reload")
            return 1

        # 1. Create a session.
        session = client.post("/session").json()
        session_id = session["session_id"]
        print(f"Created session {session_id}\n")

        def tool_call(args: dict) -> dict:
            response = client.post(
                "/tool_call",
                json={"session_id": session_id, "tool_name": TOOL, "arguments": args, "tokens": 500},
            )
            response.raise_for_status()
            return response.json()

        # 2. Two healthy, distinct calls. Each returns a result that LoopLens may cache.
        healthy_queries = ["LangGraph agent memory patterns", "LoopLens loop detection design"]
        for number, query in enumerate(healthy_queries, start=1):
            args = {"query": query}
            body = tool_call(args)
            print_step(number, f"{TOOL} {json.dumps(args)}", body)
            client.post(
                "/tool_result",
                json={"session_id": session_id, "tool_name": TOOL, "arguments": args,
                      "result": {"hits": 5, "query": query}},
            ).raise_for_status()

        # 3. The agent gets stuck repeating the same call with the same arguments.
        stuck_args = {"query": "Groq llama-3.1-8b-instant latency"}
        print(f"Agent is now stuck repeating {TOOL} {json.dumps(stuck_args)} ...\n")
        for number in range(3, 6):
            body = tool_call(stuck_args)
            print_step(number, f"{TOOL} (identical retry)", body)

        # 4. Show the dashboard summary.
        dashboard = client.get("/dashboard", params={"session_id": session_id}).json()
        stats = dashboard["session"]
        print("Session summary")
        print(f"  status        : {stats['status']}")
        print(f"  total calls   : {stats['total_calls']}")
        print(f"  blocked calls : {stats['blocked_calls']}")
        print(f"  total tokens  : {stats['total_tokens']}")
        print(f"  total cost    : ${stats['total_cost_usd']:.4f}")
        if dashboard["alerts"]:
            print(f"\nLatest Groq diagnosis:\n  {dashboard['alerts'][0]['diagnosis']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
