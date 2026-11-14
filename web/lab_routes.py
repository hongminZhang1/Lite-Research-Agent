"""Local web UI endpoints. Credentials are scoped to one request or socket run."""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError

from deep_research.agent.conversation import _connect
from deep_research.lab import run_research, search_sources
from deep_research.lab_models import Budget, BudgetStop, Gateway, Limits, ModelError, Settings
from deep_research.report import PdfError, latex_source, pdf_bytes


router = APIRouter()
ID = re.compile(r"^[a-f0-9]{12}$")
BUSY_SESSION = "该会话正在执行研究或删除，请稍后重试。"
_busy_sessions: set[str] = set()
LOCAL_CONFIG = Path(__file__).resolve().parents[1] / ".env"
WORKSPACE_CONFIG = LOCAL_CONFIG.parent.parent / "ResearchAgent" / ".env"
DEEPSEEK_BASE = "https://api.deepseek.com"
DEEPSEEK_ENDPOINTS = {DEEPSEEK_BASE, f"{DEEPSEEK_BASE}/v1"}
QWEN_DEFAULT = {"base": "http://127.0.0.1:8081/v1", "model": "qwen2.5-3b-instruct"}
DEEPSEEK_DEFAULT = {
    "base": DEEPSEEK_BASE, "model": "deepseek-v4-pro",
    "input_price": 1.32, "output_price": 3.96, "cache_price": 0.044,
    "rate_source": "DeepSeek 官方高峰费率（预算上限估算）", "billing": "deepseek",
}


def deepseek_key():
    for config in (LOCAL_CONFIG, WORKSPACE_CONFIG):
        value = dotenv_values(config, encoding="utf-8-sig", interpolate=False).get("DEEPSEEK_API_KEY")
        if value and value.strip():
            return value.strip()
    return os.getenv("DEEPSEEK_API_KEY", "").strip()


def tavily_key():
    for config in (LOCAL_CONFIG, WORKSPACE_CONFIG):
        value = dotenv_values(config, encoding="utf-8-sig", interpolate=False).get("TAVILY_API_KEY")
        if value and value.strip():
            return value.strip()
    return os.getenv("TAVILY_API_KEY", "").strip()


def model_defaults(key):
    if os.getenv("RESEARCH_CLOUD_ONLY") == "1":
        return {"fast": DEEPSEEK_DEFAULT if key else None, "strong": None}
    return {"fast": QWEN_DEFAULT, "strong": DEEPSEEK_DEFAULT if key else None}


def resolve_model_settings(body, *, default_tiers):
    if not isinstance(body, dict):
        return body
    resolved = dict(body)
    key = deepseek_key()
    defaults = model_defaults(key)
    for kind in default_tiers:
        if kind not in resolved:
            resolved[kind] = defaults[kind]
    for kind in ("fast", "strong"):
        model = resolved.get(kind)
        if not isinstance(model, dict) or not isinstance(model.get("base"), str):
            continue
        if model["base"].strip().rstrip("/") in DEEPSEEK_ENDPOINTS:
            # Normalize the official compatibility URL before adding a server credential.
            override = model.get("key")
            if isinstance(override, str):
                override = override.strip()
            if override is None or override == "":
                override = key
            resolved[kind] = {**model, "base": DEEPSEEK_BASE, "key": override}
    if not resolved.get("search_key"):
        resolved["search_key"] = tavily_key()
    return resolved


VALIDATION_MESSAGES = {
    "question": "请输入研究问题，最多 2500 个字符。",
    "scenario": "请选择有效的研究场景。",
    "policy": "请选择有效的调用策略。",
    "session_id": "研究会话编号无效，请重新打开会话。",
    "limits": "研究预算必须包含有效的数字。",
    "limits.rounds": "轮数必须是 1 到 20 之间的整数。",
    "limits.seconds": "时间预算必须是 5 或以上的整数秒，也可以留空表示不限制。",
    "limits.tokens": "Token 预算必须是 64 或以上的整数，也可以留空表示不限制。",
    "limits.usd": "费用预算必须是 0 或以上的数字，也可以留空。",
    "settings.search_key": "搜索服务的密钥格式无效。",
    "settings.search_credit_price": "搜索费率必须在 0 到 100 美元之间。",
}


