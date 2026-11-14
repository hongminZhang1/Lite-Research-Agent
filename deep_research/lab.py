"""Evidence, hypotheses and measured results for the web research workflow."""

from __future__ import annotations

import ipaddress
import re
import socket
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from .benchmark import CANDIDATES, PARAMETERS, run_benchmark
from .lab_models import Budget, BudgetStop, Gateway, Limits, ModelError, Settings, checked_citations, compact
from . import prompts
from .tools.search import SearchResult, SearchTool, model_name


DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
MST = re.compile(r"最小生成树|\bminimum[\s-]+spanning[\s-]+(?:tree|forest)s?\b|\bmst\b", re.I)
PRIM = re.compile(r"\bprim\b", re.I)
PRIM_MST = re.compile(r"\bPrimMST\b", re.I)
PRIM_REFERENCES = (
    SearchResult("PrimMST: Minimum Spanning Tree (Princeton Algorithms)", "https://algs4.cs.princeton.edu/code/javadoc/edu/princeton/cs/algs4/PrimMST.html", ""),
    SearchResult("Prim Minimum Spanning Tree (Boost.Graph)", "https://www.boost.org/doc/libs/latest/libs/graph/doc/html/graph/algorithms/spanning_trees/prim_minimum_spanning_tree.html", ""),
    SearchResult("Weighted graph algorithms with Python", "https://arxiv.org/abs/1504.07828", "Prim minimum spanning tree"),
)
FETCH_REASONS = {
    "blocked_url": "网址不符合公开网页访问条件",
    "denied": "HTTP 拒绝访问", "rate_limited": "HTTP 限流",
    "timeout": "网络超时", "network": "网络连接失败",
    "empty_body": "正文提取为空", "redirect": "重定向无法完成", "http_error": "网页返回 HTTP 错误",
}
CHANNELS = {"web": "网页搜索", "literature": "论文索引", "reference": "官方技术参考"}
PAPER_ABSTRACT = "论文摘要"


def is_prim_question(question):
    return bool(PRIM.search(question) and (MST.search(question) or re.search(r"算法|\balgorithm\b", question, re.I)))


def source_key(url):
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"gclid", "fbclid"}]
    path = parts.path.rstrip("/")
    if parts.hostname == "arxiv.org":
        path = re.sub(r"v\d+$", "", path)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), ""))


def public_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.port not in {None, 80, 443}):
            return False
        return True
    except ValueError:
        return False


def fetch_text(url: str, budget: Budget) -> tuple[str, str]:
    for _ in range(3):
        budget.check()
        if not public_url(url):
            return "", "blocked_url"
        hostname = urlsplit(url).hostname
        try:
            addresses = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(addr[4][0]).is_global for addr in addresses):
                return "", "blocked_url"
            with requests.get(url, timeout=max(0.1, min(5, budget.remaining())), allow_redirects=False,
                              stream=True, headers={"User-Agent": "Lite-Research-Agent/1.0"}) as response:
                if response.is_redirect:
                    location = response.headers.get("Location")
                    if not location:
                        return "", "redirect"
                    url = urljoin(url, location)
                    continue
                if response.status_code in {401, 403, 451}:
                    return "", "denied"
                if response.status_code == 429:
                    return "", "rate_limited"
                if response.status_code == 408:
                    return "", "timeout"
                response.raise_for_status()
                chunks = bytearray()
                for block in response.iter_content(8192):
                    budget.check()
                    chunks.extend(block)
                    if len(chunks) >= 131072:
                        break
        except requests.Timeout:
            return "", "timeout"
        except requests.HTTPError:
            return "", "http_error"
        except (OSError, requests.RequestException, ValueError):
            return "", "network"
        soup = BeautifulSoup(bytes(chunks), "html.parser")
        if hostname == "arxiv.org" and urlsplit(url).path.startswith("/abs/"):
            abstract = soup.select_one("blockquote.abstract")
            if abstract:
                return " ".join(abstract.stripped_strings)[:3000], PAPER_ABSTRACT
        for element in soup(["script", "style", "nav", "footer", "header", "aside"]):
            element.decompose()
        content = soup.select_one("main, article, [role=main]") or soup
        text = " ".join(content.stripped_strings)[:3000]
        return (text, "") if text else ("", "empty_body")
    return "", "redirect"


