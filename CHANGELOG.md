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
- 修复 `index --rebuild` 残留旧 chunk：`rebuild_all` 先实例化 client 再删 collection，避免旧向量污染新语料（`_col()` 改判 `_collection is None`，旧版判 `_client is None` 在 CLI 进程下永远成立，跳过删除）。
- 修复 Chroma metadata schema 不迁移导致 dense 主路一直是空气：旧 collection 只存 8 字段（page_number / heading_path 等），新 schema 17 字段（page_start / section_path / is_parent 等），`where={'is_parent': False}` 全部空匹配返回 0。**铁证**：dense_top1=0 时实际走 sparse 兜底。修复：换语料或改 schema 后必须 `index --rebuild`，且必须按"`_meta` 写 17 字段"确认新 collection。
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

## [Unreleased]

### 🎉 New Features

- **L1 Query Rewriting**：同义词扩展（中文→英文术语映射）+ svc_xxx 命令归一 + 长 query 拆分，零额外模型。
  - 新增 `mini_rag/core/query_rewrite.py`
  - 新增开关 `settings.QUERY_REWRITE_ENABLED`（默认 `True`）
- **L2 HyDE（Hypothetical Document Embeddings）**：调 LLM 生成假设性答案段落参与 dense 召回，救短查询 / 口语化查询。
  - 新增 `mini_rag/core/hyde.py`
  - 新增开关 `settings.HYDE_ENABLED`（默认 `True`）
- **L3 多路 dense + RRF 融合 + MMR 去冗余**：`mini_rag/core/retriever.py` 重写为多 query → 多 dense → RRF → MMR 链路，零幻觉底线（DENSE_MIN / sparse 兜底 / L3 校验）全部保留。
  - 新增开关 `settings.MMR_ENABLED`（默认 `True`）
  - 三个开关独立降级：任一关掉即退回对应子层的 dense 单路，行为可回退到 v0.1.0
- **检索效果评估脚本**：`scripts/eval_retrieval.py`，mock + online 双模式。
- **检索逻辑单元测试**：`scripts/test_retrieval_logic.py`，12/12 通过，不依赖任何模型。

### 📊 Evaluation（2026-09-04 真机数据）

**真机评估 30 条基准（Powerstore 60 文件采样）**：
- baseline（v0.1.0 等价，rewrite+hyde+mmr 全 off）：hit@1=40.0% / hit@3=56.7% / hit@10=60.0%，latency 7.6s
- optimized L1+MMR（P01-P10，hyde=off）：hit@1=70.0% / hit@3=90.0% / hit@10=100%，latency 35.0s
- optimized full L1+L2+L3（P11-P25+en+mix）：hit@1=46.7% / hit@3=73.3% / hit@10=80.0%，latency 32.9s
- optimized full L1+L2+L3（5 条负例）：hit@1=hit@3=hit@10=0%，**零幻觉守住**

**P11-P25 同子集对照（baseline vs full L1+L2+L3，15 条）**：hit@1 持平、hit@3 +13.3%、hit@10 +13.3%。

**P01-P10 同子集对照（baseline vs L1+MMR，10 条）**：hit@1 +20%、hit@3 +30%、hit@10 +30%。

**结论**：三开关全开对 hit@3 / hit@10 是显著提升（+12%~+30%），对 hit@1 提升有限（0%~+20%），代价每次查询多 25s。个人技术知识库使用模式（用户主动 ask）推荐全开；高频实时场景切 `--off-hyde`。

**典型翻盘 case**（真实查询，已按 eval_corpus.json 核实）：
- P05 "PowerStore 数据库卷故障告警"：baseline 0/1/1 → L1+MMR 1/1/1
- P08 "PowerStore base enclosure 更换流程"：baseline 0/0/0 → L1+MMR 1/1/1
- P17 "svc_db_recovery service script"：baseline 0/0/0 → full 0/1/1（HyDE 推入 top3）
- P19 "NDU from version 3.x to 3.x performance metrics not displayed"：baseline 0/0/1 → full 1/1/1

详细报告：`_build/检索优化效果评估报告.html`。

### 🔧 第二轮深度优化（2026-09-04 下午）

