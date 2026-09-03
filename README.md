# Mini-RAG 文档处理流水线

本地轻量 RAG 的文档侧流水线：**解析 → 清洗 → 切片 → 向量化 → 入库**。
五个环节各自独立，可单独替换；全部配置集中在 `mini_rag/config/settings.py`。

设计取向是 **精简 + 零幻觉**：不引 LangChain / LlamaIndex，默认全本地离线
（Ollama + Chroma），检索不到证据就拒答而不是让模型编。

![Version](https://img.shields.io/badge/version-v0.1.0-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

**核心能力**：多格式解析（PDF / DOCX / HTML / MD / TXT）· 扫描件 OCR · 多栏恢复 ·
表格 / 公式提取 · 页眉页脚清洗 · 自适应三档切片（父子分层）· 双路检索
（dense + 稀疏 RRF 融合）· 零幻觉硬阈值短路拒答 · 本地离线（Ollama + Chroma）。

> 完整变更历史见 [CHANGELOG.md](CHANGELOG.md)。

---

## 1. 安装

```bash
# 用隔离环境（推荐）
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# OCR 是可选依赖：不装也能跑，扫描页会自动跳过并在日志里标记
.venv/Scripts/pip install rapidocr-onnxruntime
```

运行前需确保本地 Ollama 已启动并拉取模型：

```bash
ollama pull qwen3-embedding:4b     # 向量化（默认）
ollama pull qwen3.5:4b             # 生成
```

| 依赖 | 必须 | 作用 | 缺失时的行为 |
|---|---|---|---|
| `pymupdf` | 是 | PDF 解析、多栏恢复、表格提取 | 无法启动 |
| `chromadb` | 是 | dense 向量库 | 无法启动 |
| `tiktoken` | 否 | `cl100k_base` token 计数 | 自动降级为字符启发式，切分仍可用 |
| `rapidocr-onnxruntime` | 否 | 扫描页 OCR | 扫描页跳过并记日志 |
| `jieba` | 是 | 稀疏路中文分词 | 无法启动 |

---

## 2. 快速开始

```bash
# 0) 改语料目录：编辑 settings.py 的 INCLUDE_DIRS
#    INCLUDE_DIRS = [r"C:/path/to/your/docs"]

# 1) 先看状态（不加载模型）
python -m mini_rag.cli status

# 2) 单文件试切，人工检查切片质量（不加载模型，最快的质量验证方式）
python scripts/preview_chunks.py "C:/docs/manual.pdf" -n 10 --check

# 3) 小批量试入库，确认没问题再全量
python -m mini_rag.cli index --limit 5 --verbose

# 4) 全量增量入库
python -m mini_rag.cli index

# 5) 提问
python -m mini_rag.cli ask "如何配置快照？"
```

---

## 3. 流水线与模块

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

## 4. 切片策略（按文档长度自适应）

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

## 5. 参数说明与调整方法

全部在 `mini_rag/config/settings.py`。改完**下次运行即生效**，无需重建索引的项已标注。

### 解析

| 参数 | 默认 | 作用 | 怎么调 |
|---|---|---|---|
| `PDF_TEXT_LAYER_MIN_CHARS` | 80 | 低于此字符数且含图像 → 判为扫描页 | 误判增多就调低；扫描件漏检就调高 |
| `OCR_ENABLED` | True | 扫描页是否走 OCR | 关掉则扫描页直接跳过 |
| `OCR_PROVIDER` | `rapidocr` | OCR 后端：`rapidocr`（本地）/ `llamaparse`（云端预留桩） | 云端会出网，见扩展点第 10 节 |
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
| `TOC_LINE_RATIO` | 0.35 | 目录条目行（`1.1`/点线+页码）占比 ≥ 此值判为目录页 | 正文误判就调高；漏判续页目录就调低 |

> 页码归一化是页脚能学到的关键：数字替换为 `#` 后，`Page 1 of 20` 与 `Page 2 of 20`
> 归并为同一个 key。不归一化的话每页都「不重复」，永远学不到页脚。

### 切片

| 参数 | 默认 | 作用 | 怎么调 |
|---|---|---|---|
| `SPLIT_TIERS` | 见上表 | 三档页数上限/子块/overlap/父块 | 直接改元组，支持任意多档 |
| `TOKENIZER_ENCODING` | `cl100k_base` | tiktoken 编码 | 换 OpenAI embedding 时保持此值 |
| `SPLIT_HEADING_BREAK_RATIO` | 0.6 | 标题作为边界的触发阈值 | 想要严格按章节切就调低到 0.2 |
| `FIGURE_CAPTION_MIN_TOKENS` | 40 | 图注/表注 ≥ 此 token 才独立成 `figure_caption` 块 | 碎片化严重就调高；想保留短图注可检索就调低 |
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
| `ON_DUPLICATE` | `skip` | hash 未变时跳过 / 强制更新 | 命令行 `--update` 可临时覆盖 |
| `DENSE_MIN` | 0.60 | dense 相似度硬阈值（防幻觉第一道防线） | **换语料后必须重新标定** |

> 换 provider 会改变向量维度（qwen3=2560 / OpenAI-small=1536 / bge-large=1024），
> 索引不兼容：`python -m mini_rag.cli index --rebuild`。

---

## 6. Chunk 元数据

每个 chunk 都带以下字段，全部随向量写入 Chroma、同步写 SQLite，支持按
`doc_id` / `chunk_type` / `section_path` / `file_path` 过滤检索：

| 字段 | 说明 |
|---|---|
| `doc_id` | 文档唯一 id（file_hash 前 16 位） |
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

---

## 7. 验证脚本

```bash
# 基本用法：打印每个 chunk 的 token 数、元数据、文本预览
python scripts/preview_chunks.py "C:/docs/manual.pdf"

# 跑验收断言（失败退出码 1，可接 CI）
python scripts/preview_chunks.py "C:/docs/manual.pdf" --check

# 对比不同档位的切片效果（同一份文件按 >100 页策略切）
python scripts/preview_chunks.py "C:/docs/manual.pdf" --tier long

# 看解析后的原始 block（判断多栏/表格/公式识别是否正确）
python scripts/preview_chunks.py "C:/docs/manual.pdf" --segments 20

# 导出 JSON 供程序化检查
python scripts/preview_chunks.py "C:/docs/manual.pdf" --json out.json
```

`--check` 的断言直接对应验收标准：

- 子块未超 token 上限
- `chunk_type` 全部合法
- `doc_id` / `doc_title` 齐全
- `long` 档：父子关系完整、父块包含子块内容、父块未超 2048 token

等价的 CLI 命令是 `python -m mini_rag.cli preview <文件>`（功能相同，参数略少）。

---

## 8. 幂等与增量

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

## 9. 实测数据（PowerStore 319 份真实英文技术文档）

| 项 | 实测值 |
|---|---|
| 文档数 / 总页数 | 319 份 / 21826 页（中位数 19 页，最大 1144 页） |
| 页数分布 | <20 页 161 份 / 20~100 页 116 份 / >100 页 42 份 |
| 扫描件 | 8 份（2.5%），混合型 PDF 真实存在 |
| 解析耗时 | 约 0.058 s/页（805 页文档 46 s） |
| 805 页 REST API 文档 | 2209 子块 + 218 父块；子块全部 ≤512，父块 1010~1566 |
| 19 页文档 | 36 chunk，均值 169 token，无超限 |

### 已知限制

1. **碎片**：表格密集的文档（如 REST API 参考）会产生大量 <50 token 的小表格 chunk。
   这是「表格独立成 chunk」的必然结果，可调高 `MIN_CHUNK_TOKENS` 缓解。
2. **公式不是真 LaTeX**：只保留原文并标记类型，需要真 LaTeX 输出请接 Nougat/Marker。
3. **本机内存敏感**：可用内存 <2.5GB 时 embedding 吞吐会从 6 chunks/s 掉到 0.4 chunks/s。
   全量入库前建议先腾内存到 4GB 以上。
4. **Ollama 必须用 `num_gpu=0`**：否则会自动走 Vulkan 用显卡导致崩溃；
   embedding 与生成模型同时驻留会撑爆内存，故查询侧用 `keep_alive=0` 用完即卸载。

---

## 10. 扩展点

**换 OCR**：改 `OCR_PROVIDER` 或实现 `core/parsers.py::_ocr_page` 的同签名函数
（Marker / PaddleOCR 都从这里接）。LlamaParse 云端接口已留桩
`core/parsers.py::_llamaparse_engine()`，返回一个对齐 RapidOCR 三元组的可调用对象即可。
云端方案会出网，违反本地隐私合规约束（DOC-05）。

**换 Embedding**：改 `EMBED_PROVIDER`，可选实现见 `core/embedder.py`。
OpenAI 与 BGE 的 Provider 已留好，装依赖即可用。

**换向量库**：实现 `core/store.py::VectorStore` 的四个方法（`upsert` / `query` /
`delete_by_file` / `count`）。`QdrantStore` 与 `MilvusStore` 是占位实现，缺包时会给出
明确的安装提示而非静默失败。