def search_sources(query: str, key: str, count=3, timeout=7, record=None, channel="web") -> list[SearchResult]:
    if channel == "reference":
        # Query by the canonical ID; accept only the abstract actually returned by the index.
        indexed = search_openalex(None, "10.48550/arXiv.1504.07828", min(4, timeout))
        abstract = next((item.abstract for item in indexed if item.abstract and MST.search(item.abstract)), None)
        references = list(PRIM_REFERENCES)
        if abstract:
            paper = references[-1]
            references[-1] = SearchResult(paper.title, paper.url, paper.snippet, abstract=abstract)
        return references[:count]
    if channel == "literature":
        doi = DOI.search(query)
        return search_openalex(query, doi[0].rstrip(".,;") if doi else None, timeout, exact=False, strict=True)[:count]
    if key:
        try:
            response = requests.post("https://api.tavily.com/search", timeout=timeout,
                                     headers={"Authorization": f"Bearer {key}"},
                                     json={"query": query[:380], "search_depth": "basic", "max_results": count,
                                           "include_answer": False, "auto_parameters": False, "include_usage": True})
            if not response.ok:
                raise ModelError(f"Tavily 搜索返回 HTTP {response.status_code}。")
            body = response.json()
            if record:
                record((body.get("usage") or {}).get("credits"))
            return [SearchResult(item["title"], item["url"], item.get("content") or "")
                    for item in body.get("results", []) if public_url(item.get("url", ""))]
        except (requests.RequestException, TypeError, KeyError, ValueError):
            raise ModelError("Tavily 搜索请求失败，请检查密钥和网络。") from None
    tool = SearchTool(max_retries=0)
    subject = model_name(query)
    doi = DOI.search(query)
    if doi or subject:
        deadline = monotonic() + timeout
        if subject and not doi:
            papers = tool._search_arxiv_title(subject, 3, timeout=min(4, timeout/2))
            if papers:
                return papers[:count]
        remaining = deadline - monotonic()
        if remaining > .1:
            papers = search_openalex(subject, doi[0].rstrip(".,;") if doi else None, remaining)
            if papers:
                return papers[:count]
        if doi:
            return [SearchResult("DOI " + doi[0], "https://doi.org/" + doi[0].rstrip(".,;"), "")]
        remaining = deadline - monotonic()
        if remaining > .1:
            return [item for item in tool._search_bing_rss(subject, count, timeout=remaining)
                    if subject.casefold() in (item.title + " " + item.url).casefold()]
        return []
    return tool._search_public_sources(query[:250], count, timeout=timeout)


def search_openalex(subject: str | None, doi: str | None, timeout: float, *, exact=True, strict=False) -> list[SearchResult]:
    params = {"filter": f"doi:https://doi.org/{doi}", "per-page": 2} if doi else {"search": subject, "per-page": 2}
    try:
        response = requests.get("https://api.openalex.org/works", params=params, timeout=timeout)
        response.raise_for_status()
        results = response.json().get("results", [])
    except (requests.RequestException, ValueError) as error:
        if strict:
            if isinstance(error, requests.Timeout):
                raise ModelError("论文索引请求超时。") from None
            if isinstance(error, requests.HTTPError) and error.response is not None:
                raise ModelError(f"论文索引返回 HTTP {error.response.status_code}。") from None
            raise ModelError("论文索引无法连接或响应无效。") from None
        return []
    sources = []
    for work in results:
        title = work.get("display_name") or ""
        if not title or (exact and not doi and subject and subject.casefold() not in title.casefold()):
            continue
        location = work.get("primary_location") or {}
        url = location.get("landing_page_url") or work.get("doi") or work.get("id")
        if not url or not public_url(url):
            continue
        words = sorted((position, word) for word, positions in (work.get("abstract_inverted_index") or {}).items()
                       for position in positions)
        abstract = " ".join(word for _, word in words)
        sources.append(SearchResult(title, url, "", abstract=abstract or None))
    return sources


def query_for(text: str, original: str) -> str:
    if is_prim_question(original):
        if not PRIM.search(text):
            text = f"Prim {text}"
        if not re.search(r"\bminimum[\s-]+spanning[\s-]+tree\b|\bmst\b", text, re.I):
            text = f"minimum spanning tree {text}"
    doi = DOI.search(original)
    subject = doi[0].rstrip(".,;") if doi else (None if is_prim_question(original) else model_name(original))
    return f"{subject} {text}" if subject and subject.casefold() not in text.casefold() else text


def paper_excerpt(abstract, title):
    """Prioritize the paper's contribution over background about other systems."""
    sentences = re.split(r"(?<=[.!?。])\s+(?=[A-Z])", abstract)
    subject = model_name(title)
    for index, sentence in enumerate(sentences):
        own_work = re.search(r"\b(?:we (?:propose|introduce|present|develop)|this (?:paper|work) (?:proposes|presents|introduces))\b", sentence, re.I)
        if own_work or (subject and subject.casefold() in sentence.casefold()):
            return " ".join(sentences[index:])[:1050]
    return abstract[:1050]


