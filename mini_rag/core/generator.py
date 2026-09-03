"""Prompt 组装 + 受限生成 + 溯源 + L3 生成后校验（防幻觉第二、三道防线）。"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from mini_rag.config import settings
from mini_rag.core.embedder import _post
from mini_rag.core.schema import Citation, QAResponse, ScoredChunk
from mini_rag.core.tokenizer import count_tokens as estimate_tokens

FALLBACK = "知识库中未找到相关信息"

SYSTEM_PROMPT = """你是一个严谨的本地技术知识检索助手。你的唯一职责是依据下方【官方检索上下文】回答用户的命令行与技术咨询。

【零幻觉铁律】
1. 你的回答必须 100% 严格来源于【官方检索上下文】。严禁使用你预训练获得的任何外部知识进行推测、补充、延伸或编造。
2. 若上下文为空、与问题无关，或未包含回答问题所需的完整信息，你必须且只能输出：
   知识库中未找到相关信息
   严禁追加任何道歉、解释或建议。
3. 命令行零容错：输出的每条命令、参数（flags）、路径必须与上下文逐字一致。严禁拼接或混用不同产品的工具。
4. 若上下文包含 WARNING / CAUTION / 高风险警示，必须在对应命令前单独一行加粗输出：**【高风险警示】**。

