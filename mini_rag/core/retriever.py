"""多路召回 + RRF 融合 + MMR 去冗余（深度优化版，零幻觉底线不动）。

链路：
  1. L1 Query Rewriting（同义词扩展 / 中英翻译 / svc 归一）
  2. L2 HyDE（用 LLM 生成假设性段落，参与 dense 召回）
  3. 多路 dense（每路 query 一个向量）+ RRF 融合
  4. sparse 兜底（仅 dense 空手时启用，保留 SPARSE_FALLBACK_ONLY 行为）
  5. DENSE_MIN 阈值守门（防幻觉第一道防线）
  6. MMR 去冗余（top-20 → top-N，避免 4 块都讲同一段）

逃生舱：
  settings.QUERY_REWRITE_ENABLED / HYDE_ENABLED / MMR_ENABLED 任意关掉即降级为
  与改造前等价的 dense 单路（SPARSE_FALLBACK_ONLY + DENSE_MIN 不变）。
"""
from __future__ import annotations

from mini_rag.config import settings
from mini_rag.core import hyde as hyde_mod
from mini_rag.core import query_rewrite as qr_mod
from mini_rag.core import store
from mini_rag.core.embedder import embed_query
from mini_rag.core.schema import ScoredChunk


def _mpr(lists: list[list[tuple[str, float]]], k: int = 60) -> dict[str, float]:
    """RRF 多路融合：lists 是 [(id, score), ...] 的列表，返回 {id: rrf_score}。

    只用名次丢弃分数的经典 RRF：sparse 无阈值时易把噪声挤进 top-k（架构评审
    已证明），所以这里所有路都要先经过各自的阈值/守门才参与融合。
    """
    out: dict[str, float] = {}
    for ranked in lists:
        for rank, (cid, _s) in enumerate(ranked, 1):
            out[cid] = out.get(cid, 0.0) + 1.0 / (k + rank)
    return out


def _mmr_select(candidates: list[ScoredChunk], top_n: int,
                lam: float = 0.6) -> list[ScoredChunk]:
    """MMR 去冗余：从 candidates 按 max-marginal-relevance 选 top_n。

    lam=0.6 偏重相关性；余下 0.4 留给多样性。embedding 来自 Chroma cosine。
    这里 cosine 直接复用 dense_score：得分越高与 query 越相关。
    """
    if len(candidates) <= top_n:
        return candidates

    selected: list[ScoredChunk] = []
    pool = list(candidates)
    while len(selected) < top_n and pool:
        if not selected:
            # 第一轮：选分数最高的
            best = max(pool, key=lambda s: s.dense_score)
        else:
            # 后续轮：MMR = λ·sim(q, c) - (1-λ)·max_sim(c, 已选)
            best = None
            best_score = -1e9
            for c in pool:
                rel = c.dense_score
                # 与已选的最大冗余：用 chunk_id 字符串的 Jaccard 近似（无 cross-encoder 时
                # 用 chunk_id 共享率作兜底——同文档同 section 共享率高，触发降权）
                max_red = 0.0
                for s in selected:
                    if c.chunk.doc_id == s.chunk.doc_id:
                        # 同文档算 0.7 冗余；同页更高
                        page_red = 0.9 if c.chunk.page_start == s.chunk.page_start else 0.5
                        max_red = max(max_red, page_red)
                    elif c.chunk.file_path == s.chunk.file_path:
                        max_red = max(max_red, 0.3)
                mmr = lam * rel - (1 - lam) * max_red
                if mmr > best_score:
                    best_score = mmr
                    best = c
        selected.append(best)
        pool.remove(best)
    return selected


