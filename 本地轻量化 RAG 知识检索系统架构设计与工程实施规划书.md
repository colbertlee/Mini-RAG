# 本地轻量化 RAG 知识检索系统架构设计与工程实施规划书 (Production-Ready Spec)

---

## 1. 系统概述与设计准则

### 1.1 项目定位
本项目是一个**面向本地文件夹、完全物理离线（Air-Gapped Ready）、轻量高效**的检索增强生成（RAG）知识问答系统。输入端由用户指定本地文件路径，向量化与 LLM 推理均通过本地部署的 **Ollama**（基于已部署的 `Qwen3.5:4b` 或 `gemma4:e4b`）运行，输出包含结构化知识点与精准可跳转的溯源标注。

### 1.2 核心设计准则
1. **100% 数据本地化与物理隔离**：LLM 推理、文本分块、Embedding 向量化、向量存储均在本地完成，杜绝任何外部网络请求。
2. **双重零幻觉控制（Zero-Hallucination Policy）**：
   * **检索层硬拦截（Hard Threshold Gating）**：若余弦相似度低于预设阈值，系统立即短路终止，不调用 LLM。
   * **生成层零温度约束（Deterministic Prompt Constraint）**：Prompt 强制限定只从上下文回答，`temperature=0.0`，未命中时统一输出标准提示语：“**知识库中未找到相关信息**”。
3. **确定性溯源（Full Provenance）**：每一条输出必须携带结构化引用出处（包含文件名、具体页码/段落、本地可直接点击的绝对路径 URI `file:///...`）。
4. **模块解耦与白盒化设计**：规范统一的数据接口（Pydantic Schema），禁止黑箱逻辑，便于自动化开发（如 Devin 交付）与单元验证。

---

## 2. 运行时技术栈与依赖基线

