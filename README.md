# Mini-RAG

本地轻量 RAG 检索系统：**解析 → 清洗 → 切片 → 向量化 → 入库 → 检索 → 生成**。
每个环节各自独立、可单独替换；全部配置集中在 `mini_rag/config/settings.py`。

设计取向是 **精简 + 零幻觉**：不引 LangChain / LlamaIndex，默认全本地离线
（Ollama + Chroma），检索不到证据就拒答而不是让模型编。

![Version](https://img.shields.io/badge/version-v0.2.1-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

**文档侧**：多格式解析（PDF / DOCX / HTML / MD / TXT）· 扫描件 OCR · 多栏恢复 ·
表格 / 公式提取 · 页眉页脚清洗 · 自适应三档切片（父子分层）。

**检索侧**：查询级证据闸 · L1 Query Rewrite · L2 HyDE · 多路 dense + gated sparse
全量 RRF（同文档票数封顶）· MMR 去冗余 · 四道零幻觉防线。

> 完整变更历史见 [CHANGELOG.md](CHANGELOG.md)；30 条基准的逐条结论见
> `_build/检索优化效果评估报告.html`。

---

## 1. 安装

```bash
# 用隔离环境（推荐）
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# 或作为包安装（含 mini-rag 命令行入口）
pip install -e .

# OCR 是可选依赖：不装也能跑，扫描页会自动跳过并在日志里标记
.venv/Scripts/pip install rapidocr-onnxruntime
```

运行前需确保本地 Ollama 已启动并拉取模型：

```bash
ollama pull qwen3-embedding:4b     # 向量化（默认，2560 维）
ollama pull qwen3.5:4b             # 生成
```

| 依赖 | 必须 | 作用 | 缺失时的行为 |
|---|---|---|---|
| `pymupdf` | 是 | PDF 解析、多栏恢复、表格提取 | 无法启动 |
| `chromadb` | 是 | dense 向量库 | 无法启动 |
| `jieba` | 是 | 稀疏路中文分词 | 无法启动 |
| `tiktoken` | 否 | `cl100k_base` token 计数 | 自动降级为字符启发式，切分仍可用 |
| `rapidocr-onnxruntime` | 否 | 扫描页 OCR | 扫描页跳过并记日志 |

---

## 2. 快速开始

```bash
# 0) 改语料目录：编辑 settings.py 的 INCLUDE_DIRS
#    INCLUDE_DIRS = [r"C:/path/to/your/docs"]

# 1) 先看状态（不加载模型）
python -m mini_rag.cli status

# 2) 单文件试切，人工检查切片质量（不加载模型，最快的质量验证方式）
python scripts/preview_chunks.py "C:/docs/manual.pdf" -n 10 --check

# 3) 小批量试入库，确认没问题再全量（入库前先腾内存到 4GB 以上）
python -m mini_rag.cli index --limit 5 --verbose

# 4) 全量增量入库
python -m mini_rag.cli index

# 5) 提问
python -m mini_rag.cli ask "如何配置快照？"
python -m mini_rag.cli ask "svc_factory_reset 的参数有哪些？" --debug
```

---

## 3. 文档侧流水线与模块

| 环节 | 文件 | 职责 | 可替换性 |
|---|---|---|---|
| 解析 | `core/parsers.py` | 按页路由数字原生/扫描件、多栏恢复、表格与公式提取 | 换 `find_tables` 实现即可 |
| 清洗 | `core/cleaner.py` | 页眉页脚学习、模板噪声、乱码、重复段落 | 改 `NOISE_PATTERNS` 即可 |
| 切片 | `core/splitter.py` | 按页数自适应分档、父子分层、overlap 落句子边界 | 改 `SPLIT_TIERS` 即可 |
| 向量化 | `core/embedder.py` | Ollama / OpenAI / BGE 三个 Provider | 改 `EMBED_PROVIDER` 即可 |
| 入库 | `core/store.py` | Chroma(dense) + SQLite FTS5(sparse) + manifest | 实现 `VectorStore` 接口 |
| 编排 | `core/pipeline.py` | 扫描、幂等、汇总报告 | — |

### 解析层的类型路由

逐页判定而非整份判定，因此**混合型 PDF**（部分页扫描、部分页原生）能正确分别处理：

```
每页 → 文字层字符数 < PDF_TEXT_LAYER_MIN_CHARS 且 页面含图像 ？
        是 → RapidOCR（渲染 DPI=200 → 识别 → 按 y 聚类成行）
        否 → PyMuPDF dict 提取 → 贪心分栏恢复阅读顺序 → 表格/公式/代码识别
```

**多栏恢复**用贪心分栏：按 `(y0, x0)` 排序后顺序扫描，若某块顶端高于「当前栏已到达的
最低点」，说明视线跳回页面上方 —— 即进入下一栏。最后按栏的左边界排序输出。
关闭 `MULTICOLUMN_ENABLED` 会退化为纯 y 序（左右栏会交错）。

**表格**用 PyMuPDF 自带的 `find_tables()`（省掉 pdfplumber 依赖），输出 Markdown，
并从正文中扣除表格区域内的文本块避免重复。跨页表格按「表头重复」判据拼接。

**公式**不引 Nougat/Marker（要拉 2GB torch）。改用启发式：数学符号密度 > 4% 或命中
LaTeX 片段时，保留原文并标 `chunk_type=formula`。实测 Dell 技术文档里公式占比极低，
为 0.7% 的内容拉 2GB 依赖不划算。

---

## 4. 检索架构（v0.2.x 核心）

检索不是「一次 embedding + 一次向量搜索」，而是一条带闸门的五段管线：

```
用户提问
   │
   ├─[0] 语料证据闸 ──── 判定无证据 ──→ 直接拒答（零模型开销，17~38ms）
   │      规则 G：查询英文 token 存在 DF=0（语料零出现）→ KB 不覆盖该专名
   │      规则 F：判别词集为空 且 中文内容词全部「DF=0 且不在中英词典」→ 无证据
   │
   ├─[1] L1 Query Rewrite → 产出多 query 变体
   │      中→英强信号词映射 + svc_xxx 命令归一 + 长查询拆分
   │      关键：保留英文专名（PowerStore / svc_xxx 原样输出，不翻译丢词）
   │
   ├─[2] L2 HyDE → +1 个假设性答案段落变体（救短查询 / 口语化查询）
   │      num_predict=80 + 45 词硬截断；LRU 缓存落盘 data/hyde_cache.jsonl
   │
   ├─[3] 多路召回（每个变体一路 dense，sparse 只跑一次）
   │      dense  : Chroma cosine，top 20
   │      sparse : SQLite FTS5 bm25，过双闸后才参与融合
   │               · SPARSE_MIN=8.0（绝对分下限）
   │               · 判别词命中（主闸）：必须命中 DF ≤ 10% 的英文判别词，
   │                 命中 powerstore/io 这类泛词不算证据
   │
   ├─[4] RRF 全量融合（k=60）+ 同文档票数封顶（RRF_DOC_VOTE_CAP=3）
   │      单路内同 doc_id 只有前 3 个 chunk 拿全额票，其后作废
   │      → 防 BM25 泛词把同文件块成堆塞进 top（堆叠淹没单票真值）
   │
   ├─[5] MMR 去冗余（相关性 = 融合排名归一化）
   │      用 dense 分当相关性会把 sparse-only 命中的块系统性挤出 top-N
   │
   └─[6] DENSE_MIN 硬阈值过滤 → FINAL_TOP_N=4 → 送 LLM
```

三层改造（Rewrite / HyDE / MMR）各自独立开关，**关掉任一层即退回该子层的 dense 单路**，
行为可回退到 v0.1.0，不会互相破坏。

| 文件 | 职责 |
|---|---|
| `core/query_rewrite.py` | L1：同义词扩展、命令归一、长查询拆分 |
| `core/hyde.py` | L2：假设性答案生成 + 落盘 LRU 缓存 |
| `core/retriever.py` | 证据闸、多路召回、RRF+封顶、MMR、阈值短路 |
| `core/generator.py` | Prompt 组装、受限生成、L3 生成后校验、引用溯源 |

### 为什么需要「票数封顶」

BM25 会把命中泛词的**同文件块成堆**塞进 top20。P24 实测：某 svc 文档 12+ 个块堆叠，
每块各拿 1 票，把唯一真值文档的单票（0.0164）压到 rank4 之外。

但封顶不能太狠 —— **堆叠 ≠ 噪声**：P20 的 ground-truth 文档自己就是 7+7 块堆叠冠军，
它最靠前的融合块是 sparse 第 3 位，cap=2 会把这块砍掉（0.0325 → 0.0164）反而 miss。
30 例 A/B 标定结果：

| cap | hit@1 | hit@3 | hit@10 | 结论 |
|---|---|---|---|---|
| 0（关闭） | 63.3 | 73.3 | 80.0 | P24 rank4 落榜 |
| 2 | 63.3 | 73.3 | 80.0 | 修 P24 破 P20，净零 |
| **3（默认）** | **66.7** | **76.7** | **80.0** | 修 P24 + P17 升 top1，无回退 |

文件级 hit@k 天然安全：`doc_id` = file_hash 前 16 位，同 doc 即同文件，
靠自身前 3 票永不落榜；跨变体共识每变体各 1 票，不受影响。

---

## 5. 零幻觉四道防线

「宁可拒答，不可编造」不是一句 prompt，而是四层可独立验证的机制：

| # | 防线 | 位置 | 机制 | 实测 |
|---|---|---|---|---|
| 1 | 语料证据闸 | 检索前 · 零模型 | 查询点名的专名在语料 DF=0 → 不等 embedding 直接拒 | N01/N02/N05，17~38ms |
| 2 | 硬阈值短路 | 检索后 | `DENSE_MIN=0.60` 过滤，无候选则不调 LLM | 正例最低 0.706 / 负例最高 0.436，无重叠 |
| 3 | LLM 语义拒答 | 生成中 | SYSTEM_PROMPT 规则 2：上下文无关或不完整 → 固定拒答串 | N03/N04 词法泄漏块被 LLM 自主判「答非所问」 |
| 4 | L3 生成后校验 | 生成后 · 零模型 | 规则校验失败 → **降级原文摘录**（第三态，不拒答也不编造） | 引用越界 / 推断话术 / 命令逐字比对 / 版本号 |
| — | 引用溯源 | 输出 | OSC 8 超链接：`file:///` URI + `#page=N`，可 Ctrl+Click 直达原文件 | — |

L3 校验失败**降级为原文摘录**而非拒答：纯原文同样零幻觉，但可用性远高于拒答。

---

## 6. 切片策略（按文档长度自适应）

| 档位 | 页数 | 子块 | overlap | 父块 | 切分依据 |
|---|---|---|---|---|---|
| `short` | <20 | 512 | 64 | 不生成 | 标题层级 + 段落 |
| `medium` | 20~100 | 768 | 96（12.5%） | 不生成 | 页/章节 |
| `long` | >100 | 512 | 64 | 1536 | 父子分层 |

**边界优先级**：标题层级 > 段落空行 > 句子边界 > 空格 > 字符兜底。
**overlap** 落在句子/段落边界：回退时按「整体单元」回退，绝不把句子拦腰切断。
**窗口语义**（与 LangChain 一致）：窗口 = chunk_size，步长 = chunk_size − overlap，
因此**含 overlap 后的最终 chunk 仍不超过上限**。

**父子分块**（仅 `long` 档）：子块送 Embedding 检索，命中后取父块作 LLM 上下文。
父块以 `is_parent=True` 入库但**不参与检索**（dense 侧 `where` 过滤、不进 FTS5），
否则与子块语义重叠会稀释召回。

> **标题是软边界，不是硬边界。** `SPLIT_HEADING_BREAK_RATIO=0.6` 表示
> 只有 buffer 已累积到 60% 时才在标题处断开。设成 0 会每个标题都硬切 —— 实测
> Dell 文档小节普遍只有几十 token，硬切会让 chunk 均值从 169 掉到 85。

---

## 7. 参数说明与调整方法

全部在 `mini_rag/config/settings.py`。改完**下次运行即生效**（检索侧参数无需重建索引）。

### 解析

| 参数 | 默认 | 作用 | 怎么调 |
|---|---|---|---|
| `PDF_TEXT_LAYER_MIN_CHARS` | 80 | 低于此字符数且含图像 → 判为扫描页 | 误判增多就调低；扫描件漏检就调高 |
| `OCR_ENABLED` | True | 扫描页是否走 OCR | 关掉则扫描页直接跳过 |
| `OCR_PROVIDER` | `rapidocr` | OCR 后端：`rapidocr`（本地）/ `llamaparse`（云端预留桩） | 云端会出网，见第 13 节 |
| `OCR_DPI` | 200 | OCR 渲染分辨率 | 150 省时 / 300 精度高但耗时约 2.3 倍 |
| `OCR_MAX_PAGES` | 80 | 单文档最多 OCR 页数（成本保护） | 千页扫描件才需要调高 |
| `OCR_MIN_CONFIDENCE` | 0.50 | OCR 行置信度过滤 | 识别噪声多就调高到 0.7 |
| `MULTICOLUMN_ENABLED` | True | 多栏阅读顺序恢复 | 单栏文档可关掉省一点时间 |
| `MULTICOLUMN_MIN_BLOCKS` | 6 | 少于此块数不尝试分栏 | — |
| `TABLE_ENABLED` | True | 表格提取 | 关掉则表格内容按正文处理 |
| `TABLE_CROSS_PAGE` | True | 跨页表格拼接 | — |
| `FORMULA_DETECT` | True | 公式启发式识别 | 误判多就关掉 |

### 清洗

| 参数 | 默认 | 作用 | 怎么调 |
|---|---|---|---|
| `CLEAN_ENABLED` | True | 清洗总开关 | 调试时可关掉对比原效果 |
| `HEADER_FOOTER_RATIO` | 0.30 | 某行在 ≥30% 的页出现于页首/页尾 → 页眉页脚 | 页眉变化多（分章节）就调低 |
| `HEADER_FOOTER_ZONE` | 0.12 | 页面上下各 12% 为候选区 | 页眉位置异常时调整 |
| `NOISE_PATTERNS` | 11 条 | 页码/版权/水印/目录点线的行级正则 | 直接加正则，无需改代码 |
| `DEDUP_PARAGRAPH` | True | 连续重复段落去重 | — |
| `TOC_DETECT` | True | 目录页整页跳过（导航不是正文） | 关闭则目录内容会混入 chunk |
| `TOC_LINE_RATIO` | 0.35 | 目录条目行占比 ≥ 此值判为目录页 | 正文误判就调高；漏判续页目录就调低 |

> 页码归一化是页脚能学到的关键：数字替换为 `#` 后，`Page 1 of 20` 与 `Page 2 of 20`
> 归并为同一个 key。不归一化的话每页都「不重复」，永远学不到页脚。

### 切片

| 参数 | 默认 | 作用 | 怎么调 |
|---|---|---|---|
| `SPLIT_TIERS` | 见第 6 节 | 三档页数上限/子块/overlap/父块 | 直接改元组，支持任意多档 |
| `TOKENIZER_ENCODING` | `cl100k_base` | tiktoken 编码 | 换 OpenAI embedding 时保持此值 |
| `SPLIT_HEADING_BREAK_RATIO` | 0.6 | 标题作为边界的触发阈值 | 想要严格按章节切就调低到 0.2 |
| `FIGURE_CAPTION_MIN_TOKENS` | 40 | 图注/表注 ≥ 此 token 才独立成块 | 碎片化严重就调高 |
| `CODE_MAX_TOKENS` | 768 | 代码块原子上限 | 实际生效值是 `min(此值, 档位 size)` |
| `TABLE_MAX_TOKENS` | 2048 | 表格原子上限 | 同上，受档位 size 约束 |
| `MIN_CHUNK_TOKENS` | 15 | 低于此值的碎片丢弃 | 碎片太多就调高 |

> **表格/代码块的硬上限是当前档位的 size**，不是 `TABLE_MAX_TOKENS`。
> 早期实现让表格独立用 2048，结果出现 979 token 的 chunk —— 超过 embedding
> 模型的有效长度会让向量质量塌掉，宁可按行切（每片都带表头）。

### 向量化与入库

| 参数 | 默认 | 作用 | 怎么调 |
|---|---|---|---|
| `EMBED_PROVIDER` | `ollama` | `ollama` / `openai` / `bge` | **换 provider 必须重建索引** |
| `EMBED_MODEL` | `qwen3-embedding:4b` | Ollama 模型名（2560 维） | — |
| `EMBED_BATCH` | 32 | 批量向量化大小 | 内存紧张调到 8 |
| `EMBED_KEEP_ALIVE` | `0` | embedding 模型驻留时长 | 批量建索引/评估设 `10m`，生产保持 `0` |
| `ON_DUPLICATE` | `skip` | hash 未变时跳过 / 强制更新 | 命令行 `--update` 可临时覆盖 |

> 换 provider 会改变向量维度（qwen3=2560 / OpenAI-small=1536 / bge-large=1024），
> 索引不兼容：`python -m mini_rag.cli index --rebuild`。
>
> `EMBED_KEEP_ALIVE` 可用环境变量 `MINIRAG_EMBED_KEEP_ALIVE` 覆盖。
> **踩过的坑**：30 条评估 × 每条重新加载 2.5GB embedding 模型 = 30 次内存冲击，
> Ollama 直接崩（WinError 10061），且崩前已跑的数据全丢。批量场景必须设 `10m`。

### 检索（v0.2.x 全部新增）

| 参数 | 默认 | 作用 | 怎么调 |
|---|---|---|---|
| `QUERY_REWRITE_ENABLED` | True | L1 查询改写 | 关 = 退回原始 query 单路 dense |
| `HYDE_ENABLED` | True | L2 假设性答案召回 | 关 = 不调 LLM 生成假设段落 |
| `MMR_ENABLED` | True | L3 多样性去冗余 | 关 = top-N 按 RRF 分数直接切 |
| `DENSE_MIN` | 0.60 | dense 相似度硬阈值（防线 2） | **换语料后必须重新标定** |
| `SPARSE_MIN` | 8.0 | sparse 绝对分下限 | 换语料重标 |
| `SPARSE_REQUIRE_EN_TOKEN` | True | sparse 主闸：必须命中判别词 | 语料非英文时关掉 |
| `SPARSE_UBIQUITOUS_RATIO` | 0.10 | 泛词/判别词分界（DF 占比） | 换语料重标 |
| `SPARSE_FALLBACK_ONLY` | False | False=全量融合；True=sparse 仅兜底（逃生舱） | 怀疑 sparse 噪声时开 True |
| `RRF_K` | 60 | RRF 平滑常数 | — |
| `RRF_DOC_VOTE_CAP` | 3 | 同文档票数封顶（0=关闭） | **改动需 30 例全量重校准** |
| `DENSE_TOP_K` / `SPARSE_TOP_K` | 20 / 20 | 各路召回深度 | — |
| `FINAL_TOP_N` | 4 | 最终进 LLM 的片段数 | 上下文够大可加到 6~8 |
| `HYDE_CACHE_ENABLED` / `_SIZE` | True / 200 | HyDE 落盘 LRU 缓存 | 评估对照组可关掉 |

> **⚠️ 换语料必重标定（铁律）**：`DENSE_MIN` / `SPARSE_MIN` / 证据闸 /
> `RRF_DOC_VOTE_CAP` 全部随语料漂移，本套数值只对 Powerstore 语料成立。
> 标定脚本见 `_build/calib_leak.py`。
>
> **关闭 HyDE 的正确姿势**：`--off-hyde` / `--off-rewrite` / `--off-mmr` 是
> `scripts/eval_retrieval.py` 的评估参数，**不是 CLI 参数**。生产环境要关 HyDE，
> 请在 `settings.py` 里设 `HYDE_ENABLED = False`。

---

## 8. Chunk 元数据

每个 chunk 都带以下字段，全部随向量写入 Chroma、同步写 SQLite，支持按
`doc_id` / `chunk_type` / `section_path` / `file_path` 过滤检索：

| 字段 | 说明 |
|---|---|
| `doc_id` | 文档唯一 id（file_hash 前 16 位）—— 同 doc 即同文件 |
| `doc_title` | 文件名去扩展名 |
| `page_start` / `page_end` | 起止物理页码（1 起）；跨页表格/父块两者不同 |
| `section_path` | 章节路径，如 `Chapter 3 > 3.2 Revenue Analysis` |
| `chunk_type` | `text` / `table` / `formula` / `figure_caption`（`code` 为内部扩展） |
| `chunk_index` | 文档内序号 |
| `parent_chunk_id` | 父块 id，无父层时为空 |
| `is_parent` | 是否父块（父块不参与检索） |
| `language` | `en` / `zh`（按 CJK 占比判定） |
| `created_at` | 入库时刻 ISO8601 |
| `has_code` / `has_warning` | 是否含代码 / 警示语 |
| `file_hash` / `token_estimate` | 内容哈希 / token 数 |

过滤检索示例：

```python
from mini_rag.core import embedder, store
vec = embedder.embed_query("snapshots")
# 只在某个文档里找表格
hits = store.dense_search(vec, top_k=10,
                          where={"doc_id": "e6ed8ed864a12879",
                                 "chunk_type": "table"})
# 命中子块后取父块作上下文
parents = store.dense_get([c.parent_chunk_id for c, _ in hits if c.parent_chunk_id])
```

> **改 metadata schema 必须 `index --rebuild`**：Chroma 的 `PersistentClient`
> 不迁移 schema。v0.1.0 曾踩过：旧 collection 存 8 字段，新 schema 17 字段，
> `where={'is_parent': False}` 全部空匹配返回 0 条 —— dense 主路一直是空气，
> 而表面上系统在「正常工作」（实际走 sparse 兜底）。
> 换语料或改 schema 后，务必按「`_meta` 写入 17 字段」确认新 collection 生效。

---

## 9. 评估与验证

```bash
# 切片质量断言（不加载模型，可接 CI）
python scripts/preview_chunks.py "C:/docs/manual.pdf" --check

# 检索逻辑单元测试（不依赖任何模型，可接 CI）—— 当前 38/38
python scripts/test_retrieval_logic.py

# 检索基准：30 条 query（15 中 + 5 英 + 5 混 + 5 负）
python scripts/eval_retrieval.py both --online --off-hyde

# 只复测失败 case / 分批跑
python scripts/eval_retrieval.py optimized --online --only P08,P15,P24
python scripts/eval_retrieval.py optimized --online --limit 10
```

离线 mock 模式（不传 `--online`）用确定性假向量，验证的是**管线逻辑与融合顺序**，
不是召回质量；质量数字一律以 `--online` 真机为准。

评估基准集在 `_build/eval_corpus.json`（v2，30 条），ground_truth 逐条核实过，
**查询描述与 ground_truth 一律以该文件为准**。

---

## 10. 幂等与增量

同一文档重复入库时按 **`doc_id` + `file_hash`** 检测：

- hash 未变且已成功 → 默认**跳过**（`ON_DUPLICATE = "skip"`）
- hash 变了 → 先删该文件的旧 dense/sparse 数据，再整体重建（避免残留孤儿 chunk）
- 命令行 `--update` 可强制重建未变化的文件

跑完输出汇总报告：新增 / 更新 / 跳过 / 失败 / 清理的计数，失败原因归类 Top N，
完整错误写 `logs/ingest_errors.jsonl`。单个文件解析失败只记录并跳过，不影响整批。

```bash
python -m mini_rag.cli index --rebuild      # 清空重建
python -m mini_rag.cli index --limit 20 -v  # 先小批量试
python -m mini_rag.cli purge                # 清空索引
```

---

## 11. 实测数据

### 解析侧（Powerstore 英文技术文档，v0.1.0 期实测）

| 项 | 实测值 |
|---|---|
| 文档数 / 总页数 | 319 份 / 21826 页（中位数 19 页，最大 1144 页） |
| 页数分布 | <20 页 161 份 / 20~100 页 116 份 / >100 页 42 份 |
| 扫描件 | 8 份（2.5%），混合型 PDF 真实存在 |
| 解析耗时 | 约 0.058 s/页（805 页文档 46 s） |
| 805 页 REST API 文档 | 2209 子块 + 218 父块；子块全部 ≤512，父块 1010~1566 |
| 19 页文档 | 36 chunk，均值 169 token，无超限 |

### 检索侧（30 条基准，Powerstore 60 文件采样 / 3469 chunk）

| 口径 | hit@1 | hit@3 | hit@10 |
|---|---|---|---|
| baseline（三开关全关，等价 v0.1.0） | 40.0% | 56.7% | 60.0% |
| **v0.2.x 终态（证据闸 + 融合 + cap=3）** | **66.7%** | **76.7%** | **80.0%** |

| 分类 | n | hit@1 | hit@3 | hit@10 | 平均延迟 |
|---|---|---|---|---|---|
| en_positive | 5 | 100% | 100% | 100% | 457 ms |
| zh_mix | 5 | 80% | 100% | 100% | 353 ms |
| zh_positive | 15 | 73.3% | 86.7% | 93.3% | 1251 ms |
| negative | 5 | 0% | 0% | 0% | 242 ms |

负例 hit 全为 0 即**全部拒答**，是期望结果。整体平均延迟 801 ms/query。

**融合 vs 单路**（30 条中 29 条含英文专名）：dense-only 60.0% / sparse-only 63.3% /
RRF 融合 73.3%。「稀疏路是净负债」的结论只对纯中文查询成立。

**零幻觉端到端 5/5**（真机 `ask` CLI，每例含 HyDE + 生成）：
N01/N02/N05 证据闸拒（17~38ms）· N03/N04 LLM 语义拒 · 全部引用可溯源。

> 五轮演进轨迹：40/56.7/60 → 53.3/76.7/76.7（融合 + MMR 修正）→ 63.3/73.3/80.0
> （+ 证据闸）→ 66.7/76.7/80.0（+ 票数封顶）。

---

## 12. 已知限制与残留

1. **碎片**：表格密集文档（如 REST API 参考）会产生大量 <50 token 的小表格 chunk。
   可调高 `MIN_CHUNK_TOKENS` 缓解。
2. **公式不是真 LaTeX**：只保留原文并标记类型，需要真 LaTeX 请接 Nougat/Marker。
3. **内存敏感**：可用内存 <2.5GB 时 embedding 吞吐从 6 chunks/s 掉到 0.4 chunks/s。
   **全量入库前必须腾内存到 4GB 以上**。
4. **Ollama 必须 `num_gpu=0`**：否则自动走 Vulkan 用显卡导致 `failed to allocate
   Vulkan0 buffer` 崩溃。embedding 与生成模型同时驻留会撑爆内存，故查询侧
   `keep_alive=0` 用完即卸。

**检索残留（已诊断，明确接受，勿重复排查）：**

| Case | 根因 | 决策 |
|---|---|---|
| P08 | BM25 长度归一 + 低 IDF 歧视操作手册类长步骤文档：GT 文档单词 `enclosure` BM25 仅 rank28-33（5.05），被安装手册/规格页高密度块（5.27~5.53）压制，融合后只剩 dense 单票 vs 对手双路票 → 挤出 top10 | 接受残留。修复候选：query token 命中 `file_name` 的文件级加权（推荐，需兼容 P15 的 `4-3-0-0` 连字符问题）/ FTS5 加权（要 rebuild）/ dense rank1 保底票（全局影响大） |
| P15 | 版本号共识 vs 单票，票数封顶救不了（不属堆叠问题） | 维持残留 |
| N03/N04 | 检索层词法泄漏（dense 侧已 100% 干净，sparse 侧泄漏） | 回答层兜住（LLM 语义拒答 + L3 降级），接受 |
| P02 | 检索命中但上下文无完整操作步骤 → 拒答 | **已拍板维持严格**：零幻觉优先于可用性，改 prompt 规则 2 会引入幻觉风险 |

---

## 13. 扩展点

**换 OCR**：改 `OCR_PROVIDER` 或实现 `core/parsers.py::_ocr_page` 的同签名函数
（Marker / PaddleOCR 都从这里接）。LlamaParse 云端接口已留桩
`core/parsers.py::_llamaparse_engine()`，返回一个对齐 RapidOCR 三元组的可调用对象即可。
云端方案会出网，违反本地隐私合规约束（DOC-05）。

**换 Embedding**：改 `EMBED_PROVIDER`，可选实现见 `core/embedder.py`。
OpenAI 与 BGE 的 Provider 已留好，装依赖即可用。

**换向量库**：实现 `core/store.py::VectorStore` 的四个方法（`upsert` / `query` /
`delete_by_file` / `count`）。`QdrantStore` 与 `MilvusStore` 是占位实现，缺包时会给出
明确的安装提示而非静默失败。

**明确不做**（范围边界，防止蔓延）：Web UI / 多轮对话记忆 / rerank 模型 / OCR 增强 /
全局摘要。Mini-RAG 只做一件事：RAG。

---

## 14. 发布

```bash
# 工作树必须干净；notes 从 _build/RELEASE_NOTES_<ver>.md 读取
bash scripts/release.sh v0.2.1 --notes _build/RELEASE_NOTES_v0.2.1.md
```

版本号是双源的，release 时两处需同步：`pyproject.toml [project].version` 与
`mini_rag/__init__.py` 的 `__version__`。

---

## 许可证

MIT