用户拍板推进 4 项：扩 L1 词典、HyDE 压缩、HyDE 缓存、L3 校验。落地过程中连带发现并修复了更深的 bug。

- **扩 L1 词典 + 保留英文专名**（`query_rewrite.py`）：
  - `_ZH_TO_EN` 扩 24 词（升级/失败/重启/型号/规格/查看/状态/变更/版本/安装/配置/控制器/磁盘…）
  - **关键修复**：旧实现把 query 里的英文专名全丢了（"PowerStore 4.3.0.0 release note" 只翻出 "change"），
    而 ground_truth 恰恰是那些英文专名。现改为「英文专名 + 翻译词」双版本输出。
  - 去重：翻译词若已在原 query 英文片段里，不再重复（避免 "BBU ... BBU battery"）
- **HyDE 段落压缩**（`hyde.py`）：prompt 60-80 词 → 30-40 词 + `num_predict=80` + `HYDE_MAX_WORDS=45` 硬截断。
  - 实测：纯生成 9.5s→5.3s（真减半），但总 latency 14.7s→10.5s（-29%）——模型加载税 5.2s 是固定项，与段落长度无关。
- **HyDE LRU 缓存落盘**（`hyde.py`）：`data/hyde_cache.jsonl`，命中 0ms，跨进程持久化。
  - 新增 `HYDE_CACHE_ENABLED / HYDE_CACHE_SIZE / HYDE_CACHE_PATH`（settings.py）
  - key 归一化（去空白 + 小写 + 去首尾标点）
- **L3 校验核查 + 修 3 个真 bug**（`generator.py`）：
  - ① 句末句号被吃进标识符（`now.` / `svc_factory_reset.` 被判为编造）→ 匹配后 rstrip(".") + 缩写白名单
  - ② ctx 只含 content 不含 file_name → LLM 正确引用文件名被判编造 → ctx 补 file_name
  - ③ 版本号正则只认三段，PowerStore 四段版本号（4.3.0.0）完全漏检 → 补四段
  - 修复前 L3 几乎把所有英文答案误杀降级为原文摘录（可用性归零）；修复后忠实转述放行、编造命令/版本号仍拦截。
- **embedding 常驻策略可配**（`embedder.py` + settings）：`EMBED_KEEP_ALIVE`（环境变量 `MINIRAG_EMBED_KEEP_ALIVE` 覆盖）。
  - 踩坑：30 条评估每条重新加载 2.5GB embedding 模型，反复内存冲击把 Ollama 搞崩（WinError 10061），崩前数据全丢。
  - 批量评估/建索引设 `10m`；生产保持 `0`（避免与 3.4GB 生成模型同时驻留）。

### 📊 第二轮评估关键发现：融合策略 > 单路

针对 P15/P25 深挖，牵出对 9-3 架构评审「稀疏路是净负债」结论的修正。全量 30 条实测（纯检索层，不含 HyDE）：

| 策略 | 30 条命中率 |
|---|---|
| dense-only（SPARSE_FALLBACK_ONLY=True，当前生产） | 60.0% |
| sparse-only（BM25） | 63.3% |
| **RRF 融合（dense + sparse）** | **73.3%** |

- 9-3 结论「本组合下稀疏路是净负债」**只对了一半**：那是纯中文查询测出的。本基准 30 条里 29 条含英文专名，sparse 对英文术语精确匹配极强。
- **P15**（版本号 4.3.0.0）：dense 排 14 / sparse 排 2 → 融合命中
- **P25**（PPT 转 EKT 幻灯片，dense 抓不住）：dense 不在 top20 / sparse 排 1 → 融合命中
- **P06**：dense 排 5 / sparse 排 1 → 融合独家命中
- 负例 N01-N05 融合后仍 0% 命中，零幻觉守住。

**待办（本轮发现，尚未落地）**：
1. 融合策略改「dense + sparse 全量 RRF」，但必须给 sparse 加阈值闸门（9-3 已发现无阈值时噪声挤进 top4）
2. 修正 P21 基准：语料（60 文件采样）里没有 svc_journalctl 文档，需换成真实存在的 svc 文档

---

格式说明：本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定，
分类沿用 Conventional Commits 的 emoji 风格。
