为你设计一个轻量、极简且完全本地化运行的 **Mini-RAG 知识检索系统**。该方案确保所有数据均在本地处理，兼顾隐私安全与零幻觉（Zero-Hallucination）约束。

---

### 一、 整体架构与技术选型

* **本地推理引擎**：[Ollama](https://ollama.com/)
  * **LLM（生成模型）**：`qwen2.5:7b`（中文理解及指令遵循能力极强）或 `llama3.1:8b`
  * **Embedding（嵌入模型）**：`nomic-embed-text` 或 `bge-m3`（通过 Ollama 统一管理）
* **向量数据库**：`ChromaDB`（轻量嵌入式，无需独立部署服务，直接存储在本地磁盘）
* **开发框架**：`LangChain` 或 `LlamaIndex`（本方案采用 LangChain 极简实现）
* **前端/交互界面**：`Streamlit`（Web 交互界面，50 行代码即可搭建）或纯 CLI 命令行交互

---

### 二、 用户视角全流程体验（User Journey）

```text
[用户指定本地文件夹] ──▶ [一键建立索引/向量化] ──▶ [用户提问] ──▶ [相似度检索与阈值判断] ──▶ [模型依据上下文回答 / 拒答]
```

#### 1. 准备与初始化
* **用户动作**：启动工具，输入/选择本地文档目录路径（如 `D:/my_docs/` 或 `./knowledge_base/`，支持 `.txt`, `.md`, `.pdf`, `.docx` 等）。
* **系统响应**：扫描文件夹并显示待处理的文件列表与总数。

#### 2. 一键构建索引（Indexing）
* **用户动作**：点击“构建/更新知识库”按钮。
* **系统内部处理**：
  1. 自动提取文本内容。
  2. 按照固定分块规则（如 500 字符/块，重叠 50 字符）切分文档。
  3. 调用 Ollama 本地 Embedding 模型生成向量。
  4. 存入本地 Chroma 向量库。
* **系统响应**：提示“知识库加载完成，共处理 X 个文档切片，可开始问答”。

#### 3. 智能问答与检索（Chat & Retrieval）
* **用户动作**：在聊天框输入业务或技术问题。
* **系统处理逻辑**：
  1. 将问题向量化，在 Chroma 中执行相似度检索（Top-K，例如检索最匹配的 3 个切片）。
  2. **防幻觉门限检查（Score Threshold）**：若检索到的最高相似度分数低于设定阈值，直接跳过 LLM 生成环节。
  3. **受限上下文组装**：若找到相关切片，将切片文本注入严格约束的 Prompt 模板。
  4. Ollama LLM 输出回答。

#### 4. 结果反馈（Output）
* **情况 A（命中内容）**：输出基于文档提炼的答案，并在末尾标注参考文档来源（如 `来源：[技术规范.pdf, 第 12 页]`）。
* **情况 B（未命中或信息不足）**：严格输出预设话术：**“知识库中未找到相关信息。”**（严禁模型发散）。

---

### 三、 核心实现逻辑与防幻觉设计

#### 1. 防幻觉 Prompt 核心设计
通过强指令约束模型只能依据提供的上下文作答：

```text
你是一个严格依据参考资料回答问题的知识助手。
请根据以下提供的【上下文内容】来回答【用户问题】。

【约束规则】：
1. 只能依据【上下文内容】中明确提到的事实进行回答。
2. 如果【上下文内容】中没有包含回答该问题所需的全部信息，或者内容与问题无关，请直接且仅回复：“知识库中未找到相关信息。”
3. 严禁使用你自己的先验知识进行推测、补充或编造。

【上下文内容】：
{context}

【用户问题】：
{question}
```

#### 2. 双重防幻觉机制（检索层 + 生成层）
1. **第一道防线（检索距离过滤）**：
   在向量检索时设置 `similarity_score_threshold`（例如余弦相似度必须 $> 0.6$）。若无任何切片达标，直接程序端返回拒答，不消耗本地算力调用 LLM。
2. **第二道防线（System Prompt 强制约束）**：
   通过上述系统级 Prompt 锁定模型的生成范围，禁止发散。

---

### 四、 极简原型代码示例 (Python + Streamlit)

运行前确保已安装依赖并启动 Ollama：
```bash
ollama pull qwen2.5:7b
ollama pull nomic-embed-text
pip install langchain langchain-community chromadb streamlit pypdf docx2txt
```

**`app.py` 核心实现：**

```python
import os
import streamlit as st
from langchain_community.document_loaders import DirectoryLoader, PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.embeddings import OllamaEmbeddings
from langchain_community.vectorstores import Chroma
from langchain_community.llms import Ollama
from langchain.chains import RetrievalQA
from langchain.prompts import PromptTemplate

# 1. 页面配置
st.set_page_config(page_title="Mini-RAG 本地知识检索", layout="wide")
st.title("📁 本地知识库检索系统 (Ollama-based)")

# 侧边栏配置
with st.sidebar:
    st.header("知识库设置")
    folder_path = st.text_input("本地文件夹路径:", value="./docs")
    btn_build = st.button("构建 / 重建知识库")

# 初始化模型
embeddings = OllamaEmbeddings(model="nomic-embed-text")
llm = Ollama(model="qwen2.5:7b", temperature=0.0)  # temperature 设为 0 确保最稳定的事实输出

DB_DIR = "./chroma_db"

# 2. 向量库构建流程
if btn_build:
    if os.path.exists(folder_path):
        with st.spinner("正在加载文档并构建向量索引..."):
            loader = DirectoryLoader(folder_path, glob="**/*.*", show_progress=True)
            docs = loader.load()
            
            text_splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
            splits = text_splitter.split_documents(docs)
            
            vectorstore = Chroma.from_documents(
                documents=splits,
                embedding=embeddings,
                persist_directory=DB_DIR
            )
            st.success(f"构建完成！共索引 {len(docs)} 个文件，{len(splits)} 个文档切片。")
    else:
        st.error("指定的文件夹路径不存在，请检查。")

# 3. 严格防幻觉 Prompt
PROMPT_TEMPLATE = """你是一个严格依据参考资料回答问题的知识助手。
请根据以下提供的【上下文内容】来回答【用户问题】。

【约束规则】：
1. 只能依据【上下文内容】中明确提到的事实进行回答。
2. 如果【上下文内容】中没有包含回答该问题所需的全部信息，或者内容与问题无关，请直接且仅回复：“知识库中未找到相关信息。”
3. 严禁使用你自己的先验知识进行推测、补充或编造。

【上下文内容】：
{context}

【用户问题】：
{question}
"""

QA_PROMPT = PromptTemplate(
    template=PROMPT_TEMPLATE, input_variables=["context", "question"]
)

# 4. 检索与问答逻辑
if os.path.exists(DB_DIR):
    vectorstore = Chroma(persist_directory=DB_DIR, embedding_function=embeddings)
    retriever = vectorstore.as_retriever(
        search_type="similarity_score_threshold",
        search_kwargs={"k": 3, "score_threshold": 0.5}  # 设定相似度阈值
    )
    
    qa_chain = RetrievalQA.from_chain_type(
        llm=llm,
        chain_type="stuff",
        retriever=retriever,
        return_source_documents=True,
        chain_type_kwargs={"prompt": QA_PROMPT}
    )

    query = st.text_input("请输入您想查询的知识点或问题：")
    if query:
        with st.spinner("正在检索本地知识库..."):
            response = qa_chain.invoke({"query": query})
            
            # 结果展示
            if not response["source_documents"]:
                st.warning("知识库中未找到相关信息。")
            else:
                st.markdown("### 💡 检索结果：")
                st.write(response["result"])
                
                with st.expander("🔍 查看检索到的上下文来源"):
                    for i, doc in enumerate(response["source_documents"]):
                        source = doc.metadata.get("source", "未知文件")
                        st.markdown(f"**切片 {i+1}** (来自: `{source}`):")
                        st.caption(doc.page_content)
else:
    st.info("请先在左侧输入本地文件夹路径并点击【构建知识库】。")
```

---

### 五、 后续可扩展特性
1. **文件类型扩展**：集成 `Unstructured` 库以支持 Excel (`.xlsx`)、PPT (`.pptx`) 甚至代码仓库解析。
2. **混合检索（Hybrid Search）**：引入 BM25（关键词检索）+ 向量检索（语义检索），提高专有名词、产品型号、配置命令的命中精确度。
3. **多轮对话记忆**：若需要多轮连续交互，可加入 `ConversationBufferMemory`，但需在 Context 中持续贯彻防幻觉策略。