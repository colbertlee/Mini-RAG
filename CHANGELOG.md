# Changelog

本项目所有显著变更记录于此。版本号遵循 [语义化版本 2.0.0](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-09-03

首个正式发布版本：本地轻量化 RAG 知识检索系统，端到端可用。

### 🎉 New Features

- **完整 RAG 流水线**：解析 → 清洗 → 切片 → 向量化 → 入库 → 检索 → 生成，各环节可独立替换。
- **多格式解析**：PDF / DOCX / HTML / Markdown / TXT，逐页类型路由（数字原生 vs 扫描件 OCR）。
- **扫描件 OCR**：RapidOCR 本地识别（可选依赖），未装时扫描页自动跳过并记日志。
- **多栏恢复**：贪心分栏按阅读顺序还原双栏 / 多栏排版。
- **表格 / 公式提取**：PyMuPDF `find_tables` 输出 Markdown 表格；公式启发式识别并标 `chunk_type=formula`。
- **内容清洗**：页眉页脚学习、模板噪声正则、目录页整页跳过、重复段落去重。
- **自适应切片**：按文档页数三档（<20 / 20~100 / >100 页），>100 页启用父子分层，overlap 落句子边界。
- **三种 Embedding Provider**：Ollama（默认，本地）/ OpenAI / BGE，配置切换。
- **双路检索 + RRF 融合**：Chroma dense + SQLite FTS5 稀疏，倒数排名融合。
- **零幻觉第一道防线**：`DENSE_MIN` 硬阈值 + 检索不到证据即程序层短路拒答，不调 LLM。
- **受限生成**：系统提示词零幻觉铁律 + 命令行逐字比对，`think:false` + 正则清洗兜底。
- **零幻觉第三道防线（L3 生成后校验）**：纯规则校验——推断话术正则、命令/标识符逐字比对上下文、引用编号越界、版本号比对；失败降级为「原文摘录」而非拒答（第三态，同样零幻觉且可用性更高）。
- **幂等增量入库**：`doc_id + file_hash` 去重，变更文件先删旧再重建，避免孤儿 chunk。
- **CLI**：`status` / `preview` / `index` / `ask` / `chat` / `purge` 六个子命令。
- **验证脚本**：`scripts/preview_chunks.py --check` 断言切片质量，可接 CI。

### 🐛 Bug Fixes

- 修复 Ollama 自动走 Vulkan 用显卡导致 `failed to allocate Vulkan0 buffer` 崩溃（请求强制 `num_gpu=0`）。
- 修复 embedding 模型与生成模型同时驻留导致内存崩溃（`keep_alive=0` 用完即卸载）。
- 修复 `index --rebuild` 残留旧 chunk：`rebuild_all` 先实例化 client 再删 collection，避免旧向量污染新语料。
- 修复 `think` 模型未关闭时输出空串 / 极慢（强制 `think:false` + 剥除 `<think>` 残留）。
- 修复 RRF 融合稀疏路噪声挤占 top4：稀疏无阈值时其 rank1 会压过 dense rank2，融合策略改为 dense 主力 + sparse 兜底（`SPARSE_FALLBACK_ONLY`，默认开启）。
- 修复 file URI 显示与链接混淆：OSC 8 分离——链接目标用 percent-encode 的合法 URI（机器用），显示用中文文件名（人用），避免裸路径空格截断 / `#` 被当 fragment。

### 📝 Documentation

- `README.md`：安装、快速开始、流水线模块、参数说明、实测数据、已知限制、扩展点。
- `DOC-01` ~ `DOC-06`：数据治理与预处理、零幻觉评估基准、Prompt 工程与护栏、硬件选型与容量、隐私合规、使用手册。
- `Mini-RAG 知识检索系统.md`、`本地轻量化 RAG 知识检索系统架构设计与工程实施规划书.md`。

### 🔧 Chores / Maintenance

- 配置集中化到 `mini_rag/config/settings.py`（单文件，不引 pydantic / 多份 yaml）。
- 数据契约用 `dataclass`；存储合并到 `core/store.py`（Chroma + SQLite FTS5 + manifest）。
- 精简实现取向：标准库优先，不引 LangChain / LlamaIndex / Streamlit。
- Embedding 吞吐按可用内存自适应预估（`EMBED_CHUNKS_PER_SEC` 系列常量）。
- 离线 / 遥测禁用（`ANONYMIZED_TELEMETRY`、`CHROMA_TELEMETRY`、`HF_HUB_OFFLINE` 等）。

---

格式说明：本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定，
分类沿用 Conventional Commits 的 emoji 风格。
