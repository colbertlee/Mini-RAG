"""多路召回 + RRF 融合 + MMR 去冗余（深度优化版，零幻觉底线不动）。

链路：
  1. L1 Query Rewriting（同义词扩展 / 中英翻译 / svc 归一）
  2. L2 HyDE（用 LLM 生成假设性段落，参与 dense 召回）
  3. 多路 dense（每路 query 一个向量）+ RRF 融合
  4. sparse 过闸门（SPARSE_MIN + 英文词命中）后参与全量 RRF 融合
  5. DENSE_MIN 阈值守门（防幻觉第一道防线）
  6. MMR 去冗余（top-20 → top-N，避免 4 块都讲同一段）

逃生舱：
  settings.QUERY_REWRITE_ENABLED / HYDE_ENABLED / MMR_ENABLED 任意关掉即降级为
  与改造前等价的 dense 单路（SPARSE_FALLBACK_ONLY=True + DENSE_MIN 不变）。
"""
from __future__ import annotations

import re

import jieba

from mini_rag.config import settings
from mini_rag.core import hyde as hyde_mod
from mini_rag.core import query_rewrite as qr_mod
from mini_rag.core import store
from mini_rag.core.embedder import embed_query
from mini_rag.core.schema import ScoredChunk

_ENG_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-\.]*")

# ---- 查询词项分级（泛词/判别词）与语料证据（2026-09-04 晚）----
# DF 查询走 SQLite instr 全表扫，毫秒级但同一查询会反复问 → 模块级缓存。
_DF_CACHE: dict[str, int] = {}


def _token_df(token: str) -> int:
    """token 的语料文档频率（含该子串的 chunk 数）。带缓存。"""
    if token not in _DF_CACHE:
        _DF_CACHE[token] = store.count_chunks_containing(token)
    return _DF_CACHE[token]


def _query_tokens(query: str) -> list[str]:
    """查询的英文 token（len≥2，去重保序，小写）。"""
    return list(dict.fromkeys(
        t.lower() for t in _ENG_TOKEN_RE.findall(query) if len(t) >= 2))


def _discriminative_tokens(query: str) -> list[str]:
    """查询的判别词：DF ≤ 泛词线的英文 token。泛词（powerstore/io/ha…）不算证据。"""
    limit = max(1, int(store.sparse_count() * settings.SPARSE_UBIQUITOUS_RATIO))
    return [t for t in _query_tokens(query) if _token_df(t) <= limit]


def _zh_content_terms(query: str) -> list[str]:
    """查询的中文内容词（≥2 字、非停用词、去重保序）。"""
    terms = [w.strip() for w in jieba.cut(query)
             if len(w.strip()) >= 2 and w.strip() not in settings.STOPWORDS
             and any("\u4e00" <= ch <= "\u9fff" for ch in w)]
    return list(dict.fromkeys(terms))


def _zh_term_has_evidence(term: str) -> bool:
    """中文内容词能否建立语料证据：自身 DF>0，或在翻译词典中（词典收录的
    都是语料相关术语，翻译后由 L1 变体负责命中——如 告警→alert）。"""
    if _token_df(term) > 0:
        return True
    return term in qr_mod._ZH_TO_EN


def _query_evidence_gate(query: str) -> str:
    """查询级语料证据检查（规则 G/F）。返回拒答原因，空串 = 通过。

    规则 G：查询存在 DF=0 的英文 token → 点名的专名/术语 KB 零覆盖 → 拒。
    规则 F：英文判别词集为空 + 中文内容词全部无证据（DF=0 且无翻译）→
            中文概念跨语言也建立不了证据 → 拒。
    详见 settings.SPARSE_UBIQUITOUS_RATIO 段注释。30 例标定零误伤。
    """
    tokens = _query_tokens(query)
    if tokens and any(_token_df(t) == 0 for t in tokens):
        return "no_subject_evidence"
    if not _discriminative_tokens(query):
        zh_terms = _zh_content_terms(query)
        if zh_terms and all(not _zh_term_has_evidence(w) for w in zh_terms):
            return "no_subject_evidence"
    return ""


