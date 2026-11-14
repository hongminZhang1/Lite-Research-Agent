"""
搜索工具 (Search Tool)
======================

设计意图
--------
Agent 的"眼睛"。把"如何联网搜索"封装成一个干净的函数 `search(query) -> [SearchResult]`,
Agent 业务层完全不需要知道底层用的是 DuckDuckGo 还是别的引擎。

为什么默认用 DuckDuckGo(面试谈资)
--------------------------------
免费、无需 API key,任何人 clone 下来就能跑,演示零门槛。代价是结果质量和稳定性
不如商用 API(Tavily / SerpAPI)。在 `SearchTool` 里我特意把"引擎"做成可替换的,
就是为了表达一个观点:**工具层应该可插拔**,生产环境换成 Tavily 只需实现同一个接口。

鲁棒性
------
搜索是最容易出问题的外部依赖(限流、网络抖动、引擎抽风)。所以这里:
- 有超时控制
- 有重试
- 失败时返回空列表而不是抛异常 —— 让上层 Agent 能"优雅降级"(把这个子问题
  标记为信息不足),而不是整个研究流程崩溃。这是面试官爱问的"工具挂了怎么办"。
"""

from __future__ import annotations

import os
import re
import time
from time import monotonic
from xml.etree import ElementTree
from urllib.parse import quote
from dataclasses import dataclass


def model_name(text: str) -> str | None:
    match = re.search(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9.-]*\d[A-Z0-9.-]*(?![A-Za-z0-9])", text)
    return match.group().rstrip(".-") if match else None


@dataclass
class SearchResult:
    """单条搜索结果。只保留 Agent 真正需要的字段,保持接口干净。"""

    title: str
    url: str
    snippet: str  # 搜索引擎给的简短摘要，用于初筛
    abstract: str | None = None  # 论文索引直接提供的原始摘要


class SearchTool:
    """搜索工具。默认使用 DuckDuckGo(通过 ddgs 库)。

    用法::

        tool = SearchTool()
        results = tool.search("open source vector databases comparison", k=5)
    """

    def __init__(self, max_retries: int = 2, retry_delay: float = 1.0) -> None:
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    def search(self, query: str, k: int = 5) -> list[SearchResult]:
        """执行搜索,返回最多 k 条结果。

        失败时(重试耗尽)返回空列表,由上层决定如何降级处理。
        """
        if os.getenv("SEARCH_PROVIDER") == "bing-rss":
            return self._search_public_sources(query, k)
        try:
            # ddgs 是 duckduckgo_search 的后继库。延迟导入,避免没装时影响其它模块。
            from ddgs import DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS  # 兼容旧库名
            except ImportError as err:
                raise RuntimeError(
                    "未安装搜索依赖。请 `pip install ddgs`(或 duckduckgo_search)。"
                ) from err

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                results: list[SearchResult] = []
                with DDGS() as ddgs:
                    for item in ddgs.text(query, max_results=k):
                        results.append(
                            SearchResult(
                                title=item.get("title", ""),
                                url=item.get("href", "") or item.get("url", ""),
                                snippet=item.get("body", ""),
                            )
                        )
                return results
            except Exception as err:  # noqa: BLE001
                last_err = err
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay)
                continue

        # 重试耗尽:不抛异常,返回空列表,让 Agent 优雅降级。
        print(f"[SearchTool] 搜索失败,已降级返回空结果。query={query!r} err={last_err}")
        return []


    def _search_public_sources(self, query: str, k: int, timeout: float = 24) -> list[SearchResult]:
        deadline = monotonic() + timeout
        def try_source(function, term):
            remaining = deadline - monotonic()
            return function(term, k, timeout=min(8, remaining)) if remaining > .1 else []

        term = model_name(query)
        if term:
            papers = try_source(self._search_arxiv_title, term)
            if papers:
                return papers
            results = try_source(self._search_bing_rss, term)
            if results:
                return results
        return try_source(self._search_bing_rss, query) or try_source(self._search_wikipedia, query)

    def _search_arxiv_title(self, term: str, k: int, timeout: float = 8) -> list[SearchResult]:
        import requests

        atom = {"atom": "http://www.w3.org/2005/Atom"}
        try:
            response = requests.get(
                "https://export.arxiv.org/api/query",
                params={"search_query": f"ti:{term}", "max_results": k},
                timeout=timeout,
            )
            response.raise_for_status()
            entries = ElementTree.fromstring(response.content).findall("atom:entry", atom)
        except (requests.RequestException, ElementTree.ParseError) as error:
            print(f"[SearchTool] arXiv 论文检索失败，将尝试网页搜索：{type(error).__name__}")
            return []
        results = []
        for entry in entries:
            title = " ".join(entry.findtext("atom:title", "", atom).split())
            paper_id = entry.findtext("atom:id", "", atom).rsplit("/", 1)[-1]
            if title and re.fullmatch(r"\d{4}\.\d{4,5}(?:v\d+)?", paper_id):
                url = f"https://arxiv.org/abs/{paper_id}"
                summary = " ".join(entry.findtext("atom:summary", "", atom).split())
                results.append(SearchResult(title, url, summary, abstract=summary or None))
        return results

    def _search_bing_rss(self, query: str, k: int, timeout: float = 8) -> list[SearchResult]:
        import requests

        try:
            response = requests.get(
                "https://www.bing.com/search",
                params={"q": query, "format": "rss"},
                timeout=timeout,
            )
            response.raise_for_status()
            items = ElementTree.fromstring(response.content).findall("./channel/item")
        except (requests.RequestException, ElementTree.ParseError):
            return []
        return [
            SearchResult(
                title=item.findtext("title", ""),
                url=item.findtext("link", ""),
                snippet=item.findtext("description", ""),
            )
            for item in items[:k]
            if item.findtext("link", "").startswith(("https://", "http://"))
        ]

    def _search_wikipedia(self, query: str, k: int, timeout: float = 8) -> list[SearchResult]:
        import requests

        language = "zh" if any("\u4e00" <= char <= "\u9fff" for char in query) else "en"
        origin = f"https://{language}.wikipedia.org"
        try:
            response = requests.get(
                f"{origin}/w/rest.php/v1/search/page",
                params={"q": query, "limit": k},
                headers={"User-Agent": "Lite-Research-Agent/1.0 (local research demo)"},
                timeout=timeout,
            )
            response.raise_for_status()
            pages = response.json().get("pages", [])
        except (requests.RequestException, ValueError):
            return []
        return [
            SearchResult(page["title"], f"{origin}/wiki/{quote(page['key'])}", page.get("description") or "")
            for page in pages
        ]
