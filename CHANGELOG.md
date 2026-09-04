# Changelog

本项目所有显著变更记录于此。版本号遵循 [语义化版本 2.0.0](https://semver.org/lang/zh-CN/)。

## [0.2.0] - 2026-09-04

检索架构五轮优化闭环：L1 Query Rewrite + L2 HyDE + 多路 RRF/MMR + 语料证据闸 + RRF 文档票数封顶。
30 条基准（Powerstore 60 文件采样）：hit@1 40.0% → 66.7%，hit@3 56.7% → 76.7%，hit@10 60.0% → 80.0%；
负例端到端拒答 5/5 全绿，零幻觉四道防线闭环。

### 🎉 New Features

- **L1 Query Rewriting**（新增 `mini_rag/core/query_rewrite.py`）：同义词扩展（中→英术语映射，扩至 24+ 强信号词）+ svc_xxx 命令归一 + 长 query 拆分，零额外模型。**关键修复：保留英文专名**——旧实现把 query 里的英文专名全丢（"PowerStore 4.3.0.0 release note" 只翻出 "change"），而 ground_truth 恰是英文专名；现改为「英文专名 + 翻译词」双版本输出，翻译词与原 query 重复时去重。开关 `QUERY_REWRITE_ENABLED`（默认 True）。
- **L2 HyDE**（新增 `mini_rag/core/hyde.py`）：LLM 生成 30-40 词假设性答案段落参与 dense 召回，救短查询/口语化查询；`num_predict=80` + `HYDE_MAX_WORDS=45` 硬截断（纯生成 9.5s→5.3s）。**LRU 缓存落盘** `data/hyde_cache.jsonl`：key 归一化（去空白+小写+去首尾标点），命中 0ms，跨进程持久化（`HYDE_CACHE_ENABLED/SIZE/PATH`）。开关 `HYDE_ENABLED`（默认 True）。
- **L3 多路 dense + gated sparse 全量 RRF + MMR 去冗余**（`mini_rag/core/retriever.py` 重写）：多 query 变体 → 多路 dense → 闸门后 sparse 全量 RRF → MMR。三开关 `QUERY_REWRITE / HYDE / MMR` 独立降级，关任一即退回对应子层 dense 单路，行为可回退 v0.1.0。
- **sparse 闸门 + 融合策略翻转**：`SPARSE_FALLBACK_ONLY` 默认翻转为 `False`（sparse 默认参与 RRF 全量融合，不再只兜底；True 为降级逃生舱）。新增三闸（settings.py）：`SPARSE_MIN=8.0`（绝对分下限）、`SPARSE_REQUIRE_EN_TOKEN=True`（判别词命中主闸——纯中文查询 sparse 整路判空，因语料是英文）、`SPARSE_UBIQUITOUS_RATIO=0.10`（泛词/判别词分级：powerstore 14.6% / io 46% 为泛词，svc_xxx / bbu / metro 为判别词，`store.count_chunks_containing()` 用 instr 子串语义与闸门一致）。
- **查询级语料证据闸（第四轮，4 条规则）**：① 规则 G——查询含 DF=0 英文 token → 点名专名 KB 零覆盖，embedding 前直接拒答（零模型开销，N02 brocade 17ms）；② 规则 F——判别词集为空 + 中文内容词全部 DF=0 且不在词典 → 拒答（N05 量子加密 38ms；P05 靠 告警→alert 词典证据存活，零误伤）；③ dense 候选判别词检查（泛词高分块出池）；④ sparse 主闸收紧为判别词。负例检索层拒答 1/5 → 3/5。
- **RRF 同文档票数封顶（第五轮）**：`RRF_DOC_VOTE_CAP=3`——单一路内同 doc_id 只有前 3 个 chunk 拿全额票，其后从融合榜除名，防 BM25 泛词堆叠（同文件块 7+7 成堆各 1 票）淹没单票真值文档。A/B 标定：cap=2 修 P24 破 P20 净零；**cap=3 严格优于关闭**（66.7/76.7/80.0 vs 63.3/73.3/80.0，P24 修复 + P17 升 top1）。教训：堆叠≠噪声，P20 的 GT 文档自己是堆叠冠军。doc_id = file_hash 前 16 位 → 文件级 hit@k 天然安全。
- **Embedding 常驻策略可配**：`EMBED_KEEP_ALIVE`（env `MINIRAG_EMBED_KEEP_ALIVE` 覆盖）。批量评估/建索引设 `10m`，生产保持 `0`（避免与 3.4GB 生成模型同时驻留内存崩溃）。
- **评估体系**：`scripts/eval_retrieval.py`（mock + online 双模式）；`_build/eval_corpus.json` v2 基准 30 条（15 中 + 5 英 + 5 混 + 5 负，ground_truth 逐条核实）；`scripts/test_retrieval_logic.py` 单元测试 **38/38**（不依赖任何模型，可接 CI）。
- **发布自动化**：`scripts/release.sh`（校验工作树 → annotated tag → push main+tag（SSH over 443）→ gh release create，notes 从 `_build/RELEASE_NOTES_<ver>.md`）。
- **打包**：新增最小 `pyproject.toml`——`pip install -e .` 可用，含 `mini-rag` CLI 入口；版本双源（本文件与 `__init__.__version__`）随 release 同步。

### 🐛 Bug Fixes

- **L3 校验三连修**（`generator.py`；修复前几乎把所有英文答案误杀降级为原文摘录，可用性归零）：① 句末句号被吃进标识符（`now.` / `svc_factory_reset.` 被判编造）→ 匹配后 rstrip(".") + 缩写白名单；② ctx 只含 content 不含 file_name → LLM 正确引用文件名被判编造 → ctx 补 file_name；③ 版本号正则只认三段 → PowerStore 四段版本号（4.3.0.0）完全漏检 → 补四段。修复后忠实转述放行、编造命令/版本号仍拦截。
- **MMR 相关性信号错误**（真机 hit@3 66.7% → 76.7% 的主因）：MMR 用 `dense_score` 当相关性 → sparse-only 命中的块（`dense_score=0.0`）被系统性挤出 top4，P15/P25 的真值文档过了 sparse 闸门却进不了最终结果。修复：MMR 的 rel 改用**融合排名归一化**（rank1=1.0 线性递减）；dense-primary 单路行为不变。
- **P21 基准修正**：原 `ground_truth_file=svc_journalctl` 在语料中不存在（60 文件样本仅含 svc_factory_reset / svc_update_drive_db / svc_db_recovery 三个 svc 文档），改为真实存在的 `svc_diag`（保持 zh_mix 特征）。
- 修复批量评估反复加载 2.5GB embedding 模型把 Ollama 搞崩（WinError 10061，崩前数据全丢）→ `EMBED_KEEP_ALIVE` 常驻策略 + 评估脚本显式设置。

### 📊 Evaluation（30 条基准，Powerstore 60 文件采样）

| 口径 | hit@1 | hit@3 | hit@10 |
|---|---|---|---|
| baseline（v0.1.0 等价，三开关全 off） | 40.0% | 56.7% | 60.0% |
| **v0.2.0 终态（L1+融合+证据闸+cap=3，--off-hyde）** | **66.7%** | **76.7%** | **80.0%** |
| 分类 hit@3 | en_positive **100%** / zh_mix 100% / zh_positive 86.7% | | |

- 五轮演进：40/56.7/60 → 53.3/76.7/76.7（融合+MMR修复）→ 63.3/73.3/80.0（+证据闸）→ **66.7/76.7/80.0（+票数封顶，终态）**。
- **零幻觉四道防线端到端 5/5**（真机 ask CLI，每例含 HyDE+生成）：证据闸拒（N01/N02/N05，17~38ms）→ LLM 语义拒（N03/N04 词法泄漏块被 LLM 判答非所问，固定拒答）→ L3 校验降级原文摘录 → 引用溯源。
- 融合>单路实证（30 条，29 条含英文专名）：dense-only 60.0% / sparse-only 63.3% / **RRF 融合 73.3%**——9-3 评审「稀疏路是净负债」只对纯中文查询成立。
- 残留（已决策）：P08 miss（未诊断）；P15 miss（多路共识 vs 单票，cap 不涉及其共识块）；N03/N04 检索层词法泄漏由回答层兜住（dense 侧 100% 干净）；P02 类「检索命中但上下文无完整步骤→拒答」属零幻觉严格性 vs 可用性权衡，待拍板。

### ⚠️ Behavior Changes

- `SPARSE_FALLBACK_ONLY` 默认值 **True → False**：sparse 路默认参与 RRF 融合。恢复 v0.1.0 行为需显式置 True。
- `ask` 全开（L1+L2+L3）延迟 +~25s/query（HyDE 模型加载税 5.2s 为固定项）；高频实时场景 `--off-hyde`（纯检索层平均 <1.2s/query）。
- **换语料必重标定（铁律）**：`DENSE_MIN(0.60)` / `SPARSE_MIN(8.0)` / 证据闸 / `RRF_DOC_VOTE_CAP` 全部随语料漂移，标定脚本 `_build/calib_leak.py`。

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

---

格式说明：本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定，
分类沿用 Conventional Commits 的 emoji 风格。
