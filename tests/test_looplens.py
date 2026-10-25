"""End-to-end tests for the LoopLens governance rules."""


def new_session(client) -> str:
    response = client.post("/session")
    assert response.status_code == 201
    return response.json()["session_id"]


def call(client, session_id, tool="search_web", args=None, tokens=0):
    return client.post(
        "/tool_call",
        json={"session_id": session_id, "tool_name": tool, "arguments": args or {}, "tokens": tokens},
    )


def test_normal_calls_are_allowed(client):
    sid = new_session(client)
    assert call(client, sid, args={"q": "a"}).json()["status"] == "allowed"
    assert call(client, sid, args={"q": "b"}).json()["status"] == "allowed"


def test_loop_is_blocked_on_third_identical_call(client):
    sid = new_session(client)
    responses = [call(client, sid, args={"q": "same"}).json() for _ in range(3)]

    assert [r["status"] for r in responses] == ["allowed", "allowed", "blocked"]
    assert "Loop detected" in responses[2]["reason"]

    dashboard = client.get("/dashboard", params={"session_id": sid}).json()
    assert dashboard["session"]["blocked_calls"] == 1
    assert any(alert["alert_type"] == "LOOP" for alert in dashboard["alerts"])


def test_only_one_loop_alert_per_streak(client):
    sid = new_session(client)
    statuses = [call(client, sid, args={"q": "same"}).json()["status"] for _ in range(4)]
    assert statuses == ["allowed", "allowed", "blocked", "blocked"]

    dashboard = client.get("/dashboard", params={"session_id": sid}).json()
    assert len([a for a in dashboard["alerts"] if a["alert_type"] == "LOOP"]) == 1


def test_argument_key_order_does_not_defeat_detection(client):
    sid = new_session(client)
    call(client, sid, args={"a": 1, "b": 2})
    call(client, sid, args={"b": 2, "a": 1})
    assert call(client, sid, args={"a": 1, "b": 2}).json()["status"] == "blocked"


def test_cost_limit_pauses_session(client):
    sid = new_session(client)
    # 30,000 tokens at $0.002 per 1K tokens = $0.06, which exceeds the $0.05 threshold.
    breach = call(client, sid, args={"q": "x"}, tokens=30_000).json()
    assert breach["status"] == "blocked"
    assert "Cost limit" in breach["reason"]
    assert breach["session_status"] == "PAUSED"

    follow_up = call(client, sid, args={"q": "y"}).json()
    assert follow_up["status"] == "blocked"
    assert "paused" in follow_up["reason"].lower()

    dashboard = client.get("/dashboard", params={"session_id": sid}).json()
    assert any(alert["alert_type"] == "COST" for alert in dashboard["alerts"])


def test_cache_returns_stored_result(client):
    sid = new_session(client)
    assert call(client, sid, args={"q": "A"}).json()["status"] == "allowed"
    stored = client.post(
        "/tool_result",
        json={"session_id": sid, "tool_name": "search_web", "arguments": {"q": "A"}, "result": {"hits": 3}},
    )
    assert stored.json()["status"] == "stored"

    assert call(client, sid, args={"q": "B"}).json()["status"] == "allowed"
    cached = call(client, sid, args={"q": "A"}).json()
    assert cached["status"] == "cached"
    assert cached["cached_result"] == {"hits": 3}


def test_unknown_session_returns_404(client):
    response = call(client, "does-not-exist")
    assert response.status_code == 404


def test_invalid_tool_name_is_rejected(client):
    sid = new_session(client)
    response = client.post(
        "/tool_call",
        json={"session_id": sid, "tool_name": "bad name!", "arguments": {}},
    )
    assert response.status_code == 422


def test_negative_tokens_are_rejected(client):
    sid = new_session(client)
    response = call(client, sid, tokens=-5)
    assert response.status_code == 422


def test_dashboard_without_sessions(client):
    body = client.get("/dashboard").json()
    assert body["session"] is None
    assert body["recent_calls"] == []


def test_reset_clears_everything(client):
    sid = new_session(client)
    call(client, sid)
    assert client.post("/reset").json()["status"] == "reset"
    assert client.get("/dashboard").json()["session"] is None
    assert call(client, sid).status_code == 404