def validation_message(error, prefix=()):
    messages = []
    for issue in error.errors(include_input=False, include_context=False, include_url=False):
        location = (*prefix, *issue["loc"])
        field = ".".join(map(str, location))
        message = VALIDATION_MESSAGES.get(field, "请求格式无效，请重新加载页面后再试。")
        if len(location) >= 2 and location[0] == "settings" and location[1] in {"fast", "strong"}:
            label = "普通模型" if location[1] == "fast" else "强模型"
            kind = issue["type"]
            detail = location[-1]
            if kind == "model_key_missing" or detail == "key":
                message = f"云端{label}缺少有效的 API Key。"
            elif kind == "model_url" or detail == "base":
                message = f"{label}的 API 地址无效，请填写 HTTPS 或本机 HTTP 地址。"
            elif kind == "model_name" or detail == "model":
                message = f"请填写有效的{label}名称。"
            elif detail in {"input_price", "output_price", "cache_price"}:
                message = f"{label}的费率必须在 0 到 100000 之间，也可以留空。"
            else:
                message = f"{label}设置无效，请检查模型地址、名称和计费设置。"
        if message not in messages:
            messages.append(message)
    return " ".join(messages[:3])


@contextmanager
def session_operation(sid):
    """Serialize writers for each session on the app's single event loop."""
    if sid in _busy_sessions:
        raise HTTPException(409, BUSY_SESSION)
    _busy_sessions.add(sid)
    try:
        yield
    finally:
        _busy_sessions.remove(sid)


def db():
    connection = _connect(os.getenv("SESSIONS_DB", "sessions.db"))
    connection.execute("CREATE TABLE IF NOT EXISTS research_runs (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
                       "created TEXT NOT NULL, data TEXT NOT NULL)")
    connection.execute("CREATE INDEX IF NOT EXISTS research_runs_session ON research_runs(session_id, created)")
    return connection


def same_origin(headers):
    origin = headers.get("origin")
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.netloc == headers.get("host")


def validate_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise HTTPException(404, "会话或报告不存在。")


def redact_payload(value, settings):
    if isinstance(value, dict):
        return {key: redact_payload(item, settings) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_payload(item, settings) for item in value]
    return settings.redact(value) if isinstance(value, str) else value


class TurnRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2500)
    scenario: str = Field(pattern="^(market|general)$")
    policy: str = Field(default="adaptive", pattern="^(adaptive|strong|compare)$")
    session_id: str | None = Field(default=None, pattern=ID.pattern)
    settings: Settings
    limits: Limits = Field(default_factory=Limits)


@router.get("/api/lab/defaults")
def defaults():
    key = deepseek_key()
    tkey = tavily_key()
    return {**model_defaults(key), "deepseek_configured": bool(key),
            "search_key": tkey, "tavily_configured": bool(tkey)}