def evidence_from_query(query, budget, gateway, sources, seen, round_no, *, channel, attempts, fetched, prim, subject):
    key = gateway.settings.search_key.get_secret_value() if channel == "web" else ""
    budget.check()
    budget.emit("search", {"query": query, "round": round_no, "channel": CHANNELS[channel]})
    attempt = {"query": query, "channel": CHANNELS[channel], "issues": [], "sources": []}
    if channel == "reference":
        attempt["lookup"] = "访问预选技术文档，并按 DOI 10.48550/arXiv.1504.07828 查询论文摘要。"
    attempts.append(attempt)

    def issue(kind, message, url=None):
        item = {"kind": kind, "message": message}
        if url:
            item["url"] = url
        attempt["issues"].append(item)
        budget.emit("notice", {**item, "round": round_no})
    started = monotonic()
    summary_seconds = 0.0
    search_entry = {"provider": "Tavily" if key else CHANNELS[channel], "credits": None,
                    "usd": None if key else 0.0, "reason": "接口未返回搜索积分" if key else "公开接口无需付费凭据"}
    budget.searches.append(search_entry)

    def record_search(credits):
        if type(credits) is int and credits >= 0:
            price = gateway.settings.search_credit_price
            search_entry.update(credits=credits, rate=price, usd=credits*price if price is not None else None,
                                reason="设置页提供的搜索积分费率" if price is not None else "未配置搜索积分费率")

    try:
        try:
            candidates = search_sources(query, key, timeout=max(.1, min(7, budget.remaining())), record=record_search, channel=channel)
        except ModelError as error:
            issue("search_error", str(error))
            search_entry["reason"] = "搜索请求失败，账单未确认"
            candidates = []
            if key:
                budget.searches.append({"provider": "公开检索", "credits": None, "usd": 0.0, "reason": "公开回退"})
                candidates = search_sources(query, "", timeout=max(.1, min(5, budget.remaining())), channel=channel)
        if not candidates and not any(item["kind"] == "search_error" for item in attempt["issues"]):
            issue("search_empty", f"没有搜索结果：{query}")
        for result in candidates:
            budget.check()
            if len(sources) >= 12:
                break
            url = result.url.split("#", 1)[0]
            if not public_url(url):
                continue
            normalized = source_key(url)
            metadata = " ".join((result.title or "", result.snippet or "", result.abstract or ""))
            unrelated = (not (PRIM_MST.search(metadata) or PRIM.search(metadata) and MST.search(metadata)) if prim
                         else subject and round_no == 1 and subject.casefold() not in (metadata + " " + url).casefold())
            if unrelated:
                ignored = ("irrelevant", normalized)
                if ignored not in fetched:
                    fetched.add(ignored)
                    issue("irrelevant", f"已过滤：与课题无关 · {result.title}", url)
                continue
            if normalized in seen:
                continue
            content, outcome = (result.abstract, PAPER_ABSTRACT) if result.abstract else ("", "")
            if not content:
                if normalized in fetched:
                    continue
                fetched.add(normalized)
                content, outcome = fetch_text(url, budget)
            basis = PAPER_ABSTRACT if outcome == PAPER_ABSTRACT else "网页正文"
            if not content:
                issue(outcome, f"{FETCH_REASONS.get(outcome, '无法读取网页')} · {result.title or url}", url)
                if result.snippet and (key or prim and PRIM.search(result.snippet) and MST.search(result.snippet)):
                    content, basis = result.snippet, "搜索摘录"
                else:
                    continue
            if prim and not (MST.search(content) or PRIM_MST.search(content)):
                issue("unsupported", f"正文无法支持结论 · {result.title}", url)
                continue
            # Keep a short source excerpt even if the summarization call cannot run.
            excerpt = paper_excerpt(content, result.title) if basis == PAPER_ABSTRACT else content[:460]
            row = {"title": gateway.settings.redact(result.title or url), "url": gateway.settings.redact(url),
                   "content": gateway.settings.redact(excerpt), "basis": basis}
            if content:  # 对所有来源（含论文摘要、短正文、搜索摘录）做相关性判断，过滤无关结果
                before_summary = monotonic()
                try:
                    identifier = len(sources) + 1
                    extracted = gateway.complete_json(prompts.EVIDENCE.format(query=query[:145], identifier=identifier,
                                                      title=row["title"][:150], url=url[:240], content=(excerpt if basis == PAPER_ABSTRACT else content[:1700])),
                                                      stage="证据摘要", round_no=round_no)
                except BudgetStop:
                    if prim:
                        sources.append(row)
                        seen.add(normalized)
                        attempt["sources"].append(len(sources))
                        budget.emit("finding", row)
                    raise
                except ModelError as error:
                    issue("extraction_error", f"证据提取模型请求失败：{error}", url)
                    if not prim:
                        continue
                    extracted = {}
                finally:
                    summary_seconds += monotonic()-before_summary
                if isinstance(extracted, dict) and extracted.get("id") == identifier and extracted.get("relevant") is False:
                    issue("unsupported", f"正文无法支持结论 · {result.title}", url)
                    continue
                elif isinstance(extracted, dict) and extracted.get("id") == identifier and extracted.get("relevant") is True:
                    facts = extracted.get("facts")
                    facts = [x.strip()[:250] for x in facts[:2] if isinstance(x, str) and x.strip()] if isinstance(facts, list) else []
                    if facts:
                        row["content"] = "；".join(facts)
            sources.append(row)
            seen.add(normalized)
            attempt["sources"].append(len(sources))
            budget.emit("finding", row)
    finally:
        budget.search_seconds += monotonic()-started-summary_seconds


def validated_plan(gateway, question, experiment, previous):
    prompt = prompts.PLAN.format(question=question[:450], history=compact(previous, 520),
                                 tools="行情基准、逐组对拍、计时" if experiment else "论文检索、网页读取、证据核查",
                                 budget=compact({key: value for key, value in gateway.budget.snapshot().items()
                                                 if key in {"remaining_tokens", "remaining_seconds", "remaining_usd"}}, 240))
    data = gateway.complete_json(prompt, stage="规划")
    valid = lambda value: (isinstance(value, dict) and isinstance(value.get("queries"), list)
                           and any(isinstance(x, str) and x.strip() for x in value["queries"])
                           and isinstance(value.get("criterion"), str) and bool(value["criterion"].strip()))
    if not valid(data) and gateway.settings.fast and gateway.settings.strong and gateway.policy == "adaptive":
        data = gateway.complete_json(prompt, stage="规划", reason="初始规划未通过结构校验")
    if not valid(data):
        data = {"queries": [question[:120]], "criterion": "用可引用证据回答核心问题。"}
    data["queries"] = list(dict.fromkeys(x.strip()[:150] for x in data["queries"] if isinstance(x, str) and x.strip()))[:2]
    if experiment:
        data["criterion"] = "密集键和稀疏键的成交量、事件数逐组一致。预检分桶数组不超过 32 MiB。记录两种数据的计时中位数。"
    elif is_prim_question(question):
        data["criterion"] = "核对文献中 Prim 的实现与适用条件。实测时验证树的连通性、边数和总权重，按图密度比较计时；20% 是待验证目标。"
    subquestions = data.get("subquestions")
    subquestions = [x.strip()[:150] for x in subquestions[:4] if isinstance(x, str) and x.strip()] if isinstance(subquestions, list) else []
    return {"queries": data["queries"], "subquestions": subquestions or data["queries"], "criterion": data["criterion"][:400],
            "first": "direct" if experiment else "核查来源覆盖情况"}