| 层次 | 选型组件 | 描述 / 版本要求 |
| :--- | :--- | :--- |
| **基础语言与环境** | Python 3.10+ | 强类型注解，支持标准异步与并发 |
| **本地模型运行时** | [Ollama](https://ollama.com/) | 绑定 `http://127.0.0.1:11434`，无外网依赖 |
| **LLM 生成大模型** | `Qwen3.5:4b` / `gemma4:e4b` | 已部署本地实例，支持运行时动态切换 |
| **Embedding 模型** | `nomic-embed-text` / 本地模型 Embed 接口 | 运行于本地 Ollama，提取稠密向量 |
| **向量数据库** | ChromaDB (Local Persistent) | 嵌入式进程级向量库，基于 DuckDB+Parquet/SQLite |
| **文档解析引擎** | `pypdf`, `python-docx`, `markdown` | 支持 PDF、Word、TXT、Markdown 原生解析 |
| **数据契约与校验**| `Pydantic v2` | 全生命周期对象校验与格式转化 |

---

## 3. 系统完整架构与数据流图

```
                           【阶段 1：知识库搭建】
[本地文件夹] ──> [多格式文档解析] ──> [元数据标记 + 分块切片] ──> [Ollama Embeddings] ──> [ChromaDB 本地持久化]
                                                                                               │
                                                                                               │
                           【阶段 2：检索与硬拦截】                                               │
[用户 Query] ──> [Query 向量化] ───────────────────────────────────────────────────────────────┤
                                                                                               │
                                                                                               ▼
                                                                                   [Top-K 相似度余弦检索]
                                                                                               │
                                              ┌────────────────────────────────────────────────┴───────────────────────────────┐
                                              │                                                                                │
                                   [最高得分 < 阈值 (0.60)]                                                         [最高得分 >= 阈值 (0.60)]
                                              │                                                                                │
                                              ▼                                                                                ▼
                                     【直接短路返回】                                                                 【阶段 3：受限生成与溯源】
                              "知识库中未找到相关信息"                                                              [组装 Strict Context + Prompt]
                                                                                                                               │
                                                                                                                               ▼
                                                                                                                    [Ollama LLM (Temp=0.0)]
                                                                                                                               │
                                                                                                                               ▼
                                                                                                                    [结构化输出 + 溯源标注]
```

---

## 4. 工程目录与文件结构

```text
mini_local_rag/
├── config/
│   ├── __init__.py
│   └── settings.py          # 全局静态与动态配置（路径、阈值、模型参数、提示词模板）
├── core/
│   ├── __init__.py
│   ├── document_loader.py   # 文件递归解析器（PDF 页码提取、DOCX/TXT 解析）
│   ├── text_splitter.py     # 文本切块器（滑动窗口分块、元数据绑定）
│   ├── embedder.py          # Ollama 本地向量生成客户端
│   ├── vector_store.py      # ChromaDB 本地持久化与增量存储管理
│   ├── retriever.py         # 相似度阈值计算与短路拦截器（防幻觉第一道防线）
│   └── generator.py         # 严格提示词组装与 LLM 推理器（防幻觉第二道防线）
├── models/
│   ├── __init__.py
│   └── schema.py            # Pydantic 核心数据模型契约
├── data/
│   ├── input_docs/          # 默认待索引的本地文件夹
│   └── vector_db/           # ChromaDB 本地索引持久化目录
├── main.py                  # CLI 交互入口、命令分发与状态机
├── requirements.txt         # 锁定的 Python 依赖
└── README.md                # 部署运行与操作指南
```

---

## 5. 模块详细设计与实现标准 (Devin 开发规范)

### 5.1 数据契约设计 (`models/schema.py`)
```python
from pydantic import BaseModel, Field
from typing import List, Optional

class DocumentChunk(BaseModel):
    """文本切片数据载体，包含严格的物理溯源信息"""
    chunk_id: str = Field(description="MD5(file_path + '_' + str(chunk_index))")
    content: str = Field(description="清洗后的文本切片内容")
    file_name: str = Field(description="源文件名，如: PowerStore_CLI_Guide.pdf")
    file_path: str = Field(description="源文件本地绝对路径")
    page_number: Optional[int] = Field(default=None, description="PDF 页码（从 1 开始），TXT/MD 为 None")
    chunk_index: int = Field(description="当前切片在文档内的顺序编号")

class RetrievalResult(BaseModel):
    """单条检索切片与对应相似度得分"""
    chunk: DocumentChunk
    similarity_score: float = Field(description="余弦相似度分数 (0.0 ~ 1.0)")

class Citation(BaseModel):
    """溯源引用模型"""
    file_name: str
    page_number: Optional[int]
    file_uri: str = Field(description="标准可点击本地 URI，如 file:///D:/docs/sample.pdf#page=5")

class QAResponse(BaseModel):
    """终端输出响应载体"""
    query: str
    answer: str
    is_fallback: bool = Field(default=False, description="是否因未匹配到信息而触发兜底回复")
    citations: List[Citation] = Field(default_factory=list)
```

---

### 5.2 配置管理 (`config/settings.py`)
```python
import os
from pydantic import BaseModel

class Settings(BaseModel):
    # 路径管理
    BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DEFAULT_DOCS_DIR: str = os.path.join(BASE_DIR, "data", "input_docs")
    VECTOR_DB_DIR: str = os.path.join(BASE_DIR, "data", "vector_db")

    # 本地 Ollama 服务配置
    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    DEFAULT_LLM_MODEL: str = "Qwen3.5:4b"          # 可切换为 gemma4:e4b
    DEFAULT_EMBED_MODEL: str = "nomic-embed-text"    # 本地 embedding 模型

    # 切块参数
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 50

    # 检索与零幻觉阈值
    TOP_K: int = 3
    SIMILARITY_THRESHOLD: float = 0.60              # 相似度低于此值强制短路

    # 生成参数
    TEMPERATURE: float = 0.0                        # 强制无随机性
    TOP_P: float = 0.1

    # 严格提示词模板
    SYSTEM_PROMPT_TEMPLATE: str = (
        "你是一个严谨的本地知识库检索助手。你的唯一任务是依据下方提供的【参考上下文】回答用户的问题。\n\n"
        "【必须遵守的铁律】：\n"
        "1. 你的回答必须 100% 严格来源于【参考上下文】，严禁基于你自身的先验知识进行推理、补充或发散。\n"
        "2. 如果【参考上下文】中的信息不足以完整回答问题，或者内容与问题无关，你必须直接回复：“知识库中未找到相关信息”，严禁输出任何额外猜测。\n"
        "3. 严禁编造任何命令、参数、专有名词或事实。\n\n"
        "【参考上下文】：\n"
        "{context}\n\n"
        "【用户问题】：\n"
        "{query}\n\n"
        "【你的回答】："
    )

settings = Settings()
```

---

### 5.3 核心处理三阶段实现规范

#### 阶段一：知识库搭建阶段
1. **文档加载器 (`core/document_loader.py`)**：
   * 递归遍历指定的本地目录。
   * **PDF 文件**：使用 `pypdf.PdfReader` 逐页读取，并在元数据中注入实际物理页码 `page_number = page_idx + 1`。
   * **DOCX 文件**：使用 `python-docx` 读取段落文本，按自然段合并提取。
   * **TXT/Markdown**：使用 `utf-8`（或自动探测编码）加载为单文档，`page_number = None`。
   * **容错机制**：遇到损坏或加密文件，打印告警日志并跳过，禁止抛出未捕获异常中断主流程。

2. **切分器 (`core/text_splitter.py`)**：
   * 采用滑动窗口算法，优先按 `\n\n`、`\n`、句号等符号切分。
   * 每个切片生成唯一哈希标识：`MD5(file_path + '_' + str(chunk_index))`。
   * 将文档元数据与切片内容一并打包为 `DocumentChunk` 实体。

3. **向量引擎与存储 (`core/embedder.py` & `core/vector_store.py`)**：
   * 封装 Ollama Embeddings API，按 Batch Size=32 分批次提交向量化计算。
   * 初始化 `chromadb.PersistentClient(path=settings.VECTOR_DB_DIR)`。
   * 使用余弦相似度集合：`client.get_or_create_collection(name="local_kb", metadata={"hnsw:space": "cosine"})`。
   * 存储内容包含：`ids`, `embeddings`, `documents` (即切片正文), `metadatas` (含 `file_name`, `file_path`, `page_number`, `chunk_index`)。

#### 阶段二：检索与防幻觉拦截阶段 (`core/retriever.py`)
1. **向量检索**：调用 Embedder 将用户的 Query 转化为向量，在 ChromaDB 中检索 `Top-K` 个切片。
2. **距离/相似度换算与硬过滤**：
   * ChromaDB 余弦距离（Cosine Distance $D$）换算为相似度分数：$Score = 1.0 - D$。
   * 提取所有命中切片中 $Score \ge settings.SIMILARITY\_THRESHOLD$ 的项。
3. **短路拦截（Early Exit）**：
   * **若没有切片满足阈值要求**，立即构建并返回：
     ```python
     QAResponse(
         query=query_text,
         answer="知识库中未找到相关信息",
         is_fallback=True,
         citations=[]
     )
     ```
   * 此分支直接跳过阶段三的 LLM 推理，从根本上杜绝模型编造。

#### 阶段三：受限生成与溯源阶段 (`core/generator.py`)
1. **上下文组装**：
   * 将通过硬拦截的有效切片拼装为格式化 Context：
     ```text
     [片段 1 (来源: VxRail_Guide.pdf, 第 12 页)]:
     ...切片内容...
     
     [片段 2 (来源: Brocade_Config.txt)]:
     ...切片内容...
     ```
2. **LLM 调用**：
   * 调用本地 Ollama API，指定模型（如 `Qwen3.5:4b`），设置 `temperature=0.0`。
   * 发送包含 `SYSTEM_PROMPT_TEMPLATE` 的完整请求。
3. **二次兜底校验与溯源标注生成**：
   * 如果 LLM 回复内容包含“知识库中未找到相关信息”或内容为空，标记 `is_fallback=True`，清空出处。
   * 若正常命中，生成标准引文：
     * **本地文件协议链接**：Windows 下格式为 `file:///C:/path/to/doc.pdf#page=12`，Linux/macOS 下为 `file:///path/to/doc.pdf#page=12`。

---

## 6. 用户交互界面与 CLI 设计 (`main.py`)

系统提供轻量级交互式命令行界面（REPL），支持动态指令与实时问答：

```python
"""
CLI 交互指令规范：
- :init <路径>     : 初始化/重新构建指定文件夹的本地知识库
- :model <模型名>  : 切换当前本地大模型（如 Qwen3.5:4b 或 gemma4:e4b）
- :threshold <值>  : 动态调整相似度拦截阈值（例如 0.65）
- :status          : 显示当前加载的文件夹、切片数量及活动模型
- :quit / :exit    : 退出系统
- <直接输入文本>   : 提交问题进行 RAG 知识检索与问答
"""
```

### 终端输出标准样式规范：

**场景 1：成功命中知识点**
```markdown
🤖 知识检索结果：
PowerStore 存储系统通过 CLI 收集日志时，应执行服务命令 `svc_journal` 或 `svc_dc`。
若需抓取全系统诊断数据包，推荐在主节点使用 `svc_collect_logs -d 3` 抓取近 3 天的事件日志。

---
📌 **引用出处（Sources）：**
* **[1] 文档名称**：《PowerStore_Service_Scripts.pdf》
  * **所在位置**：第 54 页 (切片 #8)
  * **本地路径**：`file:///D:/StorageManuals/PowerStore_Service_Scripts.pdf#page=54`
* **[2] 文档名称**：《CLI_Tool_Reference.md》
  * **所在位置**：全文片段 (切片 #2)
  * **本地路径**：`file:///D:/StorageManuals/CLI_Tool_Reference.md`
```

**场景 2：未命中知识库（触发零幻觉拦截）**
```markdown
🤖 知识检索结果：
知识库中未找到相关信息。
```

---

## 7. 部署依赖清单 (`requirements.txt`)

```text
pydantic>=2.5.0
chromadb>=0.4.22
pypdf>=3.17.0
python-docx>=1.1.0
markdown>=3.5.0
requests>=2.31.0
tqdm>=4.66.0
```

---

## 8. Devin 开发执行步骤与验收测试（Checklist）

Devin 在编写代码时必须严格依照以下顺序递进开发与自测：

- [ ] **Step 1: 环境与 Schema 建立**
  - 实现 `config/settings.py` 与 `models/schema.py`。
- [ ] **Step 2: 文档解析与切块（Document Ingestion）**
  - 实现 `core/document_loader.py`，支持多格式并严格记录 `page_number`。
  - 实现 `core/text_splitter.py`，确保切片携带完整物理元数据。
- [ ] **Step 3: 向量化与持久化（Vector Storage）**
  - 实现 `core/embedder.py` 对接本地 Ollama。
  - 实现 `core/vector_store.py`，确保 ChromaDB 本地存盘且增量支持。
- [ ] **Step 4: 零幻觉检索器（Gated Retriever）**
  - 实现 `core/retriever.py`，接入 `SIMILARITY_THRESHOLD` 硬拦截逻辑。
- [ ] **Step 5: 确定性生成与溯源（Strict Generator）**
  - 实现 `core/generator.py`，加载 Strict Prompt 模板，调用 `temperature=0.0` 模型，组装 `file:///` 协议链接。
- [ ] **Step 6: CLI 主交互开发与端到端验证**
  - 实现 `main.py`。
  - **验收测试 1（正向命中）**：在本地目录放入含专属业务配置的 PDF/TXT，提问文档内知识点，验证回答准确度与页码/URI 跳转正确性。
  - **验收测试 2（反向拦截）**：提问知识库内完全未记录的问题（如“某冷门型号交换机的默认登录 IP”），验证系统是否 100% 输出“**知识库中未找到相关信息**”，且没有任何幻觉编造。