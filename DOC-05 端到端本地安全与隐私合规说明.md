DOC-05: 端到端本地安全与隐私合规说明
文件编号: SEC-PRIV-005
版本: v2.0.0（与实现同步 2026-09-04，此前 v1.1.0 为立项规划态）
适用范围: 数据合规审计、网络隔离验证、本地运行安全保障
对应实现: mini_rag/config/settings.py（遥测与离线开关）· mini_rag/cli.py

1. 物理网络隔离与断网运行标准 (Air-Gap Compliance)

系统为 100% 离线自治，仅在本机回环地址内通信：

                    【完全离线物理边界 / Local Host Only】
 ┌───────────────────────────────────────────────────────────────────────┐
 │                                                                       │
 │  [ 用户本地文件夹 ]                                                    │
 │          │ (只读读取)                                                 │
 │          ▼                                                            │
 │  [ RAG 核心管道 ] ────(127.0.0.1:11434)────► [ 本地 Ollama 引擎 ]      │
 │          │                                   (qwen3.5:4b 本地推理)    │
 │          ▼                                                            │
 │  [ 本地向量数据库 ]                                                   │
 │    (ChromaDB + SQLite FTS5)                                           │
 │                                                                       │
 └───────────────────────────────────┬───────────────────────────────────┘
                                     │
                             ❌ [ 禁止任何外网连接 ]
                         (No External API / No Telemetry)

离线验证清单（settings.py 顶部已强制 `os.environ.setdefault`）：

| 项 | 实现 |
|---|---|
| 遥测禁用 | `ANONYMIZED_TELEMETRY=False` / `CHROMA_TELEMETRY=False` |
| 依赖库禁用动态拉取 | `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` |
| 本地回环不走代理 | `NO_PROXY=127.0.0.1,localhost` —— urllib 需 `ProxyHandler({})` 绕系统代理，否则 502 |
| 模型文件本地化 | 存放在本地 Ollama 模型库（`~/.ollama/models`） |

**唯一的出网例外**：`OCR_PROVIDER = "llamaparse"` 是**预留桩**，启用后会调用云端
LlamaParse，违反本地隐私约束。默认 `rapidocr`（纯本地）。启用前需评估合规。

注：v1.1.0 提到的 LangChain 遥测项不适用 —— 本项目不引入 LangChain / LlamaIndex。

2. 本地文件系统安全与防遍历控制 (Filesystem Hardening)

只读隔离：解析用户指定目录时仅读取，**无任何写回源文件的代码路径**。
系统只向 `data/`（索引）与 `logs/`（日志）写入自有产物。

路径遍历防护：语料目录由用户在 `settings.INCLUDE_DIRS` 中显式声明，
不使用相对路径拼接；`EXCLUDE_DIRS` 排除 `node_modules` / `.venv` / `.git` /
`site-packages` 等非业务目录。

文件类型白名单：`EXT_ALLOWLIST = {.pdf, .md, .txt, .docx, .html, .htm}`，
不解析 `.exe` / `.sh` / `.py` 等可执行文件。
附加保护：`MAX_FILE_SIZE_MB = 50`（超大文件跳过）、
`MAX_DOCS_PER_DIR = 500`（单目录上限）。

3. 数据与会话生命周期管理 (Data Lifecycle)

无痕会话：默认情况下，聊天过程中的 Prompt 与上下文仅停留在 Python 进程内存中，
进程退出即释放。

⚠️ **一个落盘例外（v1.1.0 未覆盖）**：HyDE 缓存会写入
`data/hyde_cache.jsonl`，内容是 LLM 生成的**假设性答案段落**（与查询语义相关）。
这是为了跨进程复用（CLI 每次都是新进程，不落盘的缓存无效），命中可省 ~5s/次。

- 内容不含原始文档正文，但**含查询语义的改写文本**；
- 受 `HYDE_CACHE_ENABLED` 控制（设 False 即完全不落盘）；
- LRU 上限 `HYDE_CACHE_SIZE = 200` 条，超出淘汰最久未用。

清除命令（v1.1.0 写的 `rag_tool.py --purge` 不存在，正确命令如下）：

```bash
python -m mini_rag.cli purge          # 清空向量索引 + 稀疏索引 + manifest
python -m mini_rag.cli purge --yes    # 跳过确认
```

如需彻底清除 HyDE 缓存，删除 `data/hyde_cache.jsonl` 即可（系统在索引重建时不影响它）。

4. 溯源与可审计性

每次回答附带的引用由 `generator._citations()` **程序化生成**（不是 LLM 自造），
因此天然 100% 可溯源：

- `file_uri`：OSC 8 超链接，格式为 percent-encode 的 `file:///` URI + `#page=N`，
  终端 Ctrl+Click 直达原文件对应页；
- 显示文本用中文文件名（人读），链接目标用合法 URI（机器读），二者分离 ——
  避免裸路径空格截断 / `#` 被误判为 fragment。

5. 合规边界小结

| 维度 | 状态 |
|---|---|
| 原始文档外传 | ❌ 无（仅本地读取） |
| 查询内容外传 | ❌ 无（HyDE 与生成均走本地 Ollama） |
| 遥测上报 | ❌ 已禁用 |
| 云端依赖 | ⚠️ 仅 `llamaparse` OCR 预留桩（默认关闭） |
| 磁盘残留 | ⚠️ 索引 `data/`、日志 `logs/`、HyDE 缓存 `data/hyde_cache.jsonl` |
| 一键清除 | ✅ `python -m mini_rag.cli purge` |