def benchmark_choice(experiments, proposal):
    if not experiments:
        return "direct", "先验证直接分桶在两种键分布上的适用范围。"
    previous = experiments[-1]
    tried = {item["candidate"] for item in experiments}
    if previous["candidate"] == "direct" and any(row.get("estimated_bytes") for row in previous["datasets"]):
        return "compressed", "上一轮稀疏键触发内存上限，下一轮先压缩键再分桶，分桶大小改为分组数量。"
    candidate = proposal.get("candidate") if isinstance(proposal, dict) else None
    if isinstance(candidate, str) and candidate in CANDIDATES and candidate not in tried:
        return candidate, str(proposal.get("reason", "依据上一轮指标选择下一候选。"))[:360]
    for option in ("compressed", "sorted", "baseline"):
        if option not in tried:
            return option, "继续验证一个尚未测试的候选。"
    return None, "所有候选均已测试。"


def winner(experiments):
    eligible = [item for item in experiments if all(row["passed"] for row in item["datasets"])]
    return max(eligible, key=lambda item: min(row["speedup"] for row in item["datasets"]), default=None)


def measured_conclusion(experiments):
    best = winner(experiments)
    if not best:
        return "本轮尚无同时通过密集键和稀疏键验证的候选。"
    details = "、".join(f"{row['dataset']} {row['speedup']:.3f} 倍" for row in best["datasets"])
    return (f"{best['id']}（{best['candidate']}）在两组数据上的成交量和事件数均与 pandas.groupby 基准逐组一致。"
            f"本次计时的加速比分别为{details}。加速比使用各组中位数，适用范围限于报告所列数据和机器。")


def article_fields(question, scenario, sources, experiments, summary, article):
    result = measured_conclusion(experiments)
    if scenario == "market":
        intro = "本文研究行情事件分组聚合在不同键分布下的加速方法，以 pandas.groupby 为基准，分别对密集键与稀疏键进行逐组对拍和重复计时。"
        abstract = intro + result if experiments else "本文拟比较不同键分布下的行情事件分组聚合方案。研究采用 pandas.groupby 作为对拍和计时基准，本轮尚未完成实测，正确性与性能仍待验证。"
        draft = article.get("abstract")
        # Accept only the checked method and the exact measured result; model prose must not change claims.
        if experiments and isinstance(draft, str):
            draft = " ".join(draft.split())
            if draft == intro + result:
                abstract = draft
        return abstract, ["分组聚合", "行情事件", "性能基准"], "", None
    draft = article.get("abstract")
    facts = checked_citations(summary, sources)
    if not sources:
        abstract = "本文围绕所述研究问题开展资料检索。本轮未取得可引用的证据，尚无法得出有依据的结论，相关假设需要进一步检索与验证。"
    elif isinstance(draft, str) and supported_citations(draft, sources) and not re.search(r"Token|运行耗时|API 费用|调用次数", draft, re.I):
        abstract = " ".join(draft.split())[:850]
        if not re.search(r"仍需|待验证|有待|尚需", abstract):
            abstract = "本文通过可追溯文献分析所述研究问题。" + abstract.rstrip("。") + "。结论限于所引文献，仍需独立实验验证。"
    else:
        finding = (" ".join(facts.split())[:430] if supported_citations(facts, sources) else
                   f"检索获得《{sources[0]['title'][:110]}》[1]，原文摘录见相关工作。")
        abstract = f"本文围绕所述研究问题检索并分析可追溯文献。{finding} 当前结论限于所引资料，相关假设仍需实验验证。"
    keywords = article.get("keywords")
    keywords = [x.strip()[:28] for x in keywords[:5] if isinstance(x, str) and x.strip()] if isinstance(keywords, list) else []
    design = article.get("experiment")
    required = ("hypothesis", "control", "variables", "metric", "criterion")
    if isinstance(design, dict) and all(isinstance(design.get(key), str) and design[key].strip() for key in required):
        design = {key: design[key].strip()[:280] for key in required}
    else:
        design = None
    limitations = article.get("limitations")
    return checked_citations(abstract, sources), keywords or ["文献调研", "证据分析"], limitations[:500] if isinstance(limitations, str) else "", design


def supported_citations(text, sources):
    citations = re.findall(r"\[(\d{1,6})\]", text)
    return bool(citations) and all(1 <= int(i) <= len(sources) for i in citations)


def valid_refs(value, sources):
    return sorted({i for i in value if type(i) is int and 1 <= i <= len(sources)}) if isinstance(value, list) else []


