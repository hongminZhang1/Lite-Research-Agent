from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deep_research.benchmark import run_benchmark
from deep_research.lab import benchmark_choice, measured_conclusion, query_for, run_research, search_sources
from deep_research.lab_models import Budget, Endpoint, Gateway, Limits, Settings
from deep_research.tools.search import SearchResult


PRIM_TOPIC = "优化最小生成树的 Prim 算法"
PRIM_ABSTRACT = SearchResult(
    "Prim minimum spanning tree analysis",
    "https://papers.example.org/prim", "",
    abstract="The Prim algorithm grows a minimum spanning tree by choosing the cheapest crossing edge.",
)


def prim_model(self, prompt, *, stage, **kwargs):
    if stage == "规划":
        return {"subquestions": ["什么数据适合使用二叉堆？"], "queries": ["Prim MST binary heap"],
                "criterion": "核对正确性并衡量运行时间", "first": "search"}
    if stage == "验证与修正":
        return {"action": "finish", "supported": [{"claim": "Prim 选择最便宜的跨割边。", "refs": [1, 99]}],
                "gaps": ["仍需按图密度验证。"]}
    return {}


def test_prim_filters_off_topic_before_fetch_and_changes_query(monkeypatch):
    queried = []
    emitted = []

    def search(query, key, *, channel, **kwargs):
        queried.append((channel, query))
        if channel == "web":
            return [SearchResult(title, f"https://example.org/{i}", "") for i, title in
                    enumerate(["Amazon Prime", "Prim Definition | Dictionary", "British Airways"])]
        return [PRIM_ABSTRACT]

    def unexpected_fetch(*args):
        raise AssertionError("Off-topic pages and indexed abstracts must not be fetched")

    monkeypatch.setattr("deep_research.lab.search_sources", search)
    monkeypatch.setattr("deep_research.lab.fetch_text", unexpected_fetch)
    monkeypatch.setattr(Gateway, "complete_json", prim_model)
    result = run_research(PRIM_TOPIC, "general", Settings(), Limits(rounds=2), "adaptive", [],
                          lambda kind, data: emitted.append((kind, data)))
    assert result["status"] == "complete"
    first, second = result["rounds"]
    assert first["goal"] != second["goal"]
    assert [issue["kind"] for issue in first["attempts"][0]["issues"]] == ["irrelevant"] * 3
    assert sum(kind == "notice" and data["message"].startswith("已过滤：与课题无关") for kind, data in emitted) == 3
    assert queried[0][0] == "web" and queried[1][0] == "literature" and queried[0][1] != queried[1][1]
    assert all("Prim" in query and ("minimum spanning tree" in query or "MST" in query) for _, query in queried)
    assert queried[1][1] in first["next"]
    assert second["supported"][0]["refs"] == [1]
    assert result["sources"][0]["basis"] == "论文摘要"
    assert "Kruskal" in result["design"]["metric"] and not result["experiments"]


def test_prim_fetch_failure_is_deduplicated_and_recovers_through_reference(monkeypatch):
    timeout = SearchResult("Prim MST timeout", "https://journal.example.org/timeout", "")
    empty = SearchResult("Prim MST empty", "https://journal.example.org/empty", "")
    visited = []

    def fetch(url, budget):
        visited.append(url)
        return "", "timeout" if url == timeout.url else "empty_body"

    monkeypatch.setattr(Gateway, "complete_json", prim_model)
    monkeypatch.setattr("deep_research.lab.fetch_text", fetch)
    monkeypatch.setattr("deep_research.lab.search_sources",
                        lambda *args, channel, **kwargs: [timeout, empty] if channel == "web"
                        else [SearchResult(timeout.title, timeout.url+"?utm_source=mirror", ""), PRIM_ABSTRACT])
    result = run_research(PRIM_TOPIC, "general", Settings(), Limits(rounds=2), "adaptive", [], lambda *_: None)
    first, second = result["rounds"]
    failures = first["attempts"][0]["issues"]
    assert [issue["kind"] for issue in failures] == ["timeout", "empty_body"]
    assert "网络超时" in failures[0]["message"] and "正文提取为空" in failures[1]["message"]
    assert second["attempts"][0]["channel"] == "官方技术参考"
    assert visited == [timeout.url, empty.url] and second["sources"] == [1]


