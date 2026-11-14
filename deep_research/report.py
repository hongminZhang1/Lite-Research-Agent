"""Render saved research to XeLaTeX and compile with bounded, non-interactive execution."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlsplit

from .lab import article_fields


class PdfError(Exception):
    pass


_TEX = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&",
        "#": r"\#", "%": r"\%", "_": r"\_", "^": r"\textasciicircum{}", "~": r"\textasciitilde{}"}
_CITATION = re.compile(r"\[(\d{1,6})\]")


def escape(text) -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]", "", str(text))
    return "".join(_TEX.get(char, char) for char in text)


def safe_link(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        return ""
    return quote(url, safe=":/?[]@!$&'()*+,;=%._-~#").replace("%", r"\%").replace("#", r"\#")


def table(headers, rows, widths):
    columns = "".join(r">{\raggedright\arraybackslash}p{" + width + r"\linewidth}" for width in widths)
    header = " & ".join(r"\textbf{" + escape(text) + "}" for text in headers) + r" \\ \hline"
    data = [" & ".join(escape(value) for value in row) + r" \\ \hline" for row in rows]
    return "\n".join([r"\begin{longtable}{" + columns + "}", r"\hline", header, r"\endfirsthead",
                      r"\hline", header, r"\endhead", *data, r"\end{longtable}"])


def money(amount):
    return "无法估算" if amount is None else f"{amount:.8g} USD"


def latex_source(run: dict) -> str:
    sources = run.get("sources", [])
    experiments = run.get("experiments", [])
    scenario = run.get("scenario", "general")
    fallback = article_fields(run["question"], scenario, sources, experiments, run.get("summary", ""), {})
    abstract = run.get("abstract") or fallback[0]
    keywords = run.get("keywords") or fallback[1]
    if scenario != "market" and sources and not experiments and not re.search(r"仍需|待验证|有待|尚需", abstract):
        abstract = "本文通过可追溯文献分析所述研究问题。" + abstract.rstrip("。") + "。结论限于所引文献，仍需独立实验验证。"

    def cited(value):
        parts = []
        last = 0
        for match in _CITATION.finditer(str(value)):
            parts.append(escape(str(value)[last:match.start()]))
            number = int(match[1])
            parts.append(fr"\hyperlink{{ref{number}}}{{[{number}]}}" if 0 < number <= len(sources) else "（引用无效）")
            last = match.end()
        parts.append(escape(str(value)[last:]))
        return "".join(parts)

    body = []

    def paragraph(text):
        if text:
            body.append(r"\hangindent 0pt\hangafter 1 " + cited(text) + r"\par")

    def section(title):
        body.append(r"\section{" + title + "}")

    section("研究问题")
    paragraph(run["question"])
    paragraph(run.get("plan", {}).get("criterion"))
    if run.get("detail"):
        paragraph(run["detail"])
    section("相关工作")
    if not sources:
        paragraph("本轮尚未取得可引用的文献。")
    for i, source in enumerate(sources, 1):
        paragraph(f"[{i}] {source['title']}（{source.get('basis', '来源记录')}）")
        if source.get("content"):
            body.append(r"\hangindent 2.5em\hangafter 0 " + cited(source["content"]) + r"\par")
    section("研究方法")
    if scenario == "market":
        paragraph("基准采用 pandas.groupby。各候选先验证每个标的的成交量和事件数，再在相同输入上交替计时并取中位数。")
    else:
        paragraph("根据子问题检索文献与网页，提取可引用事实，并核查证据对结论的覆盖范围。")
    for h in run.get("hypotheses", []):
        refs = " ".join(f"[{i}]" for i in h.get("refs", []))
        paragraph(f"研究假设：{h['text']} {refs}。预期：{h['expected']}。")
    params = run.get("parameters")
    if params:
        paragraph(f"随机种子 {params['seed']}，{params['rows']:,} 行，{params['groups']} 个标的，稀疏键步长 {params['stride']:,}，"
                  f"交替计时 {params['repetitions']} 次，分桶预检上限 {params['memory_limit_bytes']//1048576} MiB。")
    section("结果")
    if experiments:
        rows = []
        for exp in experiments:
            for row in exp["datasets"]:
                rows.append([f"{exp['id']} / {exp['candidate']}", row["dataset"], "通过" if row["passed"] else "未通过",
                             row.get("baseline_ms", "—"), row.get("candidate_ms", "—"), row.get("speedup", "—")])
        body.append(table(["实验 / 候选", "键分布", "对拍", "基准 ms", "候选 ms", "加速比"], rows,
                          [".23", ".12", ".12", ".12", ".12", ".12"]))
        for exp in experiments:
            for row in exp["datasets"]:
                if row.get("reason"):
                    paragraph(f"{exp['id']} / {row['dataset']}：{row['reason']}")
    else:
        paragraph("本节汇报文献证据分析，实验结果仍待验证。" if sources else "本轮未取得可报告的证据或实验结果。")
    if scenario == "market" and experiments:
        from .lab import measured_conclusion
        paragraph(measured_conclusion(experiments))
    else:
        for line in run.get("summary", "").splitlines():
            paragraph(line.strip().lstrip("# "))
    section("讨论与局限")
    paragraph(run.get("limitations") or ("测量结果适用于报告所列的合成数据与运行环境。"
              if experiments else "结论限于本轮获取的文献证据。面向其他数据集或条件的效果仍待验证。"))
    section("参考文献")
    for i, source in enumerate(sources, 1):
        url = safe_link(source["url"])
        name = escape(source["title"])
        body.append(fr"\hangindent 2.5em\hangafter 1 \hypertarget{{ref{i}}}{{[{i}]}}\hspace{{0.6em}}"
                    + (fr"\href{{{url}}}{{{name}}}" if url else name) + r"\par")
    if not sources:
        paragraph("本轮无可引用条目。")

    template = Path(__file__).with_name("paper.tex").read_text(encoding="utf-8")
    fields = {"TITLE": escape(run["question"]), "ID": escape(run.get("id", "")), "ABSTRACT": cited(abstract),
              "KEYWORDS": escape("；".join(keywords)), "BODY": "\n".join(body)}
    return re.sub(r"@@(TITLE|ID|ABSTRACT|KEYWORDS|BODY)@@", lambda match: fields[match[1]], template)


def pdf_bytes(run: dict) -> bytes:
    executable = shutil.which(os.environ.get("RESEARCH_XELATEX", "xelatex"))
    if not executable:
        raise PdfError("找不到 XeLaTeX。请安装支持中文的 TeX 发行版，或设置 RESEARCH_XELATEX。")
    miktex = "miktex" in executable.lower()
    flags = ["-disable-write18", "-disable-installer"] if miktex else ["-no-shell-escape"]
    with TemporaryDirectory(prefix="research-tex-") as temporary:
        folder = Path(temporary)
        (folder / "paper.tex").write_text(latex_source(run), encoding="utf-8")
        try:
            # Compile twice so internal reference targets remain valid across pages.
            for _ in range(2):
                result = subprocess.run([executable, *flags, "-interaction=nonstopmode", "-halt-on-error", "paper.tex"],
                                        cwd=folder, stdin=subprocess.DEVNULL, capture_output=True, timeout=25, check=False)
                if result.returncode:
                    log = (result.stdout + result.stderr).decode("utf-8", errors="replace")
                    if re.search(r"not found|cannot be found|not installed|fresh TeX installation", log, re.I):
                        raise PdfError("XeLaTeX 环境缺少中文排版包或字体。请检查 ctex、xeCJK、xurl 和中文字体。")
                    raise PdfError("XeLaTeX 未能编译报告。请下载源码，使用 XeLaTeX 查看编译诊断。")
        except subprocess.TimeoutExpired:
            raise PdfError("XeLaTeX 编译超时。可以先下载 LaTeX 源码。") from None
        except OSError:
            raise PdfError("无法启动 XeLaTeX，请检查编译器路径。") from None
        pdf = folder / "paper.pdf"
        if not pdf.is_file():
            raise PdfError("XeLaTeX 未生成 PDF。可以下载 LaTeX 源码。")
        return pdf.read_bytes()