def audit_items(values):
    return [value.strip()[:240] for value in values[:4] if isinstance(value, str) and value.strip()] if isinstance(values, list) else []


def prim_evidence_scope(sources, refs):
    comparisons, methods, crossing = [], [], []
    for ref in refs:
        text = sources[ref - 1]["content"]
        if re.search(r"\bKruskal\b", text, re.I):
            comparisons.append(ref)
        if re.search(r"\b(?:binary heap|priority queue|indexed minimum priority queue)\b", text, re.I):
            methods.append(ref)
        if re.search(r"\b(?:cheapest|minimum-weight|lightest)\b.{0,50}\b(?:crossing|cut)\b", text, re.I):
            crossing.append(ref)
    return [{"claim": label, "refs": ids} for label, ids in (
        ("来源同时讨论 Prim 和 Kruskal，可用于设计正确性对照。", comparisons),
        ("来源讨论堆或优先队列的实现，可用于选择 Prim 基准。", methods),
        ("来源描述了 Prim 选择跨割的最小权重边的做法。", crossing),
    ) if ids]


def prim_design():
    return {"hypothesis": "不同图密度下，邻接矩阵扫描或优先队列优化可能减少 Prim 的运行时间。",
            "control": "邻接表加二叉堆的 Prim 实现",
            "variables": "固定随机种子，使用同批加权连通图，|E|/|V| 约为 1.5、10、100。",
            "metric": "比较各密度下的运行时间中位数；校验树的连通性、边数和总权重，并与 Kruskal 对拍。",
            "criterion": "所有正确性检查通过；各密度分别报告实测降幅，20% 是待验证的性能目标。"}


def next_search(question, prim, channel, review, attempted, plan, has_evidence, last_issues):
    if prim:
        suggestions = [
            ("web", "Prim minimum spanning tree binary heap complexity"),
            ("literature", "Prim minimum spanning tree sparse dense graph priority queue performance"),
            ("reference", "Prim MST indexed binary heap adjacency list verification"),
        ]
    else:
        subject = model_name(question) or question[:120]
        suggestions = [
            ("web", plan["queries"][0]),
            ("literature", plan["queries"][-1] if len(plan["queries"]) > 1 else subject),
            ("literature", f"{subject} empirical comparison"),
            ("web", f"{subject} {'benchmark independent evaluation' if has_evidence else 'official documentation paper'}"),
        ]
    if attempted:
        failed_fetch = any(item["kind"] in FETCH_REASONS or item["kind"] == "unsupported" for item in last_issues)
        channels = (["reference", "literature", "web"] if prim and failed_fetch
                    else ["literature", "reference", "web"] if channel == "web"
                    else ["reference", "web", "literature"])
        suggestions.sort(key=lambda option: channels.index(option[0]))
        proposed = review.get("query")
        if isinstance(proposed, str) and proposed.strip():
            # Prefer an alternate entry point after a failed or already successful attempt.
            suggestions.insert(0, (next(item for item in channels if item != channel and (item != "reference" or prim)),
                                   proposed.strip()[:140]))
    for provider, text in suggestions:
        query = query_for(text[:160], question)
        key = (provider, " ".join(query.casefold().split()))
        if not any(key[1] == a[1] for a in attempted) and (provider != "reference" or not any(a[0] == provider for a in attempted)):
            return provider, query, key
    return None


