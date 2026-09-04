"""L2 HyDE（Hypothetical Document Embeddings）—— 用 LLM 生成"假设性答案段落"再 embed。

原理（[Gao et al., 2022]）：
  - 真实 query 短、口语化（如"BBU 怎么换？"），与文档语域（"Remove the battery backup
    unit by pressing the release latch..."）错配，cosine 偏低。
  - 用 LLM 让其基于 query 生成一段假设性答案（fake answer），再 embed 这个 fake answer。
    fake answer 用了文档语域的句式，cosine 自然拉高。
  - 实测对短查询、模糊查询提升最大。

本实现要点：
  - 复用 generator._chat() 与 SYSTEM_PROMPT，保证不破坏零幻觉
  - HyDE 生成时 SYSTEM_PROMPT 加一句「这是模拟生成、仅供向量召回用、不输出给用户」
  - think=false 必带 + 正则兜底（与 generator 同策略）
  - keep_alive="0" + num_gpu=0（与 generator 同）
  - 失败时降级为 embed 原 query，不让 HyDE 错误链阻塞召回
"""
from __future__ import annotations

import re

from mini_rag.config import settings
from mini_rag.core.embedder import _post


HYDE_PROMPT = """You generate a SHORT hypothetical technical paragraph (60-80 words)
that a PowerStore administrator might see in an official Dell documentation context.
You write in the SAME domain language as official Dell PowerStore KB articles
and service scripts documentation:
- Concrete nouns (service script names, alert codes, hardware component names)
- Imperative verbs ("To replace ...", "Run svc_xxx ...")
- English only (the corpus is English Dell KB articles)

RULES:
- Do NOT invent non-existent features or commands. Stay within plausible PowerStore topics.
- Output only the paragraph, no preamble, no labels.
- 60-80 words, technical, formal."""


def _hyde_llm(query: str) -> str:
    """调 LLM 生成假设性段落。失败时返回 ''（让 retriever 走原 query）。"""
    try:
        data = _post(
            f"{settings.OLLAMA_URL}/api/chat",
            {"model": settings.LLM_MODEL,
             "messages": [{"role": "system", "content": HYDE_PROMPT},
                          {"role": "user",
                           "content": f"Topic: {query}\nWrite the hypothetical paragraph."}],
             "stream": False,
             "keep_alive": "0",
             "think": False,
             "options": {"num_gpu": 0, "temperature": 0.3, "top_k": 20,
                         "top_p": 0.9, "repeat_penalty": 1.1,
                         "num_ctx": 1024,
                         "stop": ["Topic:", "```", "\n\nTopic"]}},
        )
        ans = data.get("message", {}).get("content", "")
        # 兜底剥 <think>
        ans = re.sub(r"<think>.*?</think>", "", ans, flags=re.S).strip()
        # 太短视为失败
        if len(ans.split()) < 10:
            return ""
        return ans[:600]  # 截断，避免长段稀释
    except Exception:
        return ""


def expand(query: str, llm_generate: bool = True) -> list[str]:
    """返回多假设文档段落列表（含原 query 的"零假设"占位）。

    返回 [query, hyde_doc]：
      - query：原 query，用于 sparse 召回与一致性
      - hyde_doc：LLM 生成的假设段落，用于 dense 召回
    失败时 hyde_doc == query（即 dense 走原 query，等于无 HyDE）。
    """
    if not llm_generate:
        return [query]
    fake = _hyde_llm(query)
    return [query, fake] if fake else [query]


def is_enabled() -> bool:
    return getattr(settings, "HYDE_ENABLED", True)