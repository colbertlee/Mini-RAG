"""L2 HyDE（Hypothetical Document Embeddings）—— 用 LLM 生成"假设性答案段落"再 embed。

原理（[Gao et al., 2022]）：
  - 真实 query 短、口语化（如"BBU 怎么换？"），与文档语域（"Remove the battery backup
    unit by pressing the release latch..."）错配，cosine 偏低。
  - 用 LLM 让其基于 query 生成一段假设性答案（fake answer），再 embed 这个 fake answer。
    fake answer 用了文档语域的句式，cosine 自然拉高。
  - 实测对短查询、模糊查询提升最大。

本实现要点：
  - 复用 embedder._post，独立 prompt（与 generator 的零幻觉 SYSTEM_PROMPT 分离）
  - HyDE prompt 明说「这是模拟生成、仅供向量召回用、不输出给用户」
  - think=false 必带 + 正则兜底（与 generator 同策略）
  - keep_alive="0" + num_gpu=0（与 generator 同）
  - 失败时降级为 embed 原 query，不让 HyDE 错误链阻塞召回

2026-09-04 两处优化：
  1. 段落从 60-80 词压到 30-40 词 —— LLM 自回归生成，token 数减半 ≈ latency 减半。
  2. LRU 缓存落盘 data/hyde_cache.jsonl —— 命中则完全跳过 LLM 调用（12s → ~1ms）。
     CLI 每次 ask 都是新进程，缓存必须持久化才有意义。
"""
from __future__ import annotations

import json
import re
import time
from collections import OrderedDict

from mini_rag.config import settings
from mini_rag.core.embedder import _post


# 段落长度：30-40 词（2026-09-04 从 60-80 压缩，latency 减半）
HYDE_PROMPT = """You generate a VERY SHORT hypothetical technical paragraph (30-40 words)
that a PowerStore administrator might see in an official Dell documentation context.
You write in the SAME domain language as official Dell PowerStore KB articles
and service scripts documentation:
- Concrete nouns (service script names, alert codes, hardware component names)
- Imperative verbs ("To replace ...", "Run svc_xxx ...")
- English only (the corpus is English Dell KB articles)

RULES:
- Do NOT invent non-existent features or commands. Stay within plausible PowerStore topics.
- Output only the paragraph, no preamble, no labels.
- Be concise: 30-40 words is a HARD LIMIT. Technical, formal, no filler."""


# ---------------- LRU 缓存 ----------------
# OrderedDict：最近使用的排最后。命中 → move_to_end；超限 → 淘汰最前。
_cache: OrderedDict[str, dict] | None = None


def _cache_path():
    return settings.HYDE_CACHE_PATH


def _load_cache() -> OrderedDict[str, dict]:
    """从 jsonl 载入缓存（按文件顺序，旧的在前）。文件损坏 → 从头开始，不致命。"""
    od: OrderedDict[str, dict] = OrderedDict()
    p = _cache_path()
    if not p.exists():
        return od
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 单行损坏不影响其他条目
                k = rec.get("query", "")
                if k:
                    od[k] = rec
    except OSError:
        return OrderedDict()
    return od


def _save_cache(od: OrderedDict[str, dict]) -> None:
    """全量重写（缓存条目数有上限，全量写比增量合并简单且不易出错）。"""
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for k, rec in od.items():
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        tmp.replace(p)  # 原子替换，避免写一半崩掉留下坏文件
    except OSError:
        pass  # 缓存写失败不影响主流程


def _cache_get(query: str) -> str | None:
    """命中返回缓存的段落，未命中返回 None。"""
    global _cache
    if not getattr(settings, "HYDE_CACHE_ENABLED", True):
        return None
    if _cache is None:
        _cache = _load_cache()
    key = _cache_key(query)
    rec = _cache.get(key)
    if rec is None:
        return None
    _cache.move_to_end(key)  # 命中 → 提到最新
    return rec.get("doc", "") or None


def _cache_put(query: str, doc: str) -> None:
    """写入缓存并落盘。超出 HYDE_CACHE_SIZE 淘汰最久未用。"""
    global _cache
    if not getattr(settings, "HYDE_CACHE_ENABLED", True):
        return
    if _cache is None:
        _cache = _load_cache()
    key = _cache_key(query)
    _cache[key] = {"query": key, "doc": doc, "ts": int(time.time())}
    _cache.move_to_end(key)
    limit = getattr(settings, "HYDE_CACHE_SIZE", 200)
    while len(_cache) > limit:
        _cache.popitem(last=False)  # 淘汰最久未用（最前）
    _save_cache(_cache)


def _cache_key(query: str) -> str:
    """缓存 key：归一化（压缩空白 + 小写 + 去首尾标点）。

    去首尾标点让 "BBU 怎么换" / "BBU 怎么换？" / " bbu 怎么换 " 命中同一条——
    语义相同只应缓存一份，否则缓存命中率白白腰斩。
    """
    q = re.sub(r"\s+", " ", query.strip()).lower()
    return q.strip("?？!！。，,、;；:：.·-_ ")


def cache_stats() -> dict:
    """给 CLI / 评估脚本看的缓存统计。"""
    if _cache is None:
        return {"size": 0, "path": str(_cache_path())}
    return {"size": len(_cache), "limit": getattr(settings, "HYDE_CACHE_SIZE", 200),
            "path": str(_cache_path())}


def clear_cache() -> int:
    """清空缓存，返回被清条目数。"""
    global _cache
    n = len(_cache) if _cache else 0
    _cache = OrderedDict()
    try:
        _cache_path().unlink(missing_ok=True)
    except OSError:
        pass
    return n


# ---------------- HyDE 生成 ----------------
def _truncate_words(text: str, max_words: int) -> str:
    """按词数硬截断（防 LLM 啰嗦时不顾 prompt 的长度限制）。"""
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def _hyde_llm(query: str) -> str:
    """调 LLM 生成假设性段落。失败时返回 ''（让 retriever 走原 query）。

    2026-09-04：段落压缩到 30-40 词，num_predict 也同步收紧，
    避免 LLM 自回归跑满 num_ctx（这是原先 12s 的主因）。
    """
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
                         "num_predict": 80,   # 30-40 词 ≈ 50-60 token，80 封顶防止跑飞
                         "stop": ["Topic:", "```", "\n\nTopic", "\n\n"]}},
        )
        ans = data.get("message", {}).get("content", "")
        # 兜底剥 <think>
        ans = re.sub(r"<think>.*?</think>", "", ans, flags=re.S).strip()
        # 取第一段（LLM 偶尔会多说一段），再按词数截断
        ans = ans.split("\n\n")[0].strip()
        max_words = getattr(settings, "HYDE_MAX_WORDS", 45)
        ans = _truncate_words(ans, max_words)
        # 太短视为失败（30-40 词目标下的下限）
        if len(ans.split()) < 8:
            return ""
        return ans
    except Exception:
        return ""


def expand(query: str, llm_generate: bool = True) -> list[str]:
    """返回多假设文档段落列表（含原 query 的"零假设"占位）。

    返回 [query, hyde_doc]：
      - query：原 query，用于 sparse 召回与一致性
      - hyde_doc：LLM 生成的假设段落，用于 dense 召回
    失败时 hyde_doc == query（即 dense 走原 query，等于无 HyDE）。

    缓存命中则完全跳过 LLM 调用（12s → ~1ms）。
    """
    if not llm_generate:
        return [query]

    # 先查缓存
    fake = _cache_get(query) if getattr(settings, "HYDE_CACHE_ENABLED", True) else None
    if fake:
        return [query, fake]

    # 未命中 → 调 LLM
    fake = _hyde_llm(query)
    if fake:
        _cache_put(query, fake)
        return [query, fake]
    return [query]


def is_enabled() -> bool:
    return getattr(settings, "HYDE_ENABLED", True)