@router.post("/api/lab/check/{kind}")
async def check(kind: str, request: Request):
    if not same_origin(request.headers):
        raise HTTPException(403, "来源不匹配。")
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Invalid settings")
        if kind == "search":
            try:
                settings = Settings.model_validate({"search_key": body.get("search_key", "") or tavily_key()})
            except ValidationError as error:
                return {"ok": False, "message": validation_message(error, ("settings",))}
            results = await asyncio.to_thread(search_sources, "VGGT4D", settings.search_key.get_secret_value(), 2)
            return {"ok": bool(results), "message": f"检索返回 {len(results)} 个来源。",
                    "sources": [settings.redact(r.title) for r in results]}
        if kind not in {"fast", "strong"}:
            raise HTTPException(404, "未知连接类型。")
        endpoint = {kind: body[kind]} if kind in body else {}
        resolved = resolve_model_settings(endpoint, default_tiers=(kind,))
        try:
            settings = Settings.model_validate({kind: resolved.get(kind)})
        except ValidationError as error:
            return {"ok": False, "message": validation_message(error, ("settings",))}
        if getattr(settings, kind) is None:
            return {"ok": False, "message": "请先填写对应模型的地址、模型名和密钥。"}
        budget = Budget(Limits(seconds=35, tokens=1000), lambda *_: None)
        gateway = Gateway(settings, budget, "strong" if kind == "strong" else "adaptive")
        await asyncio.to_thread(gateway.complete, "只回复连接成功。", max_tokens=64, stage="连接测试")
        return {"ok": True, "message": "模型已返回内容。", "usage": budget.snapshot()}
    except (BudgetStop, ModelError) as error:
        return {"ok": False, "message": str(error)}
    except ValueError:
        return {"ok": False, "message": "请检查地址、模型名、密钥和费率格式。"}


@router.get("/api/lab/sessions")
def sessions():
    with closing(db()) as connection:
        rows = connection.execute("SELECT id, topic, updated_at FROM sessions WHERE topic IS NOT NULL "
                                  "ORDER BY updated_at DESC LIMIT 100").fetchall()
    return [{"id": sid, "title": title, "updated": updated} for sid, title, updated in rows]


def read_session(sid):
    with closing(db()) as connection, connection:
        connection.execute("BEGIN")
        old = connection.execute("SELECT data FROM sessions WHERE id=?", (sid,)).fetchone()
        records = connection.execute("SELECT data FROM research_runs WHERE session_id=? ORDER BY created, rowid", (sid,)).fetchall()
    return (json.loads(old[0]) if old else None), [json.loads(record[0]) for record in records]


def session_exists(sid):
    with closing(db()) as connection:
        return connection.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone() is not None


@router.get("/api/lab/sessions/{sid}")
def session(sid: str):
    validate_id(sid)
    original, runs = read_session(sid)
    if original is None:
        raise HTTPException(404, "会话不存在或已删除。")
    return {"turns": original.get("turns", []), "runs": runs}


def delete_records(sid):
    with closing(db()) as connection, connection:
        connection.execute("DELETE FROM research_runs WHERE session_id=?", (sid,))
        if not connection.execute("DELETE FROM sessions WHERE id=?", (sid,)).rowcount:
            raise HTTPException(404, "会话不存在或已删除。")


@router.delete("/api/lab/sessions/{sid}", status_code=204)
async def delete_session(sid: str, request: Request):
    if not same_origin(request.headers):
        raise HTTPException(403, "来源不匹配。")
    validate_id(sid)
    with session_operation(sid):
        await asyncio.to_thread(delete_records, sid)
    return Response(status_code=204)