def _sparse_gated(query: str, terms: list[str], top_k: int,
                  check_tokens: bool = True) -> list[tuple[object, float]]:
    """带闸门的 sparse 召回。返回通过闸门的 [(Chunk, score), ...]。

    两道闸门（2026-09-04 实测标定，见 settings.SPARSE_MIN / SPARSE_REQUIRE_EN_TOKEN）：
      1) 绝对分数下限：挡 < SPARSE_MIN 的近乎随机噪声。
      2) 判别词命中闸（主闸）：语料是英文 Dell 文档，sparse 的可靠信号必须是
         「查询的判别词（DF ≤ 泛词线的英文 token）命中 chunk 正文」。
         泛词（powerstore 等）命中不算证据；纯中文词经 jieba 匹配到中文文档
         属跨语言结构性误配，判别词一个都没命中，直接判负。

    check_tokens=False 供离线 mock 路径：注入向量不代表真实查询意图，跳过词项判断。
    """
    if not check_tokens or (not settings.SPARSE_REQUIRE_EN_TOKEN
                            and not settings.SPARSE_MIN):
        return store.sparse_search(terms, top_k)

    # 主闸前置判断：要求判别词信号，但查询的判别词集为空（纯中文或只有泛词）→
    # 语料是英文，此时 sparse 只能靠泛词/中文词误配，整路判空。
    if settings.SPARSE_REQUIRE_EN_TOKEN:
        disc = _discriminative_tokens(query)
        if not disc:
            return []
    else:
        disc = []

    hits = store.sparse_search(terms, top_k)
    gated: list[tuple[object, float]] = []
    for c, s in hits:
        if s < settings.SPARSE_MIN:
            continue
        if settings.SPARSE_REQUIRE_EN_TOKEN:
            # 有判别词：要求至少一个判别词命中该 chunk 正文，否则判噪声。
            content_l = c.content.lower()
            if not any(t in content_l for t in disc):
                continue
        gated.append((c, s))
    return gated


def _mpr(lists: list[list[tuple[str, float]]], k: int = 60,
         doc_of: dict[str, str] | None = None,
         cap: int = 0) -> dict[str, float]:
    """RRF 多路融合：lists 是 [(id, score), ...] 的列表，返回 {id: rrf_score}。

    只用名次丢弃分数的经典 RRF：sparse 无阈值时易把噪声挤进 top-k（架构评审
    已证明），所以这里所有路都要先经过各自的阈值/守门才参与融合。

    同文档票数封顶（cap > 0 且 doc_of 给出时，2026-09-04 深夜立项）：单一路内
    同 doc_id 只有前 cap 个 chunk 拿全额票，其后作废。BM25 会把泛词命中的同文件
    块成堆塞进 top20，堆内每块各 1 票，把「唯一真值文档的单票」压到榜尾
    （P24：svc 文档 12+ 块堆叠 vs GT 单票 0.0164）。封顶后堆叠坍塌，MMR 的
    同文档多样性惩罚才接得住。文件级 hit@k 安全：doc 前 cap 票永不作废
    （同 doc 即同文件）；跨变体共识每变体各 1 票，不受影响（P15 类不受伤）。
    """
    out: dict[str, float] = {}
    for ranked in lists:
        doc_votes: dict[str, int] = {}
        for rank, (cid, _s) in enumerate(ranked, 1):
            if cap > 0 and doc_of:
                d = doc_of.get(cid)
                if d is not None:
                    n = doc_votes.get(d, 0)
                    doc_votes[d] = n + 1
                    if n >= cap:
                        continue  # 同文档超出票数上限，本票作废
            out[cid] = out.get(cid, 0.0) + 1.0 / (k + rank)
    return out


def _mmr_select(candidates: list[ScoredChunk], top_n: int,
                lam: float = 0.6) -> list[ScoredChunk]:
    """MMR 去冗余：从 candidates 按 max-marginal-relevance 选 top_n。

    lam=0.6 偏重相关性；余下 0.4 留给多样性。

    rel 用「融合排名归一化」，不用 dense_score（2026-09-04 真机 P15/P25 定位）：
    sparse-only 命中的块 dense_score=0.0，若以其为 rel，MMR 恒判零相关，
    会把 sparse 救回的真值块系统性挤出 top-N——离线融合 73.3% 与线上 66.7%
    的差距全部出在这里。rrf 排名是融合后的权威信号：rank1=1.0 线性递减，末位 >0。
    dense-primary 单路时 rrf 是 dense 排名的单调映射，行为与旧版一致。
    """
    if len(candidates) <= top_n:
        return candidates

    order = sorted(range(len(candidates)),
                   key=lambda i: -candidates[i].rrf_score)
    rel_of = {idx: 1.0 - pos / len(candidates)
              for pos, idx in enumerate(order)}

    selected: list[ScoredChunk] = []
    selected_idx: list[int] = []
    pool = list(range(len(candidates)))
    while len(selected) < top_n and pool:
        if not selected:
            # 第一轮：融合排名最高的
            best_idx = max(pool, key=lambda i: rel_of[i])
        else:
            # 后续轮：MMR = λ·rel(q, c) - (1-λ)·max_sim(c, 已选)
            best_idx = None
            best_score = -1e9
            for i in pool:
                c = candidates[i]
                rel = rel_of[i]
                # 冗余近似（无 cross-encoder）：同文档共享率高触发降权
                max_red = 0.0
                for j in selected_idx:
                    s = candidates[j]
                    if c.chunk.doc_id == s.chunk.doc_id:
                        # 同文档算冗余；同页更高
                        page_red = 0.9 if c.chunk.page_start == s.chunk.page_start else 0.5
                        max_red = max(max_red, page_red)
                    elif c.chunk.file_path == s.chunk.file_path:
                        max_red = max(max_red, 0.3)
                mmr = lam * rel - (1 - lam) * max_red
                if mmr > best_score:
                    best_score = mmr
                    best_idx = i
        selected.append(candidates[best_idx])
        selected_idx.append(best_idx)
        pool.remove(best_idx)
    return selected


