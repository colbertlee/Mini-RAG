"""token 计数：tiktoken 优先，不可用时降级为字符启发式。

切分长度以 token 为准（不按字符数），但 embedding 走的是本地 Ollama
qwen3-embedding:4b（自有 tokenizer），所以 cl100k_base 只用于「控制切分长度」，
不用于模型对齐——两者口径不同是预期内的，误差约 10%，不影响切分质量。
"""
from __future__ import annotations

from mini_rag.config import settings

_enc = None
_degraded = False


def _encoding():
    """懒加载 tiktoken 编码。首次拉取需联网下载 BPE；失败则永久降级。"""
    global _enc, _degraded
    if _enc is None and not _degraded:
        try:
            import tiktoken
            _enc = tiktoken.get_encoding(settings.TOKENIZER_ENCODING)
        except Exception:
            _degraded = True
    return _enc


def backend() -> str:
    enc = _encoding()
    return f"tiktoken:{settings.TOKENIZER_ENCODING}" if enc else "heuristic"


def count_tokens(text: str) -> int:
    enc = _encoding()
    if enc is not None:
        return len(enc.encode(text, disallowed_special=()))
    # 降级：CJK 按 1 token，其余按 0.25 token
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return int(cjk + (len(text) - cjk) * 0.25)


def encode(text: str) -> list[int]:
    enc = _encoding()
    if enc is None:
        return []
    return enc.encode(text, disallowed_special=())


def truncate(text: str, max_tokens: int) -> str:
    """按 token 硬截断——字符兜底切分时使用，保证不超上限。"""
    if count_tokens(text) <= max_tokens:
        return text
    enc = _encoding()
    if enc is None:
        # 降级：按比例砍字符，留 90% 余量避免再超
        return text[: int(max_tokens * 3.6)]
    return enc.decode(enc.encode(text, disallowed_special=())[:max_tokens])