def test_budget_after_partial_evidence_preserves_audit_for_exports(monkeypatch):
    from deep_research.lab_models import BudgetStop
    from deep_research.report import latex_source

    def model(self, prompt, *, stage, **kwargs):
        if stage == "验证与修正":
            raise BudgetStop("已达到 Token 上限。")
        return prim_model(self, prompt, stage=stage, **kwargs)

    monkeypatch.setattr(Gateway, "complete_json", model)
    monkeypatch.setattr("deep_research.lab.search_sources", lambda *a, **k: [
        SearchResult("Amazon Prime", "https://example.org/irrelevant", ""), PRIM_ABSTRACT])
    result = run_research(PRIM_TOPIC, "general", Settings(), Limits(), "adaptive", [], lambda *_: None)
    assert result["status"] == "budget" and result["rounds"][0]["sources"] == [1]
    assert result["end_reason"] == "已达到 Token 上限。"
    tex = latex_source(result)
    assert all(fragment in tex for fragment in ("逐轮核查", "论文摘要", "已过滤：与课题无关", "下一步", "已达到 Token 上限"))


def test_fetch_diagnoses_http_and_content_errors(monkeypatch):
    import requests
    from deep_research.lab import fetch_text

    monkeypatch.setattr("deep_research.lab.socket.getaddrinfo", lambda *a, **k: [(None, None, None, None, ("1.1.1.1", 0))])
    budget = Budget(Limits(), lambda *_: None)

    for status, reason in ((403, "denied"), (429, "rate_limited"), (200, "empty_body")):
        response = requests.Response()
        response.status_code = status
        response._content = b"<html><main></main></html>"
        response._content_consumed = True
        monkeypatch.setattr("deep_research.lab.requests.get", lambda *a, **k: response)
        assert fetch_text("https://example.org/article", budget) == ("", reason)

    def timeout(*args, **kwargs):
        raise requests.Timeout()

    monkeypatch.setattr("deep_research.lab.requests.get", timeout)
    assert fetch_text("https://example.org/article", budget) == ("", "timeout")


def test_real_measurements_drive_revision():
    first = run_benchmark("direct")
    assert first["datasets"][0]["passed"]
    assert first["datasets"][1]["estimated_bytes"] > first["parameters"]["memory_limit_bytes"]
    assert not first["datasets"][1]["passed"]
    assert benchmark_choice([first], {"candidate": "sorted"})[0] == "compressed"
    second = run_benchmark("compressed")
    assert all(row["passed"] and row["candidate_ms"] > 0 for row in second["datasets"])
    first["id"], second["id"] = "E01", "E02"
    assert "E02（compressed）在两组数据上" in measured_conclusion([first, second])


def test_low_budget_returns_a_result():
    settings = Settings(fast=Endpoint(base="http://127.0.0.1:8081/v1", model="test"))
    result = run_research("行情事件分组聚合", "market", settings, Limits(tokens=64), "adaptive", [], lambda *_: None)
    assert result["status"] == "budget"
    assert not result["usage"]["calls"]


def test_search_empty_returns_a_result(monkeypatch):
    monkeypatch.setattr(Gateway, "complete_json", lambda *a, **k: {"queries": ["VGGT4D"], "criterion": "引用原论文"})
    monkeypatch.setattr("deep_research.lab.search_sources", lambda *a, **k: [])
    events = []
    result = run_research("VGGT4D", "general", Settings(), Limits(), "adaptive", [], lambda kind, data: events.append(kind))
    assert result["status"] == "no_evidence"
    assert "notice" in events
    assert result["sources"] == []


def test_query_keeps_model_name_and_doi():
    assert query_for("motion cues", "研究 VGGT4D") == "VGGT4D motion cues"
    assert query_for("论文对比", "研究 10.1234/abc.123。") == "10.1234/abc.123 论文对比"


