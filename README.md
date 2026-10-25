# LoopLens

**Observability and governance middleware for AI agents.**
LoopLens detects runaway loops, enforces cost budgets, and caches redundant tool calls, with Groq-powered diagnoses explaining why an agent got stuck.

> **The problem.** Roughly 75% of enterprise agents hit a production wall. Two failure modes cause most of the damage, and both are invisible by default:
> - **Loops.** An agent repeats the same tool call with the same arguments, burning tokens while making no progress.
> - **Cost explosions.** A single session quietly consumes a budget that was never meant for it, and nobody notices until the invoice arrives.

> **The solution.** LoopLens is a lightweight seatbelt. The agent asks LoopLens before every tool call. LoopLens answers `allowed`, `blocked` or `cached`, and when it blocks a call it explains the failure in plain language.

---

## Features

| Capability | What it does |
|---|---|
| **Loop detection** | Tracks the last 10 calls per session. Three identical calls in a row (same tool, same arguments, key order ignored) raise a Loop Alert. |
| **Cost governance** | Converts tokens to USD. When a session would exceed `COST_THRESHOLD_USD`, the breaching call is blocked, the session moves to `PAUSED`, and a Cost Alert is raised. |
| **Groq diagnosis** | On a new loop, `llama-3.1-8b-instant` returns a one-sentence diagnosis of why the agent is stuck and one sentence on how to break out. |
| **Result caching** | Agents report tool results. Identical later calls within the same session return the cached result, so the tool is not executed again. |
| **Live dashboard** | A dark-mode UI with a scrolling activity feed, token-burn chart, governance stats, and alert cards. |
| **Durable audit trail** | Sessions, every tool call and every alert are persisted to SQLite. |

---

## Architecture

```
            ┌──────────────────────┐
            │   Agent (your code)  │
            └──────┬───────▲───────┘
   POST /tool_call │       │ allowed / blocked / cached
                   ▼       │
┌──────────────────────────────────────────────────────┐
│                    FastAPI  (api.py)                 │
│  ┌────────────────── GovernanceEngine ─────────────┐ │
│  │  CostTracker → LoopDetector → CacheManager      │ │
│  │                    │                            │ │
│  │             GroqDiagnoser (on loop)             │ │
│  └─────────────────────────────────────────────────┘ │
│                         │                            │
│                 SQLite (database.py)                 │
│          sessions · tool_calls · loop_alerts         │
└───────────────────────▲──────────────────────────────┘
                        │ GET /dashboard (polled every 2s)
                 ┌──────┴───────┐
                 │  Dashboard   │  static/ (HTML · CSS · JS · Chart.js)
                 └──────────────┘
```

Evaluation order for each `POST /tool_call`:

1. **Paused session?** Block the call.
2. **Would this call breach the cost budget?** Block it, pause the session, and raise a Cost Alert.
3. **Is this the third identical call in a row?** Block it and raise a Loop Alert with a Groq diagnosis. Later calls in the same streak are blocked without another Groq request.
4. **Is an identical call cached?** Return `cached` with the stored result.
5. **Otherwise** return `allowed`.

Every call is recorded with its token count and status, whatever the outcome.

---

## Quick start

### Prerequisites

- Python 3.10 or newer
- A Groq API key (optional; without one, LoopLens uses a deterministic fallback diagnosis)

### 1. Set up a virtual environment and install dependencies

```bash
cd looplens
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure the environment

```bash
cp .env.example .env
# then edit .env and set GROQ_API_KEY
```

### 3. Start the server

```bash
uvicorn api:app --reload
```

Open the dashboard at **http://127.0.0.1:8000**, then click **Simulate Runaway Agent** to watch the seatbelt engage.

### 4. Run the demo agent

In a second terminal (with the venv active):

```bash
python demo_agent.py
```

You should see the two healthy calls allowed, the third identical call allowed, and the fifth call blocked with a Groq-generated diagnosis.

### 5. Run the tests

```bash
pytest -v
```

The tests use an isolated temporary database and do not call Groq.

---

## API reference

Interactive docs are available at **http://127.0.0.1:8000/docs** while the server is running.

### `POST /session`
Creates a session.

```bash
curl -X POST http://127.0.0.1:8000/session
```
```json
{ "session_id": "3f9c…", "status": "ACTIVE", "start_time": "2026-10-25T04:30:00.000+00:00" }
```

### `POST /tool_call`
Called by the agent before executing a tool.

```bash
curl -X POST http://127.0.0.1:8000/tool_call \
  -H "Content-Type: application/json" \
  -d '{"session_id": "3f9c…", "tool_name": "search_web", "arguments": {"query": "groq pricing"}, "tokens": 1200}'