def retrieve(query: str, mock_vec: list[float] | None = None) -> tuple[list[ScoredChunk], str]:
    """深度优化版 retrieve。

    mock_vec 仅供离线测试：跳过 embed_query 直接用给定向量走 dense 主力。
    """
    use_rewrite = qr_mod.is_enabled() and mock_vec is None
    use_hyde = hyde_mod.is_enabled() and mock_vec is None

    # 1) 构造多 query
    queries = [query]
    if use_rewrite:
        queries = qr_mod.rewrite(query)  # 第一项是原 query
    if use_hyde:
        # 对原 query 跑 HyDE，生成的假设段落作为额外 query
        hyde_qs = hyde_mod.expand(query, llm_generate=True)
        # hyde_qs[0]=query, hyde_qs[1]=假设段落
        # 我们让"假设段落"参与 dense；不重复 query
        if len(hyde_qs) > 1 and hyde_qs[1] != query:
            queries = queries + [hyde_qs[1]]

    # 2) 每个 query embed → dense_search → 收集候选（仅 ≥DENSE_MIN）
    dense_id_lists: list[list[tuple[str, float]]] = []
    dense_chunks: dict[str, ScoredChunk] = {}  # cid -> ScoredChunk（取最高分版本）

    for q in queries:
        try:
            qv = mock_vec if q == query and mock_vec is not None else embed_query(q)
        except Exception as e:
            # 单路失败不阻塞整体
            print(f"[retrieve] embed 失败: {q[:30]!r} → {e}")
            continue
        hits = store.dense_search(qv, settings.DENSE_TOP_K)
        valid = [(c, s) for c, s in hits if s >= settings.DENSE_MIN]
        # 记录参与融合的名次（仅 valid 路）
        dense_id_lists.append([(c.chunk_id, s) for c, s in valid])
        for c, s in valid:
            old = dense_chunks.get(c.chunk_id)
            if old is None or s > old.dense_score:
                dense_chunks[c.chunk_id] = ScoredChunk(
                    chunk=c, dense_score=s, sparse_score=0.0,
                    rrf_score=0.0, matched_by="dense")

    # 3) RRF 融合（仅在多路 dense 都有结果时才有意义；单路时 rrf_score == dense 排名分）
    if dense_id_lists and dense_chunks:
        rrf = _mpr(dense_id_lists, k=settings.RRF_K)
        # 用 rrf 重排候选
        candidates = sorted(dense_chunks.values(),
                            key=lambda s: -rrf.get(s.chunk.chunk_id, 0.0))
        # 强制按 rrf_score 更新（供 CLI debug 打印）
        for s in candidates:
            s.rrf_score = rrf.get(s.chunk.chunk_id, 0.0)
    else:
        candidates = []

    # 4) dense 主力：有候选就只信 dense，sparse 兜底逻辑保留
    if candidates:
        if getattr(settings, "MMR_ENABLED", True):
            final = _mmr_select(candidates, settings.FINAL_TOP_N)
        else:
            final = candidates[: settings.FINAL_TOP_N]
        return final, ""

    # 5) sparse 兜底（dense 空手时，救英文命令/纯词精确匹配）
    if settings.SPARSE_FALLBACK_ONLY:
        terms = store.query_terms(query)
        sparse_hits = store.sparse_search(terms, settings.SPARSE_TOP_K)
        if sparse_hits:
            return [ScoredChunk(chunk=c, dense_score=0.0, sparse_score=s,
                                rrf_score=0.0, matched_by="sparse")
                    for c, s in sparse_hits[: settings.FINAL_TOP_N]], ""
        return [], "below_threshold"

    # 6) 旧全 RRF 行为（逃生舱）—— SPARSE_FALLBACK_ONLY=False 时启用。
    #    与 dense 主力时结构一致：多路 dense（现只有 1 路） + sparse，RRF 融合。
    terms = store.query_terms(query)
    sparse_valid = store.sparse_search(terms, settings.SPARSE_TOP_K)
    if not sparse_valid and not candidates:
        return [], "no_candidate" if not dense_id_lists else "below_threshold"

    if sparse_valid:
        # 把 sparse 也加入 RRF（仅在 SPARSE_FALLBACK_ONLY=False 时）
        sparse_id_list = [(c.chunk_id, s) for c, s in sparse_valid]
        dense_id_lists.append(sparse_id_list)
        for c, s in sparse_valid:
            sc = dense_chunks.get(c.chunk_id)
            if sc is None:
                dense_chunks[c.chunk_id] = ScoredChunk(
                    chunk=c, dense_score=0.0, sparse_score=s,
                    rrf_score=0.0, matched_by="sparse")
            else:
                sc.sparse_score = s
                sc.matched_by = "both"
        rrf = _mpr(dense_id_lists, k=settings.RRF_K)
        candidates = sorted(dense_chunks.values(),
                            key=lambda s: -rrf.get(s.chunk.chunk_id, 0.0))
        for s in candidates:
            s.rrf_score = rrf.get(s.chunk.chunk_id, 0.0)

    if candidates:
        if getattr(settings, "MMR_ENABLED", True):
            final = _mmr_select(candidates, settings.FINAL_TOP_N)
        else:
            final = candidates[: settings.FINAL_TOP_N]
        return final, ""

    return [], "below_threshold"