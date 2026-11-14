# Lite-Research Agent 1.0

Lite-Research Agent 的最初版本。输入研究问题和预算后，系统会多轮收集证据、校验结论，并记录每步使用的模型、输入和输出 Token、估算费用与运行时间。研究会话支持追问和删除，报告可导出为 PDF、LaTeX 和 JSON。

| 模式 | 运行方式 |
| --- | --- |
| 按需升级 | 本地小模型负责常规步骤，遇到规划校验失败、证据冲突或连续实验失败时，在预算允许的情况下调用强模型 |
| 每步强模型 | 每个需要模型参与的步骤都使用强模型 |
| 对照运行 | 用同一课题和预算分别运行前两种策略，对比调用、费用、运行时间和结果 |

## 启动

安装 Python 3.10 或更高版本，在仓库根目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn web.server:app --host 127.0.0.1 --port 8000
```

打开 [http://127.0.0.1:8000/](http://127.0.0.1:8000/)。默认普通模型为 `qwen2.5-3b-instruct`，使用本机的 OpenAI 兼容端点 `http://127.0.0.1:8081/v1`。请先启动对应推理服务，或在网页「设置」里填写已有的端点和模型名。

要使用 DeepSeek 强模型，将 [配置示例](.env.example) 复制为根目录的 `.env`，填入自己的 `DEEPSEEK_API_KEY`。后端会为强模型配置 `deepseek-v4-pro`。如果只有云端密钥，也可以执行 `powershell -ExecutionPolicy Bypass -File .\start-research.ps1 -CloudOnly`，再从网页启动研究。

Windows 启动脚本可在运行文件齐备时自动启动本地模型。`.runtime/llama/llama-server.exe` 和 `.runtime/models/qwen2.5-3b-instruct-q4_k_m.gguf` 需单独提供。PDF 导出需要 XeLaTeX 和中文字体。

## 使用与验证

- 在「设置」中分别测试普通模型、强模型和联网搜索。公开检索可以直接使用，也可配置 Tavily。设置费用上限时，云端模型需填写输入、输出与适用的缓存费率。DeepSeek 预填高峰费率供估算。
- 「开放调研」默认运行三轮。可用「逐轮核查」查看来源和下一步。试跑「行情聚合实验」可观察直接分桶因稀疏键触发内存限制后，系统改用键压缩复测。各轮结果与费用记录可导出。
- PDF 需要 MiKTeX 或 TeX Live，安装 `ctex`、`xeCJK`、`geometry`、`xurl` 和 `hyperref`，或用 `RESEARCH_XELATEX` 指定 XeLaTeX 路径。查看 [Prim 调研示例](examples/prim-report.pdf)。
- 执行 `python -m pytest` 运行自动化测试，执行 `python -m deep_research.eval --help` 查看独立评测参数。

## 目录

```text
deep_research/     研究循环、模型路由、检索、实验与报告
web/               FastAPI 接口和网页
tests/             自动化测试
examples/          代表性报告
docs/              后续设计计划
```

后续方向见 [设计计划](docs/roadmap.md)。

本项目基于 [hzyang0/Deep-Research-Agent](https://github.com/hzyang0/Deep-Research-Agent) 开发，沿用 [MIT 许可证](LICENSE)。
