"""retriever 改造的单元测试（不依赖 Ollama / 真 embedding）。

覆盖：
  1. _mpr（RRF）正确性
  2. _mmr_select 去冗余正确性（同 doc_id / 同 page 应被降权）
  3. query_rewrite 中文→英文、svc_xxx 归一、长 query 拆分
  4. hyde 失败时返回 [query] 不报错
  5. retriever 整体：mock_vec 路径不调 embed_query、不调 LLM
  6. retriever：3 个开关（QUERY_REWRITE / HYDE / MMR）独立降级

跑法：
    python scripts/test_retrieval_logic.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mini_rag.core import hyde as hyde_mod  # noqa: E402
from mini_rag.core import query_rewrite as qr  # noqa: E402
from mini_rag.core import retriever  # noqa: E402
from mini_rag.core.schema import Chunk, ScoredChunk  # noqa: E402


def _chunk(cid: str, doc_id: str = "d1", file_path: str = "/x.pdf",
           file_name: str = "x.pdf", page: int = 1,
           content: str = "") -> Chunk:
    return Chunk(chunk_id=cid, content=content, file_path=file_path,
                file_name=file_name, page_start=page, page_end=page,
                doc_id=doc_id)


def _scored(cid: str, dense: float, **kw) -> ScoredChunk:
    base = dict(
        doc_id="d1", file_path="/x.pdf", file_name="x.pdf", page=1,
        content=cid)
    base.update(kw)
    c = _chunk(cid, **{k: v for k, v in base.items()
                        if k in ("doc_id", "file_path", "file_name", "page")})
    return ScoredChunk(chunk=c, dense_score=dense, sparse_score=0.0,
                       rrf_score=dense, matched_by="dense")


# ============ 1) _mpr ============
def test_mpr_basic():
    rrf = retriever._mpr([
        [("a", 0.9), ("b", 0.8), ("c", 0.7)],   # rank1,2,3
        [("b", 0.9), ("a", 0.8), ("d", 0.7)],   # rank1,2,3
    ], k=60)
    # a: 1/(60+1) + 1/(60+2) = 0.01639 + 0.01613 = 0.03252
    # b: 1/(60+2) + 1/(60+1) = 同 a（次序不同但累加相同）
    # c: 1/(60+3) = 0.01587
    # d: 1/(60+3) = 0.01587
    assert abs(rrf["a"] - rrf["b"]) < 1e-9, f"a==b 累加应对等: {rrf}"
    assert rrf["a"] > rrf["c"], "a 在两路都出现，应 > c"
    assert rrf["d"] == rrf["c"], "c 与 d 同样只在 1 路 rank3"
    print("✓ test_mpr_basic")


# ============ 2) _mmr_select ============
def test_mmr_dedup():
    # 6 个候选：A 高分同页；B 中分同页；C 低分不同页；D 高分不同 doc；
    # E 高分不同 doc 但同 doc_id=2；F 高分不同 doc 不同 doc_id
    candidates = [
        _scored("a", 0.95, doc_id="d1", page=1),
        _scored("b", 0.80, doc_id="d1", page=1),  # 同 d1 同 page → 与 a 重
        _scored("c", 0.70, doc_id="d1", page=2),
        _scored("d", 0.90, doc_id="d2", page=1),
        _scored("e", 0.88, doc_id="d2", page=2),
        _scored("f", 0.85, doc_id="d3", page=1),
    ]
    sel = retriever._mmr_select(candidates, top_n=3)
    ids = [s.chunk.chunk_id for s in sel]
    # 第一轮必选 a（dense 最高）；后续 MMR 应避开 a 同页的 b，倾向 d/f
    assert ids[0] == "a", f"MMR 首项必须是 dense 最高，实际 {ids}"
    assert "b" not in ids, f"b 与 a 同 d1 同 page，应被 MMR 降权: {ids}"
    # top3 应该尽量覆盖不同 doc
    distinct_docs = set([s.chunk.doc_id for s in sel])
    assert len(distinct_docs) >= 2, f"应跨 doc 取多样本: {sel}"
    print(f"  MMR 选: {ids} → docs: {distinct_docs}")
    print("✓ test_mmr_dedup")


# ============ 3) query_rewrite ============
def test_rewrite_zh_to_en():
    qs = qr.rewrite("如何更换 BBU？")
    assert qs[0] == "如何更换 BBU？", "原 query 必须第一位"
    assert any("BBU" in q and "battery" in q.lower() for q in qs[1:]), \
        f"中文 BBU/电池应翻 BBU battery: {qs}"
    # 不应误翻弱信号词
    assert not any("如何" in q and q != "如何更换 BBU？" for q in qs), \
        "弱信号词不应被保留"
    print(f"  改写: {qs}")
    print("✓ test_rewrite_zh_to_en")


def test_rewrite_svc_norm():
    qs = qr.rewrite("PowerStore svc_db_recovery 怎么用")
    # 期望：svc_db_recovery 归一到 "db_recovery" 和 "db recovery"
    assert any("db_recovery" in q for q in qs), f"svc 归一失败: {qs}"
    assert any("db recovery" in q for q in qs), f"svc 空格归一失败: {qs}"
    print(f"  改写: {qs}")
    print("✓ test_rewrite_svc_norm")


def test_rewrite_long_split():
    qs = qr.rewrite("PowerStore 集群内 svc_journalctl 怎么看日志")
    # 长 query 含 "和"/"内"等分隔？但「内」不在分隔列表里 → 不拆
    assert any("journalctl" in q.lower() for q in qs), f"svc 归一失败: {qs}"
    print(f"  改写: {qs}")
    print("✓ test_rewrite_long_split")


def test_rewrite_no_zh_no_change():
    """纯英文无中文术语匹配时，原 query + svc 归一（如果有 svc_xxx）。"""
    qs = qr.rewrite("svc_factory_reset")
    # 无中文术语，但 svc_xxx 归一仍触发 → 应有原 query + factory_reset + factory reset
    assert qs[0] == "svc_factory_reset"
    assert "factory_reset" in qs
    assert "factory reset" in qs
    print(f"  改写: {qs}")
    print("✓ test_rewrite_no_zh_no_change")


# ============ 4) hyde 失败降级 ============
def test_hyde_fail_falls_back():
    """LLM 不可达 → expand 返回 [query]，不报错。"""
    with patch.object(hyde_mod, "_hyde_llm", return_value=""):
        out = hyde_mod.expand("BBU 怎么换？", llm_generate=True)
    assert out == ["BBU 怎么换？"], f"降级失败: {out}"
    print("✓ test_hyde_fail_falls_back")


def test_hyde_success():
    """LLM 返回正常 → expand 返回 [query, fake]。"""
    with patch.object(hyde_mod, "_hyde_llm",
                      return_value="Run svc_factory_reset to reinitialize the PowerStore cluster to factory defaults."):
        out = hyde_mod.expand("BBU 怎么换？", llm_generate=True)
    assert len(out) == 2 and out[1].startswith("Run svc_factory_reset"), out
    print(f"  expand: {out}")
    print("✓ test_hyde_success")


# ============ 5) retriever mock_vec 不调 embed_query / 不调 LLM ============
def test_retrieve_mock_no_embedder_no_llm():
    """mock_vec 路径不调 embed_query、不调 LLM（verify 不联网）。"""
    fake_vec = [0.0] * 2560

    # store.dense_search 返回 5 条
    fake_hits = [
        (_chunk(f"c{i}", doc_id=f"d{i % 2}", page=i), 0.9 - i * 0.05)
        for i in range(5)
    ]

    with patch("mini_rag.core.retriever.embed_query") as mock_embed, \
         patch("mini_rag.core.retriever.store.dense_search", return_value=fake_hits), \
         patch("mini_rag.core.retriever.hyde_mod.expand") as mock_hyde:
        scored, reason = retriever.retrieve("test query", mock_vec=fake_vec)

    mock_embed.assert_not_called()  # 关键断言：mock 路径不调 embedder
    mock_hyde.assert_not_called()   # 关键断言：mock 路径不调 HyDE
    assert not reason, f"应有结果，原因应为 ''，实为 {reason}"
    assert len(scored) > 0, "应有召回"
    assert scored[0].chunk.chunk_id == "c0", "dense 最高应排第一"
    print(f"  top1={scored[0].chunk.chunk_id} score={scored[0].dense_score:.3f} "
          f"n={len(scored)}")
    print("✓ test_retrieve_mock_no_embedder_no_llm")


# ============ 6) 开关独立降级 ============
def test_disable_rewrite():
    """关掉 QUERY_REWRITE_ENABLED → retriever 不调 query_rewrite.rewrite。"""
    fake_vec = [0.0] * 2560
    fake_hits = [(_chunk("c1", doc_id="d1"), 0.9)]
    with patch("mini_rag.config.settings.QUERY_REWRITE_ENABLED", False), \
         patch("mini_rag.core.retriever.store.dense_search", return_value=fake_hits), \
         patch("mini_rag.core.retriever.qr_mod.rewrite") as mock_rewrite:
        scored, _ = retriever.retrieve("test", mock_vec=fake_vec)
    mock_rewrite.assert_not_called()
    assert len(scored) == 1
    print("✓ test_disable_rewrite")


def test_disable_hyde():
    """关掉 HYDE_ENABLED → 不调 hyde.expand。"""
    fake_vec = [0.0] * 2560
    fake_hits = [(_chunk("c1", doc_id="d1"), 0.9)]
    with patch("mini_rag.config.settings.HYDE_ENABLED", False), \
         patch("mini_rag.core.retriever.store.dense_search", return_value=fake_hits), \
         patch("mini_rag.core.retriever.hyde_mod.expand") as mock_hyde:
        scored, _ = retriever.retrieve("test", mock_vec=fake_vec)
    mock_hyde.assert_not_called()
    print("✓ test_disable_hyde")


def test_disable_mmr():
    """关掉 MMR_ENABLED → _mmr_select 不被调用，按 rrf 排序直接切。"""
    fake_vec = [0.0] * 2560
    fake_hits = [(_chunk(f"c{i}", doc_id=f"d{i}", page=i), 0.9 - i * 0.05)
                 for i in range(5)]
    with patch("mini_rag.config.settings.MMR_ENABLED", False), \
         patch("mini_rag.core.retriever.store.dense_search", return_value=fake_hits), \
         patch("mini_rag.core.retriever._mmr_select") as mock_mmr:
        scored, _ = retriever.retrieve("test", mock_vec=fake_vec)
    mock_mmr.assert_not_called()
    # FINAL_TOP_N=4，应该返回 4 个
    assert len(scored) == 4, f"MMR off 应按 RRF 直接切 {4} 个，实为 {len(scored)}"
    print("✓ test_disable_mmr")


def main() -> int:
    tests = [
        test_mpr_basic,
        test_mmr_dedup,
        test_rewrite_zh_to_en,
        test_rewrite_svc_norm,
        test_rewrite_long_split,
        test_rewrite_no_zh_no_change,
        test_hyde_fail_falls_back,
        test_hyde_success,
        test_retrieve_mock_no_embedder_no_llm,
        test_disable_rewrite,
        test_disable_hyde,
        test_disable_mmr,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"✗ {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"✗ {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n通过 {passed}/{len(tests)} | 失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())