def test_paper_fallback_uses_indexed_abstract(monkeypatch):
    import requests
    from deep_research.tools.search import SearchTool

    monkeypatch.setattr(SearchTool, "_search_arxiv_title", lambda *args, **kwargs: [])

    def get(url, **kwargs):
        assert url == "https://api.openalex.org/works"
        assert kwargs["params"]["search"] == "VGGT4D"
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"results":[{"display_name":"VGGT4D","doi":"https://doi.org/10.1234/test","abstract_inverted_index":{"Motion":[0],"cues":[1]}}]}'
        return response

    monkeypatch.setattr("deep_research.lab.requests.get", get)
    assert search_sources("VGGT4D motion cues", "")[0].abstract == "Motion cues"


def test_cloud_request_contract_and_routing(monkeypatch):
    events = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, *, headers, json):
            import httpx
            assert url == "https://example.com/v1/chat/completions"
            assert headers == {"Authorization": "Bearer testing-secret"}
            events.append(json["model"])
            return httpx.Response(200, json={"choices": [{"message": {"content": "test"}}],
                                             "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    monkeypatch.setattr("deep_research.lab_models.httpx.Client", Client)
    settings = Settings(**{tier: Endpoint(base="https://example.com/v1", model=tier, key="testing-secret",
                                          input_price=1 if tier == "fast" else 10, output_price=2 if tier == "fast" else 20)
                           for tier in ("fast", "strong")})
    budget = Budget(Limits(usd=.01), lambda *_: None)
    gateway = Gateway(settings, budget, "adaptive")
    gateway.complete("正常")
    gateway.complete("诊断", reason="连续失败")
    assert events == ["fast", "strong"]
    assert budget.snapshot()["usd"] == .00154
    low = Gateway(settings, Budget(Limits(usd=.001), lambda *_: None), "adaptive")
    low.complete("诊断", reason="连续失败")
    assert events[-1] == "fast"


def test_cost_breakdown_and_missing_inputs():
    endpoint = Endpoint(base="https://api.openai.com/v1", model="test", key="test", input_price=2,
                        output_price=8, cache_price=.5, rate_source="测试费率")
    usage = {"prompt_tokens": 1000, "completion_tokens": 100, "prompt_tokens_details": {"cached_tokens": 400},
             "completion_tokens_details": {"reasoning_tokens": 50}}
    row = endpoint.billing_details(usage, 5000, 180, None)
    assert row["usd"] == .0022 and row["cached_input"] == 400 and not row["usage_estimated"]
    missing = endpoint.billing_details({"prompt_tokens": 1000}, 5000, 180, None)
    assert missing["usd"] is None and missing["input"] == 1000 and missing["output"] == 180 and missing["usage_reason"]
    unpriced = endpoint.model_copy(update={"output_price": None}).billing_details(usage, 5000, 180, None)
    assert unpriced["usd"] is None and "费率" in unpriced["cost_reason"]
    budget = Budget(Limits(), lambda *_: None)
    budget.calls = [dict(x, stage="规划", seconds=1) for x in (row, missing, unpriced)]
    assert budget.snapshot()["known_usd"] == .0022 and budget.snapshot()["unpriced_calls"] == 2


def test_tex_escapes_content_and_keeps_usage_in_appendix():
    from deep_research.report import latex_source

    tex = latex_source({"question": r"研究 \input{unsafe} 与 10%_收益", "scenario": "general", "summary": "有文献证据 [1]。",
                        "sources": [{"title": "题名", "url": "https://example.com/a?q=x#page", "content": "材料"}],
                        "design": dict(hypothesis="假设", control="原版", variables="变量", metric="指标", criterion="标准")})
    assert r"\input{unsafe}" not in tex and r"10\%\_收益" in tex
    assert "Token" not in tex.split(r"\end{abstract}")[0]
    assert "后续实验设计（待验证）" in tex and "运行与费用" in tex and r"\hyperlink{ref1}{[1]}" in tex


def test_literature_excerpt_keeps_paper_contribution():
    from deep_research.lab import paper_excerpt
    assert paper_excerpt("Earlier models struggled. We propose VGGT4D. It separates moving objects.",
                         "VGGT4D: Motion cues") == "We propose VGGT4D. It separates moving objects."