```
```json
{ "status": "allowed", "reason": null, "cached_result": null,
  "session_status": "ACTIVE", "total_tokens": 1200, "total_cost_usd": 0.0024 }
```

When a loop is detected:
```json
{ "status": "blocked",
  "reason": "Loop detected: search_web was called 3 times in a row with identical arguments. The agent is re-querying ...",
  "session_status": "ACTIVE", "total_tokens": 3600, "total_cost_usd": 0.0072 }
```

| Field | Type | Notes |
|---|---|---|
| `session_id` | string | Required. Returned by `POST /session`. |
| `tool_name` | string | Required. Letters, digits, `_`, `-` and `.`, up to 100 characters. |
| `arguments` | object | Optional. Up to 32 KB serialized. Key order does not matter. |
| `tokens` | integer | Optional. Tokens consumed by the agent step that produced this call. |

### `POST /tool_result`
Called by the agent after a tool executes, so identical future calls can be served from cache.

```bash
curl -X POST http://127.0.0.1:8000/tool_result \
  -H "Content-Type: application/json" \
  -d '{"session_id": "3f9c…", "tool_name": "search_web", "arguments": {"query": "groq pricing"}, "result": {"hits": 5}}'
```

### `GET /dashboard`
Returns session stats, recent calls (up to 50), alerts (up to 20) and the token series. Pass `?session_id=` to target a session, or omit it to get the most recent one.

### `POST /reset`
Deletes all sessions, calls and alerts, and clears in-memory detector and cache state. Intended for development and testing.

### `GET /health`
Liveness check.

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `GROQ_API_KEY` | *(none)* | Enables Groq-generated loop diagnoses. |
| `COST_THRESHOLD_USD` | `0.05` | Per-session budget. Calls that would exceed it are blocked and the session is paused. |
| `PRICE_PER_1K_TOKENS_USD` | `0.002` | Blended price used to convert tokens into dollars. Set this to match your model's pricing. |
| `LOOP_THRESHOLD` | `3` | Consecutive identical calls that count as a loop (minimum 2). |
| `LOOP_HISTORY_SIZE` | `10` | Calls kept in memory per session for loop detection. |
| `GROQ_MODEL` | `llama-3.1-8b-instant` | Groq model used for diagnoses. |
| `LOOPLENS_DB_PATH` | `looplens.db` | SQLite file location. |

---

## Project structure

```
looplens/
├── api.py              # FastAPI app and routes
├── engine.py           # LoopDetector, CostTracker, GroqDiagnoser, CacheManager, GovernanceEngine
├── database.py         # SQLite schema and data access
├── models.py           # Pydantic request and response models
├── config.py           # Environment-based settings with validation
├── demo_agent.py       # Simulated runaway agent
├── static/
│   ├── index.html      # Dashboard layout
│   ├── style.css       # Dark theme
│   └── app.js          # Polling, rendering, Chart.js, simulation
├── tests/
│   ├── conftest.py     # Isolated database and test environment
│   └── test_looplens.py
├── requirements.txt
├── pytest.ini
├── .env.example
└── README.md
```

---

## Design notes and limitations

- **In-memory state.** Loop history and cached results live in process memory, so restarting the server clears them. Sessions, calls and alerts persist in SQLite.
- **Single process.** Decisions are serialised with a lock, which is correct for one worker. Scaling to multiple workers needs shared state, such as Redis, for the detector and cache.
- **Blocking diagnosis.** The Groq request has a 10-second timeout and runs inside the decision lock, so it briefly delays other decisions. Moving it to a background task is a sensible next step for high-traffic deployments.
- **Token accounting is self-reported.** LoopLens trusts the `tokens` value the agent sends. Pair it with provider usage data for billing-grade accuracy.
- **LoopLens does not execute tools.** It governs calls. The agent runs the tool and reports results through `/tool_result`.

---

## Roadmap

- Background diagnosis with a webhook when an alert fires
- Per-tool and per-tenant budgets
- Redis-backed detector and cache for multi-worker deployments
- Pluggable pricing tables per model

---

## #BuildInPublic: Week 4

This project is part of Week 4 of my #BuildInPublic series, where I document building production-minded AI infrastructure in public. Follow along for the design decisions, trade-offs and mistakes along the way.

---

## License

MIT. See `LICENSE`.