【输出格式】
- 先给结论，再给命令（命令用独立 markdown 代码块包裹）。
- 不要自行输出任何来源或溯源行，引用来源由系统统一生成。"""


def to_file_uri(path: str, page: int | None) -> str:
    """生成 file:/// URI（机器用）：路径 percent-encode（quote safe="/:"），
    保证空格 / `#` / 中文在链接里合法。显示给人看的中文文件名由 CLI 层用
    osc8_link 的 text 提供，二者分离，互不干扰。"""
    p = quote(Path(path).resolve().as_posix(), safe="/:")
    uri = "file:///" + p
    return f"{uri}#page={page}" if page else uri


def osc8_link(uri: str, text: str) -> str:
    """OSC 8 终端超链接：Ctrl+Click 打开 uri，但显示中文 text。
    在不支持 OSC 8 的终端里会看到转义序列，因此调用方需自行判断是否 tty。"""
    if not uri:
        return text
    return f"\x1b]8;;{uri}\x1b\\{text}\x1b]8;;\x1b\\"


def build_context(scored: list[ScoredChunk]) -> str:
    parts: list[str] = []
    used = 0
    for i, s in enumerate(scored, 1):
        c = s.chunk
        src = c.file_name
        if c.page_number:
            src += f" | 第{c.page_number}页"
        if c.heading_path:
            src += f" | 章节: {c.heading_path}"
        block = f"[片段{i} | 来源: {src}]\n{c.content}"
        t = estimate_tokens(block)
        if used + t > settings.MAX_CONTEXT_TOKENS:
            break
        parts.append(block)
        used += t
    body = "\n\n".join(parts)
    return (f"【官方检索上下文开始】\n{body}\n【官方检索上下文结束】"
            if parts else "【官方检索上下文开始】\n（空）\n【官方检索上下文结束】")


def _chat(user: str) -> str:
    payload = {
        "model": settings.LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "keep_alive": "0",  # 生成后立即卸载，释放内存给下一次 embedding
        "think": False,   # thinking 模型必须关闭，否则输出空串且慢 15~25 倍
        "options": {
            "num_gpu": 0,  # 强制纯 CPU：本机 RX 470 走 Vulkan 会 buffer 分配失败
            "temperature": 0.0, "top_k": 1, "top_p": 0.1,
            "repeat_penalty": 1.15, "seed": 42, "num_ctx": settings.NUM_CTX,
            "stop": ["<|im_end|>", "【官方检索上下文结束】", "用户提问："],
        },
    }
    data = _post(f"{settings.OLLAMA_URL}/api/chat", payload)
    ans = data.get("message", {}).get("content", "")
    # 第二道兜底：万一 think:false 未生效，剥掉 <think> 残留
    return re.sub(r"<think>.*?</think>", "", ans, flags=re.S).strip()


def validate_answer(answer: str, scored: list[ScoredChunk]) -> tuple[bool, str]:
    """L3 生成后校验（纯规则，零额外模型调用）。返回 (通过?, 失败原因)。

    四条规则都只抓「强信号」，避免误伤忠实转述：
    1. 引用编号越界 —— LLM 被禁止自行输出来源，[N] 且 N 超出片段数 = 编造引用。
    2. 推断话术 —— 诉诸外部知识 / 不确定推断的措辞（见 settings.INFERENCE_PHRASES）。
    3. 命令/专有标识符逐字比对 —— 含 `_ - . /` 的 token 必须出现在某块上下文中。
    4. 版本号比对 —— 三段式版本号必须在上下文中出现（两段式与章节号难区分，不做）。
    """
    ctx = [s.chunk.content.lower() for s in scored]

    # 1. 引用编号越界
    n = len(scored)
    for m in re.finditer(r"\[(?:片段)?\s*(\d+)\]", answer):
        if int(m.group(1)) > n:
            return False, f"引用编号越界: [{m.group(1)}]"

    # 2. 推断话术
    for phrase in settings.INFERENCE_PHRASES:
        if phrase in answer:
            return False, f"推断话术: {phrase}"

    # 3. 命令/专有标识符逐字比对（Dell 命令几乎都是 svc_xxx 这类下划线形式）。
    #    字母开头：纯数字开头的 token（版本号/IP/页码）留给规则 4 或跳过。
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9_\-\./]{2,}", answer):
        t = m.group(0)
        if any(ch in t for ch in "_/-.") and not any(t.lower() in c for c in ctx):
            return False, f"命令/标识符不在上下文: {t}"

    # 4. 版本号比对（仅三段式）
    for v in re.findall(r"\b\d+\.\d+\.\d+\b", answer):
        if not any(v in c for c in ctx):
            return False, f"版本号不在上下文: {v}"

    return True, ""


def _excerpt_fallback(query: str, scored: list[ScoredChunk], reason: str) -> str:
    """L3 校验失败时降级为原文摘录：纯原文零幻觉，可用性远高于拒答。"""
    head = ("（系统提示：生成内容可能超出检索上下文，已降级为原文摘录以保证零幻觉。）\n"
            f"（原因：{reason}）\n")
    blocks = []
    for i, s in enumerate(scored, 1):
        src = s.chunk.file_name
        if s.chunk.page_number:
            src += f" 第{s.chunk.page_number}页"
        blocks.append(f"【片段{i} | {src}】\n{s.chunk.content}")
    return head + "\n\n".join(blocks)


def generate(query: str, scored: list[ScoredChunk], latency: dict) -> QAResponse:
    user = f"{build_context(scored)}\n\n用户提问：{query}"
    answer = _chat(user)

    if (not answer.strip()) or (FALLBACK in answer):
        return QAResponse(query=query, answer=FALLBACK, is_fallback=True,
                          rejected_by="below_threshold", citations=[],
                          latency_ms=latency)

    # L3 生成后校验：失败降级为原文摘录（第三态），不拒答、更不编造
    ok, why = validate_answer(answer, scored)
    if not ok:
        return QAResponse(query=query, answer=_excerpt_fallback(query, scored, why),
                          degraded_reason=why,
                          citations=_citations(scored),
                          retrieved=scored, latency_ms=latency)

    return QAResponse(query=query, answer=answer, citations=_citations(scored),
                      retrieved=scored, latency_ms=latency)


def _citations(scored: list[ScoredChunk]) -> list[Citation]:
    return [
        Citation(file_name=s.chunk.file_name, page_number=s.chunk.page_number,
                 chunk_id=s.chunk.chunk_id,
                 file_uri=to_file_uri(s.chunk.file_path, s.chunk.page_number),
                 snippet=s.chunk.content[:120])
        for s in scored
    ]