def retrieve(query: str, mock_vec: list[float] | None = None) -> tuple[list[ScoredChunk], str]:
    """深度优化版 retrieve。

    mock_vec 仅供离线测试：跳过 embed_query 直接用给定向量走 dense 主力。
    """
    use_rewrite = qr_mod.is_enabled() and mock_vec is None
    use_hyde = hyde_mod.is_enabled() and mock_vec is None

    # 0) 查询级语料证据闸（规则 G/F，零幻觉前置防线）——仅真实查询路径生效。
    #    mock 是离线测试注入的向量，不代表真实查询意图，跳过语义判断。
    #    拒答发生在 embedding 之前：被拒查询零模型开销。
    if mock_vec is None:
        gate_reason = _query_evidence_gate(query)
        if gate_reason:
            return [], gate_reason

    # 判别词集：dense 候选池的词项证据检查用（仅真实查询路径）。
    # 判别词非空时，候选必须命中 ≥1 个判别词——「PowerStore + 不存在的东西」
    # 类查询的泛词高分 chunk（N03 的 BBU 文档 0.700）据此出池。
    disc_tokens = _discriminative_tokens(query) if mock_vec is None else []

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

    # 2) 每个 query embed → dense_search → 收集候选（仅 ≥DENSE_MIN 且过判别词检查）
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
        if disc_tokens:
            # 词项证据检查：候选须命中 ≥1 个判别词（泛词命中不算证据）
            valid = [(c, s) for c, s in valid
                     if any(t in c.content.lower() for t in disc_tokens)]
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
        doc_of = {s.chunk.chunk_id: s.chunk.doc_id
                  for s in dense_chunks.values()}
        rrf = _mpr(dense_id_lists, k=settings.RRF_K, doc_of=doc_of,
                   cap=settings.RRF_DOC_VOTE_CAP)
        # 用 rrf 重排候选
        candidates = sorted(dense_chunks.values(),
                            key=lambda s: -rrf.get(s.chunk.chunk_id, 0.0))
        # 强制按 rrf_score 更新（供 CLI debug 打印）
        for s in candidates:
            s.rrf_score = rrf.get(s.chunk.chunk_id, 0.0)
    else:
        candidates = []

    # 4) sparse 召回（过闸门）。SPARSE_FALLBACK_ONLY=False 时才参与融合，
    #    True 时 dense 有候选就只信 dense（逃生舱降级语义）。
    sparse_valid = None
    if not settings.SPARSE_FALLBACK_ONLY or not candidates:
        terms = store.query_terms(query)
        sparse_valid = _sparse_gated(query, terms, settings.SPARSE_TOP_K,
                                     check_tokens=mock_vec is None)

    # 5) 融合决策：
    #    - 全量 RRF（SPARSE_FALLBACK_ONLY=False，或 dense 空手时兜底）：dense + sparse 一起 RRF。
    #    - dense 主力（SPARSE_FALLBACK_ONLY=True 且 dense 有候选）：只信 dense，跳过 sparse。
    if candidates and settings.SPARSE_FALLBACK_ONLY:
        if getattr(settings, "MMR_ENABLED", True):
            final = _mmr_select(candidates, settings.FINAL_TOP_N)
        else:
            final = candidates[: settings.FINAL_TOP_N]
        return final, ""

    # 6) 全量 RRF 融合：把 sparse（已过闸）并入 dense，一起 RRF。
    if sparse_valid:
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
        # 此时 dense_chunks 已含 sparse 块，doc_of 覆盖全部参与融合的 chunk
        doc_of = {s.chunk.chunk_id: s.chunk.doc_id
                  for s in dense_chunks.values()}
        rrf = _mpr(dense_id_lists, k=settings.RRF_K, doc_of=doc_of,
                   cap=settings.RRF_DOC_VOTE_CAP)
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