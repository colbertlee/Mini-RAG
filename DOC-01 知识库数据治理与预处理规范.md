DOC-01: 知识库数据治理与预处理规范
文件编号: DATA-GOV-001
版本: v2.0.0（与实现同步 2026-09-04，此前 v1.1.0 为立项规划态）
适用范围: 本地文档解析引擎、文本清洗分块逻辑、向量元数据结构定义
对应实现: mini_rag/core/parsers.py · cleaner.py · splitter.py · store.py

> 说明：v1.1.0 是立项期的目标规范，部分条目与最终实现不同。本版已按落地代码校正，
> 凡「规划未落地」的条目均以 ⚠️ 标注，避免规范与实现继续背离。

1. 多源异构文档解析标准 (Document Parsing Pipeline)
按文件格式执行差异化提取，解析质量直接决定入库数据的天花板：

 用户指定文件夹 (INCLUDE_DIRS)
    │
    ├── [.pdf]  ──► PyMuPDF (逐页类型路由 + 贪心多栏恢复 + 物理页码硬绑定)
    ├── [.md]   ──► 标题/代码块扫描 (按 # 层级提取 section_path，``` 块原子化)
    ├── [.docx] ──► python-docx (段落与结构化表格逐行展平)
    └── [.txt]  ──► 编码探测 (UTF-8 优先 → 探测兜底 → replace 降级) + 双换行分段

文件类型白名单 EXT_ALLOWLIST = {.pdf, .md, .txt, .docx, .html, .htm}，
目录黑名单 EXCLUDE_DIRS 排除 node_modules / .venv / .git 等。
单文件超过 MAX_FILE_SIZE_MB = 50 跳过；单目录上限 MAX_DOCS_PER_DIR = 500。

1.1 PDF 解析约束（以官方产品手册 / 命令参考为主）
物理页码强关联：以文档物理 Page Index（从 1 起）为唯一坐标，
严禁多页合并后再统一切分。跨页表格 / 父块会产生 page_start ≠ page_end。

逐页类型路由（非整份判定，因此混合型 PDF 可正确处理）：
  文字层字符数 < PDF_TEXT_LAYER_MIN_CHARS(80) 且 页面含图像 → 扫描页 → RapidOCR
  否则 → PyMuPDF dict 提取 → 贪心分栏恢复阅读顺序 → 表格/公式/代码识别

多栏恢复：按 (y0, x0) 排序后顺序扫描，块顶端高于「当前栏已到达的最低点」
即判定视线跳回页首 = 进入下一栏，最后按栏左边界排序输出。
关闭 MULTICOLUMN_ENABLED 退化为纯 y 序（左右栏会交错）。

噪音清洗规则：
- 页眉/页脚剔除：行在 ≥30% 的页出现于页首/页尾区域（上下各 12%）即判为页眉页脚。
  页码归一化是关键：数字替换为 # 后 "Page 1 of 20" 与 "Page 2 of 20" 归并为同一 key，
  不归一化则每页都「不重复」，永远学不到页脚。
- 模板噪声正则：NOISE_PATTERNS 11 条，覆盖纯页码、版权行、confidentiality、
  as-is 免责声明、draft 标记等。直接加正则即可扩展，无需改代码。
- 断字重组（Hyphenation Fix）：已落地。正则 ([a-zA-Z])-\n([a-z])
  将 config-\nuration 还原为 configuration。
- 目录页整页跳过：显式 "Table of Contents" 标题，或目录条目行
  （纯章节号 / 点线+页码）占比 ≥ TOC_LINE_RATIO(0.35)。目录是导航不是正文。

1.2 Markdown / 文本文档解析约束
标题层级保留：按 # 层级维护祖先标题栈，拼装进该分块的 section_path 元数据
（例：PowerStore > PSTCLI Reference > volume modify）。
代码块保护：``` 包裹的内容整体作为原子 Segment，不参与正文切分。
编码：UTF-8 优先，失败后编码探测，最终 replace 降级 —— 宁可少量乱码也不整份失败。

2. 语义感知切分策略 (Semantic Chunking & Code Preservation)
切分按文档页数自适应三档，不是全局固定值（详见 README 第 6 节）：

  档位      页数        子块    overlap   父块
  short     <20         512     64        不生成
  medium    20~100      768     96        不生成
  long      >100        512     64        1536（父子分层）

边界优先级：标题层级 > 段落空行 > 句子边界 > 空格 > 字符兜底。
overlap 落在句子/段落边界 —— 回退时按整体单元回退，绝不把句子拦腰切断。
窗口语义（与 LangChain 一致）：窗口 = chunk_size，步长 = chunk_size − overlap，
因此含 overlap 后的最终 chunk 仍不超过上限。

2.1 代码块 / 表格的原子化与硬上限
代码块原子保护：由 ``` 包裹的 CLI 命令或配置片段视为不可分割单元；
超过上限时按行切分。表格同理（超限按行切，每片都带表头）。

  ⚠️ 关键修正（v1.1.0 的规划是错的）：硬上限取 min(CODE_MAX_TOKENS, 当前档位 size)，
  不是独立的 768。早期实现让表格独立用 2048，结果出现 979 token 的 chunk ——
  超过 embedding 模型有效长度会让向量质量塌掉，宁可按行切。

2.2 父子分块（仅 long 档）
子块送 Embedding 检索，命中后取父块作 LLM 上下文。
父块以 is_parent=True 入库但**不参与检索**（dense 侧 where 过滤、不进 FTS5），
否则与子块语义重叠会稀释召回。

2.3 高风险警示标记（部分落地）
已落地：splitter 用 WARN_RE 识别 WARNING / CAUTION / 高风险警示，
写入 has_warning 元数据；generator 在输出命令前加粗输出 **【高风险警示】**。

  ⚠️ 未落地（v1.1.0 规划）：「切分器必须将警示语句与后续紧邻的第一条命令
  强制绑定到同一 Chunk」。当前实现只做标记、不做强制绑定 ——
  强制绑定会破坏长度约束与边界优先级，收益不抵复杂度。

3. 元数据模式规范 (Metadata Schema Standard)
每个 chunk 写入 Chroma（dense）与 SQLite FTS5（sparse）时携带以下字段。
以下为**实现中的真实字段**，v1.1.0 列出的 doc_name / page_number / heading_path
已改名（代码中保留为只读兼容属性，仅供旧代码读取，构造时必须用新名）。

| 字段 | 类型 | 说明 | 用途 |
|---|---|---|---|
| chunk_id | str | chunk 唯一主键 | 去重、父块回取 |
| doc_id | str | 文档唯一 id（file_hash 前 16 位） | 文件级聚合、票数封顶依据 |
| doc_title | str | 文件名去扩展名 | 引用标注的文档名 |
| file_name | str | 含扩展名的文件名 | 溯源展示 |
| file_path | str | 源文件绝对路径 | OSC 8 file:/// 链接跳转 |
| page_start / page_end | int? | 起止物理页码（1 起） | 物理页码溯源 |
| section_path | str | 章节路径 | 章节上下文补充 |
| chunk_type | str | text / table / formula / figure_caption | 按类型过滤 |
| chunk_index | int | 文档内序号 | 顺序还原 |
| parent_chunk_id | str | 父块 id，无父层为空 | 父子回取 |
| is_parent | bool | 是否父块 | 父块不参与检索 |
| language | str | en / zh（按 CJK 占比判定） | 语言路由 |
| created_at | str | ISO8601 入库时刻 | 审计 |
| has_code / has_warning | bool | 含代码 / 含警示语 | 高风险提示 |
| file_hash / token_estimate | str / int | 内容哈希 / token 数 | 增量更新、长度校验 |

兼容别名（只读 property）：page_number → page_start，heading_path → section_path。

  ⚠️ 铁律：改 metadata schema 必须 index --rebuild 删 collection 重建。
  Chroma 的 PersistentClient 不迁移 schema。v0.1.0 曾踩坑：旧 collection 存 8 字段、
  新 schema 17 字段，where={'is_parent': False} 全部空匹配返回 0 条 ——
  dense 主路一直是空气，而系统表面「正常工作」（实际静默走 sparse 兜底）。

4. 落地状态对照（v1.1.0 规划 → v2.0.0 实现）

| 规范条目 | 状态 | 说明 |
|---|---|---|
| 物理页码硬绑定 | ✅ 已落地 | page_start / page_end |
| 页眉页脚剔除 + 页码归一化 | ✅ 已落地 | HEADER_FOOTER_RATIO / ZONE |
| 断字重组 | ✅ 已落地 | cleaner._DEHYPH |
| 目录页跳过 | ✅ 已落地 | TOC_DETECT / TOC_LINE_RATIO |
| 标题层级保留 | ✅ 已落地 | section_path |
| 代码块原子化 | ✅ 已落地 | 上限 min(768, 档位 size) |
| 父子分层 | ✅ 已落地 | 仅 long 档，父块不参与检索 |
| 高风险警示标记 | ✅ 已落地 | has_warning + prompt 加粗输出 |
| 警示语句与命令强制同块 | ⚠️ 未落地 | 仅标记，不强制绑定 |
| 固定 512/64 切分 | ❌ 已变更 | 改为按页数自适应三档 |
| pdfplumber 解析 | ❌ 已变更 | 改为 PyMuPDF find_tables |
| doc_name/page_number/heading_path | ❌ 已变更 | 改为 doc_title/page_start/section_path |
