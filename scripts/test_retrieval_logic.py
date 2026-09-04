"""retriever 改造的单元测试（不依赖 Ollama / 真 embedding）。

覆盖：
  1. _mpr（RRF）正确性
  2. _mmr_select 去冗余正确性（同 doc_id / 同 page 应被降权）
  3. query_rewrite 中文→英文、svc_xxx 归一、长 query 拆分
  3b. query_rewrite 保留英文专名（P15/P21 的关键修复）
  4. hyde 失败时返回 [query] 不报错
  4b. hyde LRU 缓存：命中跳过 LLM / 未命中调 LLM / 淘汰 / 持久化
  4c. hyde 段落词数截断（30-40 词压缩）
  5. retriever 整体：mock_vec 路径不调 embed_query、不调 LLM
  6. retriever：3 个开关（QUERY_REWRITE / HYDE / MMR）独立降级
  7. generator.validate_answer（L3 零幻觉第三道）四条规则

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
    c = _chunk(cid, content=base.get("content", ""),
               **{k: v for k, v in base.items()
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


# ============ 1b) _mpr 同文档票数封顶（2026-09-04 深夜立项）============
def test_mpr_doc_cap_off_equals_plain():
    """cap=0（关闭）时与经典 RRF 完全一致。"""
    lists = [[("a", 0.9), ("b", 0.8), ("c", 0.7)]]
    doc_of = {"a": "d1", "b": "d1", "c": "d1"}
    assert retriever._mpr(lists, k=60, doc_of=doc_of, cap=0) == \
        retriever._mpr(lists, k=60)
    print("✓ test_mpr_doc_cap_off_equals_plain")


def test_mpr_doc_cap_collapses_pile():
    """同 doc 3 块 rank1/2/3，cap=2 → 第 3 块票作废；异 doc 不受影响。"""
    lists = [[("a", 0.9), ("b", 0.8), ("c", 0.7), ("x", 0.6)]]
    doc_of = {"a": "d1", "b": "d1", "c": "d1", "x": "d9"}
    rrf = retriever._mpr(lists, k=60, doc_of=doc_of, cap=2)
    assert abs(rrf["a"] - 1 / 61) < 1e-9
    assert abs(rrf["b"] - 1 / 62) < 1e-9
    assert "c" not in rrf, f"同 doc 第 3 票应作废: {rrf}"
    assert abs(rrf["x"] - 1 / 64) < 1e-9, "异 doc 的票不应受影响"
    print("✓ test_mpr_doc_cap_collapses_pile")


def test_mpr_doc_cap_keeps_cross_variant_consensus():
    """跨变体共识不受影响：同 doc 在 3 个 list 各占 rank1，每路第 1 票都全额。"""
    lists = [
        [("a", 0.9), ("z", 0.8)],
        [("a", 0.9), ("y", 0.8)],
        [("a", 0.9), ("w", 0.8)],
    ]
    doc_of = {"a": "d1", "z": "d2", "y": "d3", "w": "d4"}
    rrf = retriever._mpr(lists, k=60, doc_of=doc_of, cap=2)
    assert abs(rrf["a"] - 3 / 61) < 1e-9, f"3 路各 1 票应全额累加: {rrf}"
    print("✓ test_mpr_doc_cap_keeps_cross_variant_consensus")


def test_mpr_doc_cap_file_level_safety():
    """文件级安全：GT 块是同 doc 第 3 名时其票作废，但同文件前 2 块仍有票在榜
    （doc_id=file_hash 前缀，同 doc 即同文件，hit@k 是文件级）。"""
    lists = [[("g1", 0.9), ("g2", 0.8), ("gt", 0.7), ("o", 0.6)]]
    doc_of = {"g1": "dg", "g2": "dg", "gt": "dg", "o": "d9"}
    rrf = retriever._mpr(lists, k=60, doc_of=doc_of, cap=2)
    assert "gt" not in rrf
    assert abs(rrf["g1"] - 1 / 61) < 1e-9, "同文件第 1 票必须全额保留"
    assert rrf["g1"] > rrf["o"], "文件 dg 仍应高于只 rank4 的 o"
    print("✓ test_mpr_doc_cap_file_level_safety")


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


# ============ 3b) query_rewrite 保留英文专名（P15/P21 关键修复）============
def test_rewrite_keeps_english_proper_nouns():
    """中文改写必须保留 query 里的英文专名（产品名 / 版本号 / svc 命令）。

    这是 P15 / P21 的真正病因：旧实现只输出中文术语的英文翻译，
    把 "PowerStore 4.3.0.0 release note"、"svc_journalctl" 这类最强信号全丢了，
    而 ground_truth 恰恰就是这些英文专名。
    """
    # P15：ground_truth = pwrstr-4-3-0-0-rn → 版本号 + release note 必须留住
    qs = qr.rewrite("PowerStore 4.3.0.0 release note 变更内容")
    assert any("PowerStore" in q and "4.3.0.0" in q and "release" in q.lower()
               for q in qs), f"P15 英文专名丢了: {qs}"
    assert any("change" in q.lower() for q in qs), f"P15「变更」未翻译: {qs}"

    # P21：ground_truth = svc_journalctl → svc_ 前缀必须留住
    qs = qr.rewrite("svc_journalctl 怎么看日志")
    assert any("svc_journalctl" in q for q in qs), f"P21 svc_journalctl 丢了: {qs}"

    # 通用：英文产品名 NAS server
    qs = qr.rewrite("如何查看 NAS server 状态")
    assert any("NAS" in q and "server" in q for q in qs), f"NAS server 丢了: {qs}"

    # 翻译词不应与专名重复（"如何更换 BBU？" 不应出现 "BBU ... BBU battery"）
    qs = qr.rewrite("如何更换 BBU？")
    for q in qs[1:]:
        assert q.lower().count("bbu") <= 1, f"BBU 重复: {q!r}"
    print("✓ test_rewrite_keeps_english_proper_nouns")


# ============ 4) hyde 失败降级 ============
def test_hyde_fail_falls_back():
    """LLM 不可达 → expand 返回 [query]，不报错。"""
    with patch.object(hyde_mod, "_hyde_llm", return_value=""):
        with patch("mini_rag.config.settings.HYDE_CACHE_ENABLED", False):
            out = hyde_mod.expand("BBU 怎么换？", llm_generate=True)
    assert out == ["BBU 怎么换？"], f"降级失败: {out}"
    print("✓ test_hyde_fail_falls_back")


def test_hyde_success():
    """LLM 返回正常 → expand 返回 [query, fake]。"""
    with patch("mini_rag.config.settings.HYDE_CACHE_ENABLED", False):
        with patch.object(hyde_mod, "_hyde_llm",
                          return_value="Run svc_factory_reset to reinitialize the PowerStore cluster to factory defaults."):
            out = hyde_mod.expand("BBU 怎么换？", llm_generate=True)
    assert len(out) == 2 and out[1].startswith("Run svc_factory_reset"), out
    print(f"  expand: {out}")
    print("✓ test_hyde_success")


# ============ 4b) hyde LRU 缓存 ============
def _tmp_cache():
    import tempfile
    from pathlib import Path as _P
    return _P(tempfile.mkdtemp()) / "hyde_cache.jsonl"


def test_hyde_cache_hit_skips_llm():
    """缓存命中 → 完全跳过 LLM 调用（12s → ~1ms），这是压缩 latency 的主力。"""
    cache_file = _tmp_cache()
    with patch("mini_rag.config.settings.HYDE_CACHE_PATH", cache_file), \
         patch("mini_rag.config.settings.HYDE_CACHE_ENABLED", True), \
         patch.object(hyde_mod, "_cache", None), \
         patch.object(hyde_mod, "_hyde_llm", return_value="fake doc about BBU") as mock_llm:
        out1 = hyde_mod.expand("BBU 怎么换", llm_generate=True)
        assert mock_llm.call_count == 1, "首次未命中应调 LLM"
        assert out1 == ["BBU 怎么换", "fake doc about BBU"], out1

        out2 = hyde_mod.expand("BBU 怎么换", llm_generate=True)
        assert mock_llm.call_count == 1, \
            f"命中缓存不应再调 LLM，实为 {mock_llm.call_count} 次"
        assert out2 == out1, "两次结果应一致"

        # 归一化：多一个问号 / 大小写不同应命中同一条
        out3 = hyde_mod.expand("BBU 怎么换？", llm_generate=True)
        assert mock_llm.call_count == 1, f"问号差异应命中同一条，实为 {mock_llm.call_count}"
        assert out3[1] == out1[1]
    print("✓ test_hyde_cache_hit_skips_llm")


def test_hyde_cache_persists_across_processes():
    """缓存落盘 → 新进程（_cache=None）能读到。CLI 每次 ask 都是新进程，这条是关键。"""
    import json
    cache_file = _tmp_cache()
    with patch("mini_rag.config.settings.HYDE_CACHE_PATH", cache_file), \
         patch("mini_rag.config.settings.HYDE_CACHE_ENABLED", True), \
         patch.object(hyde_mod, "_cache", None), \
         patch.object(hyde_mod, "_hyde_llm", return_value="persisted doc") as mock_llm:
        hyde_mod.expand("持久化测试", llm_generate=True)
        assert mock_llm.call_count == 1

    # 模拟新进程：_cache 重置为 None，只能从文件读
    assert cache_file.exists(), "缓存未落盘"
    with patch("mini_rag.config.settings.HYDE_CACHE_PATH", cache_file), \
         patch("mini_rag.config.settings.HYDE_CACHE_ENABLED", True), \
         patch.object(hyde_mod, "_cache", None), \
         patch.object(hyde_mod, "_hyde_llm", return_value="SHOULD NOT BE CALLED") as mock_llm:
        out = hyde_mod.expand("持久化测试", llm_generate=True)
    assert mock_llm.call_count == 0, "新进程应从磁盘缓存命中，不该调 LLM"
    assert out == ["持久化测试", "persisted doc"], out

    # jsonl 格式校验：一行一条，含 query/doc/ts
    lines = [l for l in cache_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1, f"应只有 1 条缓存，实为 {len(lines)}"
    rec = json.loads(lines[0])
    assert "query" in rec and "doc" in rec and "ts" in rec, f"字段缺失: {rec}"
    print("✓ test_hyde_cache_persists_across_processes")


def test_hyde_cache_lru_eviction():
    """超过 HYDE_CACHE_SIZE 淘汰最久未用（最前），保留最近用的。"""
    cache_file = _tmp_cache()
    with patch("mini_rag.config.settings.HYDE_CACHE_PATH", cache_file), \
         patch("mini_rag.config.settings.HYDE_CACHE_ENABLED", True), \
         patch("mini_rag.config.settings.HYDE_CACHE_SIZE", 3), \
         patch.object(hyde_mod, "_cache", None), \
         patch.object(hyde_mod, "_hyde_llm", side_effect=lambda q: f"doc for {q}"):
        for i in range(5):
            hyde_mod.expand(f"q{i}", llm_generate=True)
        assert len(hyde_mod._cache) == 3, f"应淘汰到 3 条，实为 {len(hyde_mod._cache)}"
        keys = list(hyde_mod._cache.keys())
        # 最久未用的 q0/q1 应被淘汰，q2/q3/q4 保留
        assert keys == ["q2", "q3", "q4"], f"淘汰顺序错: {keys}"
    print("✓ test_hyde_cache_lru_eviction")


def test_hyde_cache_disabled():
    """关掉 HYDE_CACHE_ENABLED → 每次都调 LLM（用于评估对照）。"""
    cache_file = _tmp_cache()
    with patch("mini_rag.config.settings.HYDE_CACHE_PATH", cache_file), \
         patch("mini_rag.config.settings.HYDE_CACHE_ENABLED", False), \
         patch.object(hyde_mod, "_cache", None), \
         patch.object(hyde_mod, "_hyde_llm", return_value="doc") as mock_llm:
        hyde_mod.expand("q", llm_generate=True)
        hyde_mod.expand("q", llm_generate=True)
    assert mock_llm.call_count == 2, f"关缓存应每次都调 LLM，实为 {mock_llm.call_count}"
    print("✓ test_hyde_cache_disabled")


# ============ 4c) hyde 段落长度压缩 ============
def test_hyde_truncate_words():
    """段落超过 HYDE_MAX_WORDS 被硬截断（30-40 词压缩的保险丝）。"""
    long_doc = " ".join(f"w{i}" for i in range(100))
    with patch("mini_rag.config.settings.HYDE_MAX_WORDS", 45):
        out = hyde_mod._truncate_words(long_doc, 45)
    assert len(out.split()) == 45, f"应截断到 45 词，实为 {len(out.split())}"

    short = "Run svc_factory_reset now."
    assert hyde_mod._truncate_words(short, 45) == short, "短段落不应被改"
    print("✓ test_hyde_truncate_words")


def test_hyde_prompt_is_short():
    """HYDE_PROMPT 必须要求 30-40 词（压缩目标写进 prompt，不只是截断）。"""
    assert "30-40" in hyde_mod.HYDE_PROMPT, "prompt 未要求 30-40 词"
    assert "30-40 words is a HARD LIMIT" in hyde_mod.HYDE_PROMPT
    print("✓ test_hyde_prompt_is_short")


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
         patch("mini_rag.core.retriever.store.query_terms", return_value=[]), \
         patch("mini_rag.core.retriever.store.sparse_search", return_value=[]), \
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
         patch("mini_rag.core.retriever.store.query_terms", return_value=[]), \
         patch("mini_rag.core.retriever.store.sparse_search", return_value=[]), \
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
         patch("mini_rag.core.retriever.store.query_terms", return_value=[]), \
         patch("mini_rag.core.retriever.store.sparse_search", return_value=[]), \
         patch("mini_rag.core.retriever._mmr_select") as mock_mmr:
        scored, _ = retriever.retrieve("test", mock_vec=fake_vec)
    mock_mmr.assert_not_called()
    # FINAL_TOP_N=4，应该返回 4 个
    assert len(scored) == 4, f"MMR off 应按 RRF 直接切 {4} 个，实为 {len(scored)}"
    print("✓ test_disable_mmr")


# ============ 7) sparse 阈值闸门（2026-09-04 全量 RRF 融合前置）============
def test_sparse_gate_pure_chinese_rejected():
    """纯中文查询（无英文 token）→ sparse 整路判空，不参与融合。"""
    from mini_rag.core import retriever as r
    with patch.object(r.settings, "SPARSE_REQUIRE_EN_TOKEN", True), \
         patch.object(r.settings, "SPARSE_MIN", 8.0), \
         patch("mini_rag.core.retriever.store.sparse_search",
              return_value=[(_chunk("n1", content="PowerStore 使用服务 LAN 端口"),
                             20.0)]):
        gated = r._sparse_gated("交换机怎么配置", ["交换机", "配置"], 20)
    assert gated == [], f"纯中文查询 sparse 应判空: {gated}"
    print("✓ test_sparse_gate_pure_chinese_rejected")


def test_sparse_gate_english_token_not_in_content_rejected():
    """查询有英文 token 但未命中 chunk 正文 → 判跨语言噪声。"""
    from mini_rag.core import retriever as r
    # N02 场景：查 Brocade/zone，召回的是中文文档（英文 token 不在正文）
    with patch.object(r.settings, "SPARSE_REQUIRE_EN_TOKEN", True), \
         patch.object(r.settings, "SPARSE_MIN", 8.0), \
         patch("mini_rag.core.retriever.store.sparse_search",
              return_value=[(_chunk("n2", content="使用服务 LAN 端口访问 SSH 和 PowerStore Manager"),
                             16.76)]):
        gated = r._sparse_gated("如何在 Brocade 交换机上配置 zone",
                                ["Brocade", "交换机", "配置", "zone"], 20)
    assert gated == [], f"英文 token 未命中正文应判噪声: {gated}"
    print("✓ test_sparse_gate_english_token_not_in_content_rejected")


def test_sparse_gate_english_token_hit_passes():
    """查询英文 token 命中 chunk 正文 → 放行。"""
    from mini_rag.core import retriever as r
    with patch.object(r.settings, "SPARSE_REQUIRE_EN_TOKEN", True), \
         patch.object(r.settings, "SPARSE_MIN", 8.0), \
         patch("mini_rag.core.retriever.store.sparse_search",
              return_value=[(_chunk("p", content="Run svc_factory_reset to reinitialize the PowerStore"),
                             28.06)]):
        gated = r._sparse_gated("svc_factory_reset 怎么用",
                                ["svc_factory_reset", "怎么"], 20)
    assert len(gated) == 1, f"英文 token 命中应放行: {gated}"
    print("✓ test_sparse_gate_english_token_hit_passes")


def test_sparse_gate_low_score_rejected():
    """绝对分数 < SPARSE_MIN → 挡掉近乎随机噪声。"""
    from mini_rag.core import retriever as r
    with patch.object(r.settings, "SPARSE_REQUIRE_EN_TOKEN", False), \
         patch.object(r.settings, "SPARSE_MIN", 8.0), \
         patch("mini_rag.core.retriever.store.sparse_search",
              return_value=[(_chunk("low", content="something"), 3.5)]):
        gated = r._sparse_gated("quantum encryption", ["quantum", "encryption"], 20)
    assert gated == [], f"低分应被 SPARSE_MIN 挡掉: {gated}"
    print("✓ test_sparse_gate_low_score_rejected")


# ============ 7.5) 查询级语料证据闸（规则 G/F，2026-09-04 晚）============
def test_query_gate_absent_english_token_rejected():
    """规则 G：查询存在 DF=0 的英文 token（brocade）→ KB 零覆盖 → 拒答。"""
    from mini_rag.core import retriever as r
    reason = r._query_evidence_gate("如何在 Brocade 交换机上配置 zone")
    assert reason == "no_subject_evidence", f"brocade DF=0 应拒答: {reason!r}"
    print("✓ test_query_gate_absent_english_token_rejected")


def test_query_gate_chinese_no_evidence_rejected():
    """规则 F：判别词空（powerstore 是泛词）+ 中文概念全无证据（量子/加密
    DF=0 且不在词典）→ 拒答。"""
    from mini_rag.core import retriever as r
    reason = r._query_evidence_gate("PowerStore 量子加密")
    assert reason == "no_subject_evidence", f"量子加密应拒答: {reason!r}"
    print("✓ test_query_gate_chinese_no_evidence_rejected")


def test_query_gate_passes_answerable_queries():
    """正例不误伤：词典翻译词（告警→alert）与罕见但存在的术语都放行。"""
    from mini_rag.core import retriever as r
    # P05：中文词 DF=0，但 故障/告警 在词典 → 有证据
    assert r._query_evidence_gate("PowerStore 数据库卷故障告警") == "", \
        "告警可翻译成 alert，不应拒答"
    # P02：bbu DF=52 判别词非空，无 DF=0 token → 通过
    assert r._query_evidence_gate("PowerStore 怎么更换 BBU？") == "", \
        "bbu 存在于语料，不应拒答"
    # P25：全英文罕见术语，DF 全 >0 → 通过
    assert r._query_evidence_gate("TRIF Metro SCSI Persistent Reservations") == "", \
        "P25 术语都存在，不应拒答"
    print("✓ test_query_gate_passes_answerable_queries")


def test_dense_candidates_require_discriminative_hit():
    """dense 判别词检查：高分 chunk 未命中任何判别词 → 出池（N03 场景：
    「PowerStore Docker 安装」的 BBU 文档 0.700 不含 docker，不算证据）。"""
    from mini_rag.core import retriever as r
    fake_vec = [0.0] * 2560
    # 高分 chunk：不含 docker → 判别词检查拒绝
    fake_hits = [(_chunk("c1", content="How to identify failing BBU for replacement"), 0.70)]
    with patch("mini_rag.core.retriever.embed_query", return_value=fake_vec), \
         patch("mini_rag.core.retriever.store.dense_search", return_value=fake_hits), \
         patch("mini_rag.core.retriever.store.query_terms", return_value=[]), \
         patch("mini_rag.core.retriever.store.sparse_search", return_value=[]), \
         patch("mini_rag.core.retriever.qr_mod.rewrite",
               return_value=["PowerStore Docker 安装"]), \
         patch("mini_rag.core.retriever.hyde_mod.is_enabled", return_value=False):
        scored, reason = r.retrieve("PowerStore Docker 安装")
    assert scored == [], f"未命中判别词的高分 chunk 应出池: {scored}"
    assert reason == "below_threshold", f"reason 应为 below_threshold: {reason!r}"
    # 反向：chunk 命中判别词 docker → 保留
    fake_hits2 = [(_chunk("c2", content="Docker containers are not supported"), 0.70)]
    with patch("mini_rag.core.retriever.embed_query", return_value=fake_vec), \
         patch("mini_rag.core.retriever.store.dense_search", return_value=fake_hits2), \
         patch("mini_rag.core.retriever.store.query_terms", return_value=[]), \
         patch("mini_rag.core.retriever.store.sparse_search", return_value=[]), \
         patch("mini_rag.core.retriever.qr_mod.rewrite",
               return_value=["PowerStore Docker 安装"]), \
         patch("mini_rag.core.retriever.hyde_mod.is_enabled", return_value=False):
        scored2, reason2 = r.retrieve("PowerStore Docker 安装")
    assert len(scored2) == 1 and scored2[0].chunk.chunk_id == "c2", \
        f"命中判别词的 chunk 应保留: {scored2}"
    print("✓ test_dense_candidates_require_discriminative_hit")


def test_sparse_gate_mock_path_skips_token_check():
    """check_tokens=False（离线 mock 路径）→ 不做判别词判断，直接返回 sparse 结果。"""
    from mini_rag.core import retriever as r
    with patch("mini_rag.core.retriever.store.sparse_search",
              return_value=[(_chunk("m1", content="任意内容"), 20.0)]):
        gated = r._sparse_gated("纯中文无判别词", ["词"], 20, check_tokens=False)
    assert len(gated) == 1, f"mock 路径应跳过词项检查: {gated}"
    print("✓ test_sparse_gate_mock_path_skips_token_check")


# ============ 8) L3 生成后校验（零幻觉第三道）============
# generator.validate_answer 已在 mini_rag/core/generator.py 实现（4 条规则），
# 但此前无测试覆盖。这里补齐，并显式覆盖「误伤」边界——
# 校验过严会把忠实转述也降级，等于把可用性让给零幻觉，两头不讨好。
from mini_rag.core import generator as gen  # noqa: E402


def _ctx(*contents: str):
    """按内容构造 ScoredChunk 列表。"""
    return [_scored(f"c{i}", 0.9 - i * 0.01, content=c)
            for i, c in enumerate(contents)]


def test_validate_citation_out_of_range():
    """[N] 中 N 超出片段数 = 编造引用 → 拦截。"""
    scored = _ctx("Run svc_factory_reset to reset the cluster.")
    ok, why = gen.validate_answer("见[片段7]的说明。", scored)
    assert not ok and "越界" in why, f"应拦截越界引用: {ok}, {why}"
    # 未越界应放行
    ok, why = gen.validate_answer("见[片段1]的说明。", scored)
    assert ok, f"片段1 存在不应拦截: {why}"
    print("✓ test_validate_citation_out_of_range")


def test_validate_inference_phrase():
    """推断话术（诉诸外部知识）→ 拦截。"""
    scored = _ctx("Run svc_factory_reset to reset the cluster.")
    ok, why = gen.validate_answer("一般来说，需要先检查电源。", scored)
    assert not ok and "推断" in why, f"应拦截推断话术: {ok}, {why}"
    print("✓ test_validate_inference_phrase")


def test_validate_command_not_in_context():
    """命令 / 专有标识符不在上下文 → 拦截（Dell 零容错的硬要求）。"""
    scored = _ctx("Run svc_factory_reset to reset the cluster.")
    # svc_factory_reset 在上下文 → 放行
    ok, why = gen.validate_answer("Run `svc_factory_reset` now.", scored)
    assert ok, f"上下文有的命令不应拦截: {why}"
    # svc_nonexistent_zzz 不在 → 拦截
    ok, why = gen.validate_answer("Run svc_nonexistent_zzz now.", scored)
    assert not ok and "svc_nonexistent_zzz" in why, f"应拦截编造命令: {ok}, {why}"
    print("✓ test_validate_command_not_in_context")


def test_validate_version_not_in_context():
    """版本号不在上下文 → 拦截。三段 + 四段都要覆盖。

    2026-09-04：PowerStore OS 版本号是四段（4.3.0.0），旧正则只认三段，
    把 "9.9.9.9" 截成 "9.9.9"，等于四段完全没被校验 —— 这里补上。
    两段式不做：与章节号（1.2）难区分，误伤率高于收益。
    """
    scored = _ctx("PowerStore OS 3.0.0.0 is required.")
    # 四段式：上下文有 → 放行
    ok, why = gen.validate_answer("Requires 3.0.0.0 or later.", scored)
    assert ok, f"上下文有的四段版本号不应拦截: {why}"
    # 四段式：编造 → 拦截，且要报完整的四段（不是被截成三段）
    ok, why = gen.validate_answer("Requires 9.9.9.9 or later.", scored)
    assert not ok and "9.9.9.9" in why, f"应拦截编造四段版本号: {ok}, {why}"
    # 三段式：编造 → 拦截
    ok, why = gen.validate_answer("Requires 7.7.7 or later.", scored)
    assert not ok and "7.7.7" in why, f"应拦截编造三段版本号: {ok}, {why}"
    # 两段式：不做（章节号难区分）→ 放行
    ok, why = gen.validate_answer("See section 8.2 for details.", scored)
    assert ok, f"两段式是章节号，不应拦截: {why}"
    print("✓ test_validate_version_not_in_context")


def test_validate_accepts_faithful_answer():
    """忠实转述必须放行——校验不能误伤，否则可用性归零。"""
    ctx = ("To replace the battery backup unit (BBU), press the release latch "
           "and slide the module out. Run svc_factory_reset afterwards.")
    scored = _ctx(ctx)
    faithful = ("To replace the BBU, press the release latch and slide the module out.\n"
                "Afterwards run svc_factory_reset.")
    ok, why = gen.validate_answer(faithful, scored)
    assert ok, f"忠实转述被误伤: {why}"
    print("✓ test_validate_accepts_faithful_answer")


def test_validate_no_false_positive_on_plain_text():
    """普通英文句子不该被「标识符」规则误伤（只有含 _ - . / 的 token 才校验）。"""
    scored = _ctx("The cluster has two nodes. Each node runs the PowerStore OS.")
    plain = "The cluster has two nodes and each node runs the PowerStore OS."
    ok, why = gen.validate_answer(plain, scored)
    assert ok, f"普通句子被误伤: {why}"
    print("✓ test_validate_no_false_positive_on_plain_text")


def main() -> int:
    tests = [
        test_mpr_basic,
        test_mpr_doc_cap_off_equals_plain,
        test_mpr_doc_cap_collapses_pile,
        test_mpr_doc_cap_keeps_cross_variant_consensus,
        test_mpr_doc_cap_file_level_safety,
        test_mmr_dedup,
        test_rewrite_zh_to_en,
        test_rewrite_svc_norm,
        test_rewrite_long_split,
        test_rewrite_no_zh_no_change,
        test_rewrite_keeps_english_proper_nouns,
        test_hyde_fail_falls_back,
        test_hyde_success,
        test_hyde_cache_hit_skips_llm,
        test_hyde_cache_persists_across_processes,
        test_hyde_cache_lru_eviction,
        test_hyde_cache_disabled,
        test_hyde_truncate_words,
        test_hyde_prompt_is_short,
        test_retrieve_mock_no_embedder_no_llm,
        test_disable_rewrite,
        test_disable_hyde,
        test_disable_mmr,
        test_sparse_gate_pure_chinese_rejected,
        test_sparse_gate_english_token_not_in_content_rejected,
        test_sparse_gate_english_token_hit_passes,
        test_sparse_gate_low_score_rejected,
        test_query_gate_absent_english_token_rejected,
        test_query_gate_chinese_no_evidence_rejected,
        test_query_gate_passes_answerable_queries,
        test_dense_candidates_require_discriminative_hit,
        test_sparse_gate_mock_path_skips_token_check,
        test_validate_citation_out_of_range,
        test_validate_inference_phrase,
        test_validate_command_not_in_context,
        test_validate_version_not_in_context,
        test_validate_accepts_faithful_answer,
        test_validate_no_false_positive_on_plain_text,
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