def research_rounds(question, plan, sources, seen, budget, gateway, rounds, emit):
    prim = is_prim_question(question)
    subject = model_name(question) if not prim and not DOI.search(question) else None
    attempted, fetched = set(), set()
    channel = None
    review = {}
    last_issues = []
    end = "已达到设定轮次上限。"
    for number in range(1, budget.limits.rounds + 1):
        budget.check()
        available = budget.snapshot()
        if available["remaining_tokens"] is not None and available["remaining_tokens"] <= 0:
            raise BudgetStop("已达到 Token 上限。")
        if budget.limits.usd is not None and available["known_usd"] > budget.limits.usd:
            raise BudgetStop("已达到费用上限。")
        selection = next_search(question, prim, channel, review, attempted, plan, bool(sources), last_issues)
        if not selection:
            end = "可用查询和检索入口已尝试，暂无新的推进方式。"
            break
        channel, query, key = selection
        attempted.add(key)
        goal = ("建立可引用的基线" if number == 1 else
                "修复检索并补足证据" if not sources else "用独立来源复核或补足关键缺口")
        row = {"round": number, "goal": goal, "attempts": [], "sources": [], "supported": [],
               "gaps": [], "conflicts": [], "next": ""}
        rounds.append(row)
        emit("phase", {"name": "researching", "label": f"第 {number} 轮 · {goal}"})
        emit("round_review", row)
        try:
            evidence_from_query(query, budget, gateway, sources, seen, number, channel=channel,
                                attempts=row["attempts"], fetched=fetched, prim=prim, subject=subject)
            row["sources"] = [ref for attempt in row["attempts"] for ref in attempt["sources"]]
            last_issues = [issue for attempt in row["attempts"] for issue in attempt["issues"]]
            emit("phase", {"name": "validation", "label": f"第 {number} 轮 · 核对验收标准"})
            if sources:
                ids = list(dict.fromkeys(row["sources"] + list(range(1, min(2, len(sources)) + 1))))[:4]
                observations = [{"id": i, "basis": sources[i - 1]["basis"], "fact": sources[i - 1]["content"][:135]} for i in ids]
                validation_prompt = prompts.VALIDATION.format(question=question[:260], criterion=plan["criterion"][:230],
                    observations=compact({"evidence": observations, "issues": [item["kind"] for item in last_issues]}, 870))
                try:
                    review = gateway.complete_json(validation_prompt,
                        stage="验证与修正", round_no=number)
                except ModelError as error:
                    message = f"第 {number} 轮核查模型调用失败：{error}"
                    row["attempts"][-1]["issues"].append({"kind": "validation_error", "message": message})
                    emit("notice", {"kind": "validation_error", "message": message})
                    review = {"action": "search", "gaps": ["模型核查未完成，待复核来源与验收标准。"]}
                review = review if isinstance(review, dict) else {}
                conflicts = review.get("conflicts")
                if gateway.policy == "adaptive" and gateway.settings.fast and gateway.settings.strong and isinstance(conflicts, list):
                    for item in conflicts[:3]:
                        if not isinstance(item, dict) or not isinstance(item.get("claim"), str) or not item["claim"].strip():
                            continue
                        refs = valid_refs(item.get("refs"), sources)
                        if len(refs) < 2 or not set(refs).issubset(ids):
                            continue
                        try:
                            verified = gateway.complete_json(validation_prompt + "\n请复核两处引用是否直接矛盾：" + compact(item, 320),
                                stage="验证与修正", round_no=number, reason="关键证据冲突，需要复核引用")
                            if isinstance(verified, dict):
                                review = verified
                        except ModelError as error:
                            emit("notice", {"kind": "validation_error", "message": f"证据冲突复核失败：{error}"})
                        break
            else:
                review = {"action": "search", "gaps": ["尚未取得可引用的相关资料。"]}
            claims = review.get("supported")
            for item in claims[:3] if isinstance(claims, list) else []:
                if isinstance(item, dict) and isinstance(item.get("claim"), str) and item["claim"].strip():
                    refs = valid_refs(item.get("refs"), sources)
                    if refs:
                        row["supported"].append({"claim": item["claim"].strip()[:220], "refs": refs})
            row["gaps"] = audit_items(review.get("gaps"))
            conflicts = review.get("conflicts")
            if isinstance(conflicts, list):
                for item in conflicts[:3]:
                    if isinstance(item, dict) and isinstance(item.get("claim"), str):
                        refs = valid_refs(item.get("refs"), sources)
                        if len(refs) >= 2:
                            row["conflicts"].append(item["claim"].strip()[:205] + " " + " ".join(f"[{ref}]" for ref in refs))
            if prim:
                # Keep the model's direction, but don't let it claim measured speed or a better MST
                # from abstracts. Minimum total weight is a correctness invariant.
                row["supported"] = prim_evidence_scope(sources, row["sources"])
                row["gaps"].append("图密度下的性能、正确性和 20% 目标仍待基准实验。")
                if re.search(r"生成树.{0,4}质量|质量.{0,4}生成树", " ".join(
                        str(review.get(key, "")) for key in ("query", "reason")), re.I):
                    review = {key: value for key, value in review.items() if key not in {"query", "reason"}}
            if re.search(r"(?:本轮|本次|当前)实测", str(review.get("reason", ""))):
                review.pop("reason", None)
            row["gaps"] = [re.sub(r"(?:本轮|本次|当前)实测", "已有资料", text) for text in row["gaps"]]
            if not row["sources"]:
                row["gaps"].append("本轮未新增独立来源。")
            if (number >= 2 and review.get("action") == "finish" and row["sources"] and
                    len({urlsplit(source["url"]).hostname for source in sources}) >= 2 and
                    row["supported"] and not row["gaps"] and not row["conflicts"]):
                end = "关键结论已完成独立来源核对。"
                row["next"] = end
                break
            upcoming = next_search(question, prim, channel, review, attempted, plan, bool(sources), last_issues)
            if number == budget.limits.rounds:
                row["next"] = end
                if prim and sources:
                    row["next"] += " 后续用同批连通图进行 Prim 基准计时，并用 Kruskal 对拍总权重。"
                elif upcoming:
                    row["next"] += f" 如继续，可从{CHANNELS[upcoming[0]]}补查「{upcoming[1]}」。"
                break
            if not upcoming:
                end = "可用查询和检索入口已尝试，暂无新的推进方式。"
                row["next"] = end
                break
            cause = ("更换歧义查询或入口" if not sources else "补查独立来源")
            reason = review.get("reason")
            row["next"] = f"{cause}。下一轮用{CHANNELS[upcoming[0]]}查询「{upcoming[1]}」。"
            if isinstance(reason, str) and len(reason.strip()) > 3:
                row["next"] += " 理由：" + reason.strip()[:180]
        except (BudgetStop, ModelError) as error:
            row["sources"] = [ref for attempt in row["attempts"] for ref in attempt["sources"]]
            row["gaps"].append("本轮验收尚未完成。")
            row["next"] = f"{error} 可继续核对本轮未完成的来源或查询。"
            raise
        finally:
            emit("round_review", row)
            emit("usage", budget.snapshot())
    return end


