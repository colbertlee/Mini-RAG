DOC-01: 知识库数据治理与预处理规范
文件编号: DATA-GOV-001
版本: v1.1.0
适用范围: 本地文档解析引擎、文本清洗分块逻辑、向量元数据结构定义

1. 多源异构文档解析标准 (Document Parsing Pipeline)
为避免因文档解析质量低下引入的“脏数据”，解析流水线必须按文件格式执行差异化提取与清洗：

 用户指定文件夹 (Folder)
    │
    ├── [.pdf]  ──► PyMuPDF/pdfplumber (双栏/单栏排版自适应 + 物理页码硬绑定)
    ├── [.md]   ──► AST 语法树解析 (基于 Markdown Header 提取完整层级路径)
    ├── [.docx] ──► python-docx (段落与结构化表格逐行展平)
    └── [.txt]  ──► 编码检测 (UTF-8 / GBK 自适应) + 双换行分段
1.1 PDF 文件解析约束（以官方产品手册/命令参考为主）
物理页码强关联：提取文本时，必须以文档的物理 Page Index（从 1 开始计）作为唯一坐标，严禁多页合并后再统一切分。
噪音清洗规则：
页眉/页脚剔除：通过正则表达式匹配并剔除重复的版权声明（如 Copyright © 2026 Dell Inc.）、保密标签及页码占位符。
断字重组（Hyphenation Fix）：将行尾连字符换行（如 ad- \n ministration）自动重组修复为完整单词（administration）。
1.2 Markdown / 文本文档解析约束
标题层级保留：解析器需提取当前段落所处的所有祖先标题（例如：# PowerStore > ## PSTCLI Reference > ### volume），并拼装至该分块的元数据中。
2. 语义感知切分策略 (Semantic Chunking & Code Preservation)
为防止将完整的 CLI 命令、选项列表（Flags）或高风险警示切断，切分器必须满足以下硬性逻辑：

                    ┌──────────────────────────────┐
                    │       待切分原始文档流        │
                    └──────────────┬───────────────┘
                                   │
              ┌────────────────────┴────────────────────┐
              ▼                                         ▼
   【普通文本段落】                           【CLI 代码块 / 命令段落】
   • 窗口: 512 Tokens                        • 强制原子化 (Atomic Chunking)
   • 重叠: 64 Tokens                         • 触发保护: 允许扩展至 768 Tokens
   • 边界: 句号 / 换行                        • 禁止截断 ```bash 内部内容
2.1 切分超参数配置
标准切片长度 (chunk_size): 512 Tokens。
滑动重叠窗口 (chunk_overlap): 64 Tokens（确保跨切片代词与上下文关联不丢失）。
切分分隔符优先级 (separators): ["\n\n```", "```\n\n", "\n## ", "\n### ", "\n\n", "\n", "。"]。
2.2 CLI 命令与表格保护机制
代码块原子保护：任何由 ``` 包裹的 CLI 命令或配置文件片段，均视为**不可分割原子切片**。若代码块超出 512 Tokens 但小于 768 Tokens，不触发截断，允许作为单一 Chunk 存储；若超过 768 Tokens，按行（\n）切分，并强制在新切片头部重复添加注释说明。
高风险警示关联：若文本包含 【WARNING】、【CAUTION】 或 【高风险警示】，切分器必须将警示语句与后续紧邻的第一条操作命令绑定在同一个 Chunk 内，严禁分离。
3. 元数据模式规范 (Metadata Schema Standard)
每一个写入本地向量数据库（ChromaDB）的切片，必须附带以下强类型元数据（Metadata）：

字段名称 (KEY)
数据类型
说明与示例
用途
chunk_id
String
SHA-256 (文件路径 + 页码 + 块序号)
唯一主键，支持增量去重
doc_name
String
PowerStore_pstcli_Guide.pdf
输出引用标注的文档名
file_path
String
/data/docs/storage/PowerStore_pstcli_Guide.pdf
本地跳转与二次核验路径
page_number
Integer
86 (PDF 取实际页码，纯文本/MD 默认为 1)
物理页码溯源
heading_path
String
PSTCLI > Volume Management > volume modify
章节上下文补充
has_code
Boolean
true / false
标记是否包含 CLI 命令行
file_hash
String
源文件 MD5 / SHA-256 摘要
检测本地文件修改，支持增量更新


