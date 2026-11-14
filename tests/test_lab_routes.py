"""Model and session contracts through the actual web endpoints."""
import json
import threading
from contextlib import closing
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web import lab_routes
from web import server


SECRET = "fake-sol-project-key-for-test"
SOL = {"base": "https://api.openai.com/v1", "model": "gpt-6.1-sol", "key": SECRET,
       "input_price": 2, "output_price": 10, "cache_price": 0.1}
LOCAL = {"base": "http://127.0.0.1:8081/v1", "model": "qwen2.5-3b-instruct"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = str(tmp_path / "sessions.db")
    monkeypatch.setenv("SESSIONS_DB", db)
    monkeypatch.delenv("RESEARCH_CLOUD_ONLY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(lab_routes, "LOCAL_CONFIG", tmp_path / ".env")
    monkeypatch.setattr(lab_routes, "WORKSPACE_CONFIG", tmp_path / "other.env")
    monkeypatch.setattr(server, "_DB_PATH", db)
    with TestClient(server.app) as app:
        yield app


def research_request(settings, sid=None):
    return {"question": "如何加速行情事件聚合？", "scenario": "market", "settings": settings,
            "limits": {"rounds": 2, "tokens": 12000}, "session_id": sid}


def receive_run(ws):
    records = []
    while True:
        event = ws.receive_json()
        assert event["type"] != "error", event
        if event["type"] == "result":
            records.append(event["data"])
        if event["type"] == "complete":
            return event["session_id"], records


def model_transport(monkeypatch, handler):
    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    stub = SimpleNamespace(Client=lambda **kwargs: real_client(transport=transport, **kwargs),
                           TimeoutException=httpx.TimeoutException, HTTPError=httpx.HTTPError)
    monkeypatch.setattr("deep_research.lab_models.httpx", stub)


def test_sol_connection_chinese_research_and_local_upgrade(client, monkeypatch):
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path == "/v1/chat/completions"
        if body["model"] == SOL["model"]:
            assert request.url.host == "api.openai.com"
            assert request.headers["Authorization"] == "Bearer " + SECRET
            assert body["reasoning_effort"] == "low" and body["max_completion_tokens"] >= 128
            assert "temperature" not in body and "max_tokens" not in body
        else:
            assert body["model"] == LOCAL["model"]
            assert "temperature" in body and "max_tokens" in body
            assert "max_completion_tokens" not in body and "reasoning_effort" not in body
        prompt = body["messages"][-1]["content"]
        answer = "连接成功"
        if "研究问题：" in prompt:
            answer = "{}" if body["model"] == LOCAL["model"] else json.dumps(
                {"queries": ["行情聚合"], "criterion": "逐组验证", "subquestions": ["如何减少分桶空间？"]}, ensure_ascii=False)
        elif "本轮新增证据" in prompt:
            answer = json.dumps({"candidate": "compressed", "hypothesis": "测试" + SECRET}, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 25,
                      "prompt_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 20},
                      "completion_tokens_details": {"reasoning_tokens": 12}}})

    model_transport(monkeypatch, respond)
    for tier in ("fast", "strong"):
        check = client.post(f"/api/lab/check/{tier}", json={tier: SOL}).json()
        assert check["ok"], check
        assert check["usage"]["tokens"] == 125
        assert check["usage"]["usd"] == pytest.approx((70 * 2 + 10 * .1 + 20 * 2.5 + 25 * 10) / 1_000_000)
        with client.websocket_connect("/ws/research") as ws:
            ws.send_json({**research_request({tier: SOL}), "policy": "strong" if tier == "strong" else "adaptive"})
            sid, (result,) = receive_run(ws)
        assert result["status"] == "complete"
        assert [x["candidate"] for x in result["experiments"]] == ["direct", "compressed"]
        assert {call["model"] for call in result["usage"]["calls"]} == {SOL["model"]}
        assert SECRET not in client.get(f"/api/lab/sessions/{sid}").text

    with client.websocket_connect("/ws/research") as ws:
        ws.send_json(research_request({"fast": LOCAL, "strong": SOL}))
        _, (result,) = receive_run(ws)
    assert result["status"] == "complete"
    routing = [(call["stage"], call["model"]) for call in result["usage"]["calls"]]
    assert routing[:3] == [("规划", LOCAL["model"]), ("规划", SOL["model"]), ("假设", LOCAL["model"])]
    assert any("如何加速行情事件聚合" in msg["content"] for call in calls for msg in call["messages"])


@pytest.mark.parametrize(("status", "code", "expected"), [
    (401, "invalid_api_key", "身份验证失败"), (403, "model_not_found", "调用权限"),
    (403, "unsupported_country_region_territory", "国家或地区"), (429, "credit_balance_exhausted", "额度不足"),
    (429, "rate_limit_exceeded", "限流"), (400, "unsupported_parameter", "参数不兼容"),
    (None, None, "无法连接模型 API"),
])
def test_sol_connection_errors(client, monkeypatch, status, code, expected):
    def respond(request):
        if status is None:
            raise httpx.ConnectError("fixture network unavailable", request=request)
        return httpx.Response(status, json={"error": {"code": code, "message": SECRET}})

    model_transport(monkeypatch, respond)
    response = client.post("/api/lab/check/fast", json={"fast": SOL})
    assert not response.json()["ok"]
    assert expected in response.json()["message"]
    assert SECRET not in response.text


def test_deepseek_default_uses_server_key_only_on_official_endpoint(client, monkeypatch, tmp_path):
    local_config = tmp_path / ".env"
    secret = "fake-deepseek-project-key"
    local_config.write_text(f"DEEPSEEK_API_KEY={secret}\n", encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(lab_routes, "LOCAL_CONFIG", local_config)
    calls = []
    local_response = "valid"

    def respond(request):
        calls.append((request.url.host, request.headers.get("Authorization")))
        body = json.loads(request.content)
        if request.url.host == "api.deepseek.com":
            assert body["model"] == "deepseek-v4-pro"
            assert body["thinking"] == {"type": "disabled"}
        answer = json.dumps({"queries": ["行情聚合"], "criterion": "验证结果"}, ensure_ascii=False)
        if request.url.host == "127.0.0.1" and local_response == "unavailable":
            raise httpx.ConnectError("fixture local model unavailable", request=request)
        if request.url.host == "127.0.0.1" and local_response == "invalid":
            answer = "{}"
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 10,
                                                   "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 10}})

    model_transport(monkeypatch, respond)
    response = client.get("/api/lab/defaults")
    assert response.json()["fast"] == LOCAL and response.json()["deepseek_configured"]
    preset = response.json()["strong"]
    assert preset["model"] == "deepseek-v4-pro"
    assert secret not in response.text and "key" not in preset
    for base in (preset["base"], preset["base"] + "/v1/"):
        assert client.post("/api/lab/check/strong", json={"strong": {**preset, "base": base, "key": " "}}).json()["ok"]
        assert calls[-1] == ("api.deepseek.com", f"Bearer {secret}")
    assert client.post("/api/lab/check/fast", json={}).json()["ok"]
    assert not client.post("/api/lab/check/strong", json={"strong": {**preset, "base": "https://example.org/v1"}}).json()["ok"]
    assert calls[-1] == ("127.0.0.1", None)

    for mode, budget, expected in (("valid", None, 0), ("invalid", 0, 0), ("invalid", None, 1), ("unavailable", None, 0)):
        local_response = mode
        request = research_request({})
        request["limits"]["usd"] = budget
        with client.websocket_connect("/ws/research") as ws:
            ws.send_json(request)
            sid, (result,) = receive_run(ws)
        rows = result["usage"]["calls"]
        assert rows[0]["model"] == LOCAL["model"]
        cloud = [row for row in rows if row["tier"] == "strong"]
        assert len(cloud) == expected
        if cloud:
            assert cloud[0]["model"] == preset["model"]
            assert cloud[0]["reason"] and cloud[0]["usd"] > 0
        if mode == "unavailable":
            assert result["status"] == "model_error" and "检查 Qwen" in result["detail"]
        else:
            assert result["status"] == "complete"
        assert secret not in client.get(f"/api/lab/sessions/{sid}").text


@pytest.mark.parametrize(("settings", "limits", "expected"), [
    ({}, {"rounds": 0}, "轮数必须"),
    ({}, {"seconds": None}, "时间预算必须"),
    ({}, {"tokens": True}, "Token 预算必须"),
    ({}, {"usd": -1}, "费用预算必须"),
    ({"strong": {"base": "https://api.deepseek.com/v1", "model": "deepseek-v4-pro"}}, {}, "云端强模型缺少"),
    ({"fast": {"base": "http://127.0.0.1:bad", "model": "dummy"}}, {}, "普通模型的 API 地址无效"),
])
def test_research_validation_reports_field_without_echoing_input(client, settings, limits, expected):
    request = research_request(settings)
    request["limits"] = limits
    request["question"] = SECRET
    with client.websocket_connect("/ws/research") as ws:
        ws.send_json(request)
        result = ws.receive_json()
    assert result["code"] == "validation"
    assert expected in result["message"]
    assert SECRET not in result["message"]


def test_active_session_and_old_downloads_after_delete(client, monkeypatch):
    sid, other = "a00000000001", "a00000000002"
    rid, followup, other_rid = "b00000000001", "b00000000002", "b00000000003"
    for session_id, run_id in ((sid, rid), (sid, followup), (other, other_rid)):
        lab_routes.save_run(session_id, {"id": run_id, "question": "删除验收临时课题"})
    with closing(lab_routes.db()) as connection, connection:
        connection.execute("UPDATE sessions SET data=? WHERE id=?", (json.dumps({"turns": [{"content": "旧追问"}]}), sid))

    entered, release = threading.Event(), threading.Event()

    def fake_run(question, scenario, settings, limits, policy, history, emit):
        entered.set()
        assert release.wait(timeout=8)
        return {"question": question}

    monkeypatch.setattr(lab_routes, "run_research", fake_run)
    with client.websocket_connect("/ws/research") as ws:
        ws.send_json(research_request({"fast": LOCAL}, sid))
        assert entered.wait(timeout=5)
        try:
            assert client.delete(f"/api/lab/sessions/{sid}").status_code == 409
            assert client.delete(f"/api/lab/sessions/{sid}", headers={"Origin": "https://example.com"}).status_code == 403
        finally:
            release.set()
        _, (last_run,) = receive_run(ws)

    assert client.delete(f"/api/lab/sessions/{sid}").status_code == 204
    assert client.get(f"/api/lab/sessions/{sid}").status_code == 404
    assert client.delete(f"/api/lab/sessions/{sid}").status_code == 404
    for run_id in (rid, followup, last_run["id"]):
        assert client.get(f"/api/lab/runs/{run_id}.json").status_code == 404
        for extension in ("pdf", "tex"):
            assert client.get(f"/api/lab/reports/{run_id}.{extension}").status_code == 404
    assert [item["id"] for item in client.get("/api/lab/sessions").json()] == [other]
    assert client.get(f"/api/lab/runs/{other_rid}.json").status_code == 200
    with client.websocket_connect("/ws/research") as ws:
        ws.send_json(research_request({"fast": LOCAL}, sid))
        assert "已删除" in ws.receive_json()["message"]


def test_legacy_websocket_cannot_restore_deleted_session(client, monkeypatch):
    class Conversation:
        session_id = "a00000000003"

        @classmethod
        def load(cls, sid, db):
            return cls() if lab_routes.session_exists(sid) else None

        def handle_message(self, *args):
            return {"answer": "测试回答"}

        def save(self, *args):
            pass

    monkeypatch.setattr(server, "ConversationSession", Conversation)
    lab_routes.save_run(Conversation.session_id, {"id": "b00000000004", "question": "旧版临时课题"})
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"message": "旧版追问", "session_id": Conversation.session_id})
        assert ws.receive_json()["type"] == "session"
        assert ws.receive_json()["type"] == "turn_done"
        assert client.delete(f"/api/lab/sessions/{Conversation.session_id}").status_code == 204
        ws.send_json({"message": "删除后的旧版追问"})
        assert "已删除" in ws.receive_json()["message"]
    assert client.get(f"/api/lab/sessions/{Conversation.session_id}").status_code == 404
