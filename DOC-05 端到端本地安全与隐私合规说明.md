DOC-05: 端到端本地安全与隐私合规说明
文件编号: SEC-PRIV-005
版本: v1.1.0
适用范围: 数据合规审计、网络隔离验证、本地运行安全保障

1. 物理网络隔离与断网运行标准 (Air-Gap Compliance)
本系统设计为 100% 离线自治系统，杜绝任何数据回传。

                    【完全离线物理边界 / Local Host Only】
 ┌───────────────────────────────────────────────────────────────────────┐
 │                                                                       │
 │  [ 用户本地文件夹 ]                                                    │
 │          │ (只读读取)                                                 │
 │          ▼                                                            │
 │  [ RAG 核心管道 ] ────(127.0.0.1:11434)────► [ 本地 Ollama 引擎 ]      │
 │          │                                   (Qwen / BGE 本地推理)   │
 │          ▼                                                            │
 │  [ 本地向量数据库 ]                                                   │
 │    (ChromaDB SQLite)                                                  │
 │                                                                       │
 └───────────────────────────────────┬───────────────────────────────────┘
                                     │
                             ❌ [ 禁止任何外网连接 ]
                         (No External API / No Telemetry)
离线验证清单：
模型文件必须存放在本地 Ollama 模型库目录（如 ~/.ollama/models）。
依赖库禁用动态拉取：Embedding 模型必须配置离线加载模式（local_files_only=True）。
遥测禁用：所有第三方开源组件（如 Chroma、LangChain 等）必须配置环境变量 ANONYMIZED_TELEMETRY=False。
2. 本地文件系统安全与防遍历控制 (Filesystem Hardening)
只读隔离（Read-Only Access）：
系统在解析用户指定的文档目录时，仅以 r（Read Only）模式打开文档，禁止任何覆写、修改或删除操作。
路径遍历防护（Path Traversal Guard）：
用户传入的文件夹路径必须经过规范化解析（os.path.realpath / pathlib.Path.resolve()）。
严禁通过相对路径跳转（如 ../../etc/）越界读取操作系统核心文件。
文件类型白名单：
仅扫描 .pdf、.md、.txt、.docx 扩展名，严格拒绝解析 .exe、.sh、.py 等可执行文件，防止潜在的代码注入。
3. 数据与会话生命周期管理 (Data Lifecycle)
无痕会话（Stateless Memory）：
默认情况下，聊天过程中的 Prompt 与上下文回答仅停留在 Python 进程的 RAM（内存）中。
用户退出程序或终端后，内存自动释放，不向磁盘写入明文聊天日志。
一键脱敏与索引清理：
提供安全清除指令 python rag_tool.py --purge，自动清理向量缓存目录及解析产生的临时缓存文件。