@router.get("/api/lab/reports/{rid}.pdf")
def report(rid: str):
    data = report_record(rid)
    try:
        content = pdf_bytes(data)
    except PdfError as error:
        raise HTTPException(503, str(error)) from None
    return Response(content, media_type="application/pdf",
                    headers={"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="research-{rid}.pdf"'})


@router.get("/api/lab/reports/{rid}.tex")
def report_tex(rid: str):
    return Response(latex_source(report_record(rid)), media_type="text/plain",
                    headers={"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="research-{rid}.tex"'})


def report_record(rid):
    validate_id(rid)
    with closing(db()) as connection:
        row = connection.execute("SELECT data FROM research_runs WHERE id=?", (rid,)).fetchone()
    if not row:
        raise HTTPException(404, "报告不存在。")
    return json.loads(row[0])


@router.get("/api/lab/runs/{rid}.json")
def run_json(rid: str):
    validate_id(rid)
    with closing(db()) as connection:
        row = connection.execute("SELECT data FROM research_runs WHERE id=?", (rid,)).fetchone()
    if not row:
        raise HTTPException(404, "实验记录不存在。")
    return Response(row[0], media_type="application/json",
                    headers={"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="run-{rid}.json"'})


def save_run(sid: str, run: dict):
    now = datetime.now(timezone.utc).isoformat()
    with closing(db()) as connection, connection:
        connection.execute("INSERT INTO sessions(id, topic, data, updated_at) VALUES (?, ?, '{}', ?) "
                           "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at", (sid, run["question"], now))
        connection.execute("INSERT INTO research_runs(id,session_id,created,data) VALUES (?,?,?,?)",
                           (run["id"], sid, now, json.dumps(run, ensure_ascii=False)))


def prior_records(sid):
    old, history = read_session(sid)
    if old is None:
        raise HTTPException(404, "会话不存在或已删除，请开始新研究。")
    if old.get("findings"):
        historical = {"question": old.get("topic") or "既有研究", "sources": [
            {"title": row["source_title"], "url": row["source_url"], "content": row["content"], "basis": "既有会话"}
            for row in old["findings"]]}
        history.insert(0, historical)
    return history


@router.websocket("/ws/research")
async def research(ws: WebSocket):
    if not same_origin(ws.headers):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        body = await ws.receive_json()
        if isinstance(body, dict):
            body["settings"] = resolve_model_settings(body.get("settings", {}), default_tiers=("fast", "strong"))
            if isinstance(body.get("question"), str):
                body["question"] = body["question"].strip()
        args = TurnRequest.model_validate(body)
        question = args.settings.redact(args.question.strip())
        if not question:
            await ws.send_json({"type": "error", "message": "请输入研究问题。"})
            return
        if args.policy in {"strong", "compare"} and args.settings.strong is None:
            await ws.send_json({"type": "error", "message": "请先配置强模型。"})
            return
        if args.policy == "compare" and args.settings.fast is None:
            await ws.send_json({"type": "error", "message": "对照需要同时配置普通模型和强模型。"})
            return
        if args.settings.fast is None and args.settings.strong is None:
            await ws.send_json({"type": "error", "message": "请先在设置中配置模型。"})
            return
        sid = args.session_id or uuid.uuid4().hex[:12]
        with session_operation(sid):
            history = await asyncio.to_thread(prior_records, sid) if args.session_id else []
            terminal = await stream_research(ws, args, sid, question, history)
        await ws.send_json(terminal)
    except HTTPException as error:
        await ws.send_json({"type": "error", "message": error.detail})
    except ValidationError as error:
        await ws.send_json({"type": "error", "message": validation_message(error), "code": "validation"})
    except ValueError:
        await ws.send_json({"type": "error", "message": "研究请求不是有效的 JSON，请重新加载页面后再试。", "code": "validation"})
    except WebSocketDisconnect:
        return
    finally:
        if ws.client_state.name != "DISCONNECTED":
            try:
                await ws.close()
            except (RuntimeError, OSError):
                pass


async def stream_research(ws, args, sid, question, history):
    group = uuid.uuid4().hex[:12]
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def run():
        for policy in ("adaptive", "strong") if args.policy == "compare" else (args.policy,):
            def emit(kind, data):
                packet = {"type": kind, "data": redact_payload(data, args.settings), "policy": policy}
                loop.call_soon_threadsafe(queue.put_nowait, packet)
            emit("policy_start", {})
            result = run_research(question, args.scenario, args.settings, args.limits, policy, history, emit)
            result = redact_payload(result, args.settings)
            result.update(id=uuid.uuid4().hex[:12], group=group)
            save_run(sid, result)
            emit("result", result)

    async def process():
        try:
            await asyncio.to_thread(run)
        except Exception:
            await queue.put({"type": "error", "message": "无法保存或完成研究，请检查服务状态。"})
        else:
            await queue.put({"type": "complete", "session_id": sid})

    task = asyncio.create_task(process())
    try:
        while True:
            packet = await queue.get()
            if packet["type"] in {"complete", "error"}:
                return packet
            await ws.send_json(packet)
    finally:
        await task
