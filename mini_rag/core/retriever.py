"""双路召回 + RRF 融合 + 阈值硬拦截（防幻觉第一道防线）。"""
from __future__ import annotations

from mini_rag.config import settings
from mini_rag.core import embedder, store
from mini_rag.core.schema import ScoredChunk


def retrieve(query: str) -> tuple[list[ScoredChunk], str]:
    """返回 (候选列表, 拒答原因)。拒答原因为 "" 表示通过。

    融合策略由 settings.SPARSE_FALLBACK_ONLY 控制：
    - True（默认）：dense 主力 + sparse 兜底。dense 有通过 DENSE_MIN 的候选就
      只按 dense 分数排序返回，不融合 sparse —— 因为 RRF 只用名次丢弃分数，
      sparse rank1 会压过 dense rank2 把噪声挤进 top4。
      仅当 dense 空手时回退 sparse（FTS5 MATCH 已保证词项命中，救英文命令精确匹配）。
    - False：旧的全 RRF 融合（语料换中文 / 需英文术语精确匹配时切回）。
    """
    qv = embedder.embed_query(query)
    dense_hits = store.dense_search(qv, settings.DENSE_TOP_K)
    dense_valid = [(c, s) for c, s in dense_hits if s >= settings.DENSE_MIN]

    # dense 主力：有达标候选就只信 dense，稀疏路不参与，避免跨语言噪声挤占名次
    if dense_valid:
        ranked = sorted(dense_valid, key=lambda x: -x[1])[: settings.FINAL_TOP_N]
        scored = [ScoredChunk(chunk=c, dense_score=s, sparse_score=0.0,
                              rrf_score=s, matched_by="dense")
                  for c, s in ranked]
        return scored, ""

    if settings.SPARSE_FALLBACK_ONLY:
        # dense 空手 → sparse 兜底（唯一能救纯英文词/命令精确匹配的场景）
        terms = store.query_terms(query)
        sparse_hits = store.sparse_search(terms, settings.SPARSE_TOP_K)
        if sparse_hits:
            scored = [ScoredChunk(chunk=c, dense_score=0.0, sparse_score=s,
                                  rrf_score=0.0, matched_by="sparse")
                      for c, s in sparse_hits[: settings.FINAL_TOP_N]]
            return scored, ""
        return [], "below_threshold"

    # 旧行为：全 RRF 融合（保留作逃生舱，噪声风险见 settings 注释）
    terms = store.query_terms(query)
    sparse_valid = store.sparse_search(terms, settings.SPARSE_TOP_K)

    if not dense_valid and not sparse_valid:
        reason = ("no_candidate" if (not dense_hits and not sparse_valid)
                  else "below_threshold")
        return [], reason

    rrf: dict[str, dict] = {}
    for rank, (chunk, sim) in enumerate(dense_valid, 1):
        e = rrf.setdefault(chunk.chunk_id, {"chunk": chunk, "dense": sim,
                                           "sparse": 0.0, "rrf": 0.0, "by": "dense"})
        e["rrf"] += 1.0 / (settings.RRF_K + rank)
    for rank, (chunk, score) in enumerate(sparse_valid, 1):
        e = rrf.get(chunk.chunk_id)
        if e is None:
            e = rrf[chunk.chunk_id] = {"chunk": chunk, "dense": 0.0,
                                       "sparse": score, "rrf": 0.0, "by": "sparse"}
        else:
            e["sparse"] = score      # 已存在则补齐稀疏分，避免丢失
            e["by"] = "both"
        e["rrf"] += 1.0 / (settings.RRF_K + rank)

    ranked = sorted(rrf.values(), key=lambda x: -x["rrf"])[: settings.FINAL_TOP_N]
    scored = [ScoredChunk(chunk=e["chunk"], dense_score=e["dense"],
                          sparse_score=e["sparse"], rrf_score=e["rrf"],
                          matched_by=e["by"]) for e in ranked]
    return scored, ""