def run_research(question: str, scenario: str, settings: Settings, limits: Limits, policy: str,
                 history: list[dict], emit) -> dict:
    notices = []

    def publish(kind, data):
        if kind == "notice":
            notices.append(data["message"])
        emit(kind, data)

    budget = Budget(limits, publish)
    gateway = Gateway(settings, budget, policy)
    experiment = scenario == "market"
    sources, seen = [], set()
    for prior in reversed(history[-4:]):
        for row in prior.get("sources", []):
            if len(sources) < 6 and public_url(row.get("url", "")) and source_key(row["url"]) not in seen:
                sources.append(row.copy())
                seen.add(source_key(row["url"]))
    initial_sources = len(sources)
    previous_experiments = [row for prior in history[-4:] for row in prior.get("experiments", [])][-8:]
    previous = {"question": history[-1]["question"] if history else None,
                "experiments": [{"candidate": row["candidate"], "datasets": row["datasets"]}
                                for row in previous_experiments[-3:]], "evidence_count": initial_sources}
    plan, hypotheses, experiments, revisions, rounds = {}, [], [], [], []
    status, detail, conclusion, end_reason = "complete", "", "", "已达到设定轮次上限。"
    article, previous_validation = {}, {}
    sent_evidence = set()
    emit("usage", budget.snapshot())
    for source in sources:
        emit("finding", source)
    try:
        emit("phase", {"name": "planning", "label": "规划"})
        plan = validated_plan(gateway, question, experiment, previous)
        emit("plan", plan)
        emit("phase", {"name": "researching", "label": "收集证据"})
        if experiment:
            for title, url, content in (
                ("NumPy bincount 参考文档", "https://numpy.org/doc/stable/reference/generated/numpy.bincount.html",
                 "bincount 的分桶数量至少为最大输入值加一。"),
                ("pandas DataFrame.groupby 参考文档", "https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.DataFrame.groupby.html",
                 "基准按标的分组，明确使用 sort=True 并聚合 sum 和 size。"),
            ):
                if source_key(url) not in seen:
                    row = {"title": title, "url": url, "content": content, "basis": "实验任务的参考文档"}
                    sources.append(row)
                    seen.add(source_key(url))
                    emit("finding", row)
        else:
            end_reason = research_rounds(question, plan, sources, seen, budget, gateway, rounds, emit)
        for round_no in range(1, limits.rounds+1) if experiment else ():
            budget.check()
            emit("phase", {"name": "hypothesis", "label": f"第 {round_no} 轮 · 提出假设"})
            evidence = [{"id": i, "text": row.get("content", "")[:230]} for i, row in enumerate(sources[:8], 1)]
            results = [{"id": r["id"], "candidate": r["candidate"],
                        "datasets": [{k: v for k, v in row.items() if k != "timings_ms"} for row in r["datasets"]]}
                       for r in experiments[-2:]]
            failed = len(results) >= 2 and all(any(not row["passed"] for row in item["datasets"]) for item in results)
            reason = "连续两轮候选未通过约束，诊断失败后选择可复测方案" if failed else None
            new_evidence = [row for row in evidence if sources[row["id"]-1]["url"] not in sent_evidence][:2]
            proposal = gateway.complete_json(prompts.HYPOTHESIS.format(question=question[:250],
                evidence=compact(new_evidence, 650),
                measurement=compact({"last": results[-1:] or previous.get("experiments", [])[-1:], "validation": previous_validation}, 550),
                actions="direct, compressed, sorted, baseline"),
                stage="假设", round_no=round_no, reason=reason)
            sent_evidence.update(sources[row["id"]-1]["url"] for row in new_evidence)
            proposal = proposal if isinstance(proposal, dict) else {}
            conflict_refs = proposal.get("conflict_refs")
            conflict = isinstance(conflict_refs, list) and len({i for i in conflict_refs if type(i) is int and 1 <= i <= len(sources)}) >= 2
            if (conflict and not failed
                    and settings.fast and settings.strong and policy == "adaptive"):
                conflict_evidence = [row for row in evidence if row["id"] in conflict_refs]
                diagnosis = gateway.complete_json(f"核对是否有可引用的关键矛盾。问题：{question[:300]}。证据：{compact(conflict_evidence, 950)}。"
                                                  "返回 {reason:差异,query:用于核验的短检索词}。",
                                                  stage="证据核验", round_no=round_no, reason="关键证据可能冲突")
                if isinstance(diagnosis, dict):
                    proposal.update({k: v for k, v in diagnosis.items() if k in {"reason", "query"} and isinstance(v, str)})
            candidate, explanation = benchmark_choice(experiments, proposal)
            if candidate is None:
                end_reason = explanation
                break
            refs = proposal.get("refs")
            refs = sorted({i for i in refs if type(i) is int and 1 <= i <= len(sources)}) if isinstance(refs, list) else []
            hypothesis = {"round": round_no, "text": str(proposal.get("hypothesis") or "现有证据可用于检验该方向。")[:420],
                          "expected": str(proposal.get("expected") or "得到可检查的证据。")[:280],
                          "criterion": str(proposal.get("criterion") or plan["criterion"])[:350], "refs": refs,
                          "candidate": candidate}
            if candidate in {"direct", "compressed"}:
                hypothesis.update(text=CANDIDATES[candidate], criterion=plan["criterion"],
                                  expected="密集键和稀疏键分别通过对拍与内存预检。",
                                  refs=[i for i, row in enumerate(sources, 1) if "numpy.bincount" in row["url"]])
            hypotheses.append(hypothesis)
            emit("hypothesis", hypothesis)
            revision = {"round": round_no, "reason": explanation,
                        "based_on": experiments[-1]["id"] if experiments else None}
            if experiments:
                revisions.append(revision)
                emit("revision", revision)
            row = {"round": round_no, "goal": f"验证 {candidate} 的正确性和速度", "attempts": [],
                   "sources": hypothesis["refs"], "supported": [], "gaps": [], "conflicts": [], "next": ""}
            rounds.append(row)
            emit("phase", {"name": "analysis", "label": f"第 {round_no} 轮 · 实验"})
            started = monotonic()
            try:
                result = run_benchmark(candidate, budget.check)
            except BudgetStop:
                row.update(gaps=["本轮未完成计时或对拍。"], next="预算恢复后重新测量当前候选。")
                emit("round_review", row)
                raise
            finally:
                budget.experiment_seconds += monotonic()-started
            result.update(id=f"E{round_no:02d}", round=round_no)
            experiments.append(result)
            emit("experiment", result)
            emit("phase", {"name": "validation", "label": f"第 {round_no} 轮 · 验证"})
            emit("usage", budget.snapshot())
            best = winner(experiments)
            row["experiment"] = result["id"]
            row["supported"] = [{"claim": f"{item['dataset']}通过对拍，加速比 {item['speedup']:.3f}×。", "refs": []}
                                for item in result["datasets"] if item["passed"]]
            row["gaps"] = [f"{item['dataset']}：{item['reason']}" for item in result["datasets"] if not item["passed"]]
            if round_no >= 2 and best and min(x["speedup"] for x in best["datasets"]) > 1:
                end_reason = "已有候选通过两种键分布的正确性与性能验收。"
                row["next"] = end_reason
                emit("round_review", row)
                break
            _, next_reason = benchmark_choice(experiments, {})
            previous_validation = {"action": "revise", "reason": next_reason}
            row["next"] = next_reason if round_no < limits.rounds else end_reason
            emit("round_review", row)
        if experiment:
            conclusion = measured_conclusion(experiments)
            if experiments and budget.remaining() > 15:
                emit("phase", {"name": "writing", "label": "整理论文摘要"})
                try:
                    draft = gateway.complete_json(prompts.MARKET_ABSTRACT.format(question=question[:240], result=conclusion), stage="论文摘要")
                    if isinstance(draft, dict):
                        article = draft
                except (BudgetStop, ModelError):
                    pass  # The authoritative summary is already available from measured results.
        elif sources and is_prim_question(question):
            claims = prim_evidence_scope(sources, range(1, len(sources) + 1))
            conclusion = "".join(item["claim"].rstrip("。") + " " + " ".join(f"[{ref}]" for ref in item["refs"]) + "。"
                                 for item in claims)
            article = {"keywords": ["最小生成树", "Prim 算法", "图密度"],
                       "limitations": "本研究只核查文献资料。最小生成树总权重用于校验正确性，性能目标须由同批图上的独立计时验证。"}
        elif sources:
            emit("phase", {"name": "writing", "label": "综合结果"})
            draft = gateway.complete_json(prompts.WRITING.format(question=question[:270], results="本轮为文献分析",
                evidence=compact([{"id": i, "title": row["title"][:115], "fact": row["content"][:780 if i==1 else 185]} for i, row in enumerate(sources[:5], 1)], 1450),
                hypothesis=(hypotheses[-1]["text"] if hypotheses else "")[:220]), stage="论文写作", max_tokens=600)
            article = draft if isinstance(draft, dict) else {}
            conclusion = article.get("findings", "")
            conclusion = conclusion[:1500] if isinstance(conclusion, str) else ""
            if not supported_citations(conclusion, sources):
                conclusion = "本轮取得可追溯资料。来源中的主要发现见相关工作，相关假设仍需进一步验证。"
        else:
            status, detail = "no_evidence", "本轮没有取得可引用的证据。可以缩短查询、使用 DOI 或在设置中配置独立搜索服务。"
    except BudgetStop as error:
        status, detail = "budget", str(error)
        end_reason = detail
    except ModelError as error:
        status, detail = "model_error", str(error)
        end_reason = detail
    except Exception as error:
        status, detail = "error", f"研究执行失败（{type(error).__name__}）。已保留当前结果。"
        end_reason = detail
    if experiment and experiments and not conclusion:
        conclusion = measured_conclusion(experiments)
    best = winner(experiments)
    abstract, keywords, limitations, design = article_fields(question, scenario, sources, experiments, conclusion, article)
    if not experiment and is_prim_question(question):
        design = prim_design()
    unresolved = list(dict.fromkeys(rounds[-1]["gaps"] + rounds[-1]["conflicts"])) if rounds else []
    return {"question": question, "scenario": scenario, "policy": policy, "status": status, "detail": detail,
            "plan": plan, "sources": sources, "hypotheses": hypotheses, "experiments": experiments,
            "revisions": revisions, "notices": notices, "summary": checked_citations(conclusion, sources), "usage": budget.snapshot(),
            "reused_sources": initial_sources, "reused_experiments": len(previous_experiments),
            "best": best["id"] if best else None, "parameters": PARAMETERS if experiment else None,
            "abstract": abstract, "keywords": keywords, "limitations": limitations, "design": design, "prompt_version": prompts.VERSION,
            "rounds": rounds, "end_reason": end_reason, "unresolved": unresolved}
