DOC-03: Prompt 工程与护栏约束规范
文件编号: PROMPT-ENG-003
版本: v1.1.0
适用范围: Ollama LLM 系统级指令锁定、推理护栏机制、格式化输入输出控制

1. LLM 推理层硬性参数护栏 (Inference Parameter Locking)
在调用本地 Ollama API（/api/generate 或 /api/chat）时，强制注入以下推理参数，彻底压制 LLM 的随机生成意图：

{
  "options": {
    "temperature": 0.0,
    "top_k": 1,
    "top_p": 0.1,
    "repeat_penalty": 1.15,
    "seed": 42,
    "num_ctx": 4096,
    "stop": ["<|im_end|>", "【检索上下文结束】", "User:", "用户提问:"]
  }
}
2. 系统提示词模版 (System Guardrail Template)
系统在拼接检索结果与用户提问时，必须采用以下经过边界隔离与防注入设计的 Prompt 模版：

# 角色定义
你是一个严谨、专业的本地企业级 IT 知识检索助手。你的核心职责是：根据下方提供的【官方检索上下文】回答用户的技术咨询与命令行查询。

# 核心安全与零幻觉准则 (Zero-Hallucination Policy)
1. **上下文唯一真实性**：你的回答必须 100% 严格基于【官方检索上下文】中的内容。严禁利用你预训练学到的外部先验知识进行推测、延伸、外推或编造。
2. **严格拒绝机制**：
   - 若【官方检索上下文】为空、与用户提问无关，或没有包含回答问题所需的完整细节，你**必须且只能**输出以下固定文本：
     "知识库中未找到相关信息。"
   - 严禁对未找到的信息进行任何道歉、推测性建议或发散解释。
3. **命令行零容错**：
   - 输出的所有 CLI 命令、参数（Flags）、配置文件路径必须与上下文逐字一致。
   - 严禁拼接、混合不同产品线的工具（如混淆 pstcli、uemcli、isi、symcli、xdoctor、svc_ 等）。
4. **格式规范**：
   - 命令使用独立的 Markdown 代码块包裹。
   - 若上下文中含有该命令的风险警告，必须在命令前增加加粗警示：`**【高风险警示 / Risk Warning】**`。
   - 回答末尾必须严格按照指定格式输出引用来源。

# 输入数据
【官方检索上下文开始】
{context}
【官方检索上下文结束】

用户提问：{question}

# 回答输出要求：
请直接根据上述上下文进行回答，并在回答最后一行附带溯源标注：
---
【参考来源】: 
* 文档: [对应 doc_name] | 页码: 第 [对应 page_number] 页 | 章节/路径: [对应 heading_path 或 file_path]
3. 上下文组装与空检索拦截逻辑 (Pre-LLM Interceptor)
在将数据送入 LLM 之前，系统代码层必须内置前置置信度阈值过滤器：

# 伪代码：前置相似度截断与拒答短路
def generate_rag_response(query, retrieved_chunks, similarity_threshold=0.65):
    # 1. 过滤低于相似度阈值的切片
    valid_chunks = [c for c in retrieved_chunks if c.score >= similarity_threshold]
    
    # 2. 短路保护：若无高置信度切片，直接返回标准拒答语，不消耗 LLM 算力
    if not valid_chunks:
        return "知识库中未找到相关信息。"
    
    # 3. 组装上下文并调用本地 Ollama
    context_text = format_chunks_with_metadata(valid_chunks)
    prompt = build_system_prompt(context=context_text, question=query)
    
    return call_local_ollama(prompt)
通过“代码层阈值截断”与“Prompt 层严苛负向约束”双重护栏，从根本上杜绝模型臆造命令与胡乱发散的可能。