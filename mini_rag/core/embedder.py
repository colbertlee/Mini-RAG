"""Embedding 层：默认本地 Ollama，OpenAI / BGE 通过配置切换。

两个本机硬约束（实测踩过，别删）：
  - num_gpu=0：Ollama 会自动走 Vulkan 用 RX 470，qwen3.5:4b 会报
    `failed to allocate Vulkan0 buffer` 直接崩，必须强制纯 CPU。
  - keep_alive=0：embedding 模型(2.5GB) 与生成模型(3.4GB) 同时驻留会撑爆内存，
    查询向量化后立即卸载，任一时刻只留一个模型。
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

from mini_rag.config import settings


def _post(url: str, payload: dict, headers: dict | None = None) -> dict:
    """POST 到 Ollama，绕过系统代理直连 127.0.0.1，失败指数退避重试。

    失败时透出原始错误体（如模型加载失败的原因），而不是只给一个无信息的状态码。
    """
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last: str = ""
    for attempt in range(1, settings.EMBED_RETRIES + 1):
        try:
            with opener.open(req, timeout=settings.REQUEST_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
            if attempt < settings.EMBED_RETRIES:
                time.sleep(2 ** attempt)
        except Exception as e:
            last = str(e)
            if attempt < settings.EMBED_RETRIES:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Embedding 请求失败（{url}）: {last}")


# ================= Ollama（默认） =================
def _ollama_embed_texts(texts: list[str],
                        batch_size: int | None = None) -> list[list[float]]:
    """批量向量化，输出顺序与输入严格一致。单批失败退化为逐条重试。"""
    batch_size = batch_size or settings.EMBED_BATCH
    out: list[list[float] | None] = [None] * len(texts)
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        try:
            data = _post(f"{settings.OLLAMA_URL}/api/embed",
                         {"model": settings.EMBED_MODEL, "input": batch,
                          "options": {"num_gpu": 0}})
            for j, vec in enumerate(data["embeddings"]):
                out[i + j] = vec
        except Exception:
            for j, t in enumerate(batch):
                d = _post(f"{settings.OLLAMA_URL}/api/embed",
                          {"model": settings.EMBED_MODEL, "input": [t],
                           "options": {"num_gpu": 0}})
                out[i + j] = d["embeddings"][0]
    return out  # type: ignore[return-value]


def _ollama_embed_query(q: str) -> list[float]:
    # keep_alive 默认 "0"：查询向量化后立即卸载，给生成模型腾内存。
    # 批量评估/建索引时用 MINIRAG_EMBED_KEEP_ALIVE=10m 让模型常驻 ——
    # 否则每条 query 都重新加载 2.5GB，反复内存冲击会把 Ollama 搞崩。
    data = _post(f"{settings.OLLAMA_URL}/api/embed",
                 {"model": settings.EMBED_MODEL, "input": [q],
                  "options": {"num_gpu": 0},
                  "keep_alive": getattr(settings, "EMBED_KEEP_ALIVE", "0")})
    return data["embeddings"][0]


def unload_embed_model() -> bool:
    """卸载 embedding 模型（批量任务结束后释放内存）。失败不影响主流程。"""
    try:
        _post(f"{settings.OLLAMA_URL}/api/embed",
              {"model": settings.EMBED_MODEL, "input": [""],
               "options": {"num_gpu": 0}, "keep_alive": "0"})
        return True
    except Exception:
        return False


class OllamaEmbedder:
    name = "ollama"

    def embed_texts(self, texts: list[str],
                    batch_size: int | None = None) -> list[list[float]]:
        return _ollama_embed_texts(texts, batch_size)

    def embed_query(self, q: str) -> list[float]:
        return _ollama_embed_query(q)


# ================= OpenAI（预留） =================
class OpenAIEmbedder:
    """预留实现：pip install openai 并配置 OPENAI_API_KEY 后可用。

    注意：文档内容会出网，违反本地隐私合规约束（DOC-05），仅在明确接受时使用。
    """

    name = "openai"

    def __init__(self, model: str | None = None):
        self.model = model or settings.OPENAI_EMBED_MODEL
        self.api_key = (settings.OPENAI_API_KEY
                        or os.environ.get("OPENAI_API_KEY", ""))
        if not self.api_key:
            raise RuntimeError(
                "OPENAI_API_KEY 未配置：设置 settings.OPENAI_API_KEY 或环境变量")

    def _call(self, texts: list[str]) -> list[list[float]]:
        from openai import OpenAI
        client = OpenAI(api_key=self.api_key)
        resp = client.embeddings.create(model=self.model, input=texts)
        return [d.embedding for d in resp.data]

    def embed_texts(self, texts: list[str],
                    batch_size: int | None = None) -> list[list[float]]:
        batch_size = batch_size or settings.EMBED_BATCH
        out: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            out.extend(self._call(texts[i:i + batch_size]))
        return out

    def embed_query(self, q: str) -> list[float]:
        return self._call([q])[0]


# ================= BGE（预留） =================
class BGEEmbedder:
    """预留实现：pip install sentence-transformers 后可用（本地 bge 系列）。"""

    name = "bge"

    def __init__(self, model: str | None = None):
        self.model_name = model or settings.BGE_MODEL
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed_texts(self, texts: list[str],
                    batch_size: int | None = None) -> list[list[float]]:
        vecs = self._load().encode(
            texts, batch_size=batch_size or settings.EMBED_BATCH,
            normalize_embeddings=True)
        return [list(map(float, v)) for v in vecs]

    def embed_query(self, q: str) -> list[float]:
        return self.embed_texts([q])[0]


_PROVIDERS = {
    "ollama": OllamaEmbedder,
    "openai": OpenAIEmbedder,
    "bge": BGEEmbedder,
}

_provider = None


def get_embedder():
    global _provider
    if _provider is None:
        cls = _PROVIDERS.get(settings.EMBED_PROVIDER)
        if cls is None:
            raise RuntimeError(
                f"未知 EMBED_PROVIDER: {settings.EMBED_PROVIDER}，"
                f"可选 {list(_PROVIDERS)}")
        _provider = cls()
    return _provider


def embed_texts(texts: list[str], batch_size: int | None = None) -> list[list[float]]:
    """批量向量化，输出顺序与输入严格一致。"""
    return get_embedder().embed_texts(texts, batch_size)


def embed_query(q: str) -> list[float]:
    return get_embedder().embed_query(q)
