"""检索效果评估脚本（离线 + 在线双模式）。

用法：
    python scripts/eval_retrieval.py baseline   # 改造前基线
    python scripts/eval_retrieval.py optimized  # 改造后
    python scripts/eval_retrieval.py both       # 都跑

离线模式（默认，不需要 Ollama）：
    用 ground_truth_doc 中随机 chunk 的真实向量作 query，向量在 Chroma 里
    一次性导出 30 个 mock query 向量。验证"融合策略 + 阈值守门"的逻辑正确性，
    不评估语义匹配（语义匹配需要真 embedding）。

在线模式（--online）：
    调 embedder.embed_query 真实向量化，评估端到端语义命中。
    需要 Ollama 在跑、可用内存 ≥ 4GB。

输出：_build/eval_<tag>_report.json + 在 stdout 打汇总表。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mini_rag.config import settings  # noqa: E402
from mini_rag.core import retriever, store  # noqa: E402

EVAL_CORPUS = ROOT / "_build" / "eval_corpus.json"
REPORT_DIR = ROOT / "_build"

TOP_KS = (1, 3, 10)


def load_corpus() -> list[dict]:
    return json.loads(EVAL_CORPUS.read_text(encoding="utf-8"))["cases"]


def _hit(case: dict, retrieved: list[dict]) -> dict:
    """判定 top-k 是否命中 ground_truth_file（token 级宽松口径）。

    真实 file_name 与 ground_truth_file 通常不是完全相等（如前者是
    "PowerStore_ How to use svc_factory_reset _ Dell US.pdf"，后者写 "svc_factory_reset"）。
    解法：ground_truth_file 列表里任一项若出现在 file_name（lower 子串）即命中。
    """
    gts = case.get("ground_truth_file", [])
    gts = gts or case.get("ground_truth_doc", [])  # 兼容 v1 字段名
    res = {f"hit@{k}": 0 for k in TOP_KS}
    if not gts:
        return res
    gts_lower = tuple(g.lower() for g in gts)
    for i, sc in enumerate(retrieved):
        fn = (sc["chunk"].get("file_name") or "").lower()
        if any(g in fn for g in gts_lower):
            for k in TOP_KS:
                if i < k:
                    res[f"hit@{k}"] = 1
    return res


def _doc_match(file_name: str, prefixes: tuple) -> bool:
    return any(file_name.lower().startswith(p) for p in prefixes)


def _summarize(rows: list[dict]) -> dict:
    """聚合：按 category、difficulty、overall 三维度算 hit-rate + 平均延迟。"""
    by_cat: dict[str, list] = defaultdict(list)
    by_diff: dict[str, list] = defaultdict(list)
    overall: list = []
    for r in rows:
        by_cat[r["category"]].append(r)
        by_diff[r["difficulty"]].append(r)
        overall.append(r)

    def _agg(rows_in: list[dict]) -> dict:
        n = len(rows_in)
        if not n:
            return {}
        agg = {"n": n}
        for k in TOP_KS:
            agg[f"hit@{k}"] = round(sum(r["hits"][f"hit@{k}"] for r in rows_in) / n, 4)
        agg["avg_latency_ms"] = round(sum(r["latency_ms"] for r in rows_in) / n, 1)
        agg["avg_dense_top1"] = round(
            sum(r.get("dense_top1", 0.0) for r in rows_in) / n, 3)
        return agg

    return {
        "overall": _agg(overall),
        "by_category": {k: _agg(v) for k, v in sorted(by_cat.items())},
        "by_difficulty": {k: _agg(v) for k, v in sorted(by_diff.items())},
        "cases": rows,
    }


def _print_table(summary: dict, tag: str) -> None:
    print(f"\n{'=' * 64}")
    print(f"[{tag}] 检索评估汇总")
    print(f"{'=' * 64}")
    print(f"{'维度':<16} {'n':>4} {'hit@1':>8} {'hit@3':>8} {'hit@10':>8} "
          f"{'avg_ms':>8}")
    print("-" * 64)

    def _line(rows_in: dict, label: str) -> None:
        if not rows_in:
            return
        print(f"{label:<16} {rows_in['n']:>4} "
              f"{rows_in['hit@1'] * 100:>7.1f}% "
              f"{rows_in['hit@3'] * 100:>7.1f}% "
              f"{rows_in['hit@10'] * 100:>7.1f}% "
              f"{rows_in['avg_latency_ms']:>7.1f}")

    _line(summary["overall"], "Overall")
    for k, v in summary["by_category"].items():
        _line(v, f"  cat={k}")
    for k, v in summary["by_difficulty"].items():
        _line(v, f"  diff={k}")


def _run_mock(retriever_fn, tag: str) -> None:
    """离线 mock：用 ground_truth_doc 的真实 chunk 向量模拟 query。

    不评估语义命中（mock 与真实 query embedding 无关），仅验证融合策略 +
    MMR 的逻辑对 top-k 结果的影响。

    评估口径改为：mock 向量与 ground_truth_doc 内的 chunk 高度相似，
    所以负例用「随机挑非 ground_truth_doc 的 chunk 向量」来模拟。
    """
    cases = load_corpus()
    rng = random.Random(42)

    # 拉所有 dense 向量（一次性，便于离线选 mock）
    col = store._col()
    raw = col.get(include=["metadatas", "embeddings", "documents"])
    ids, docs_text, metas, embs = (raw["ids"], raw["documents"],
                                    raw["metadatas"], raw["embeddings"])
    by_doc: dict[str, list[int]] = defaultdict(list)
    for i, m in enumerate(metas):
        by_doc[m.get("file_name", "")].append(i)
    doc_names = list(by_doc.keys())

    print(f"Chroma 中现有 {len(ids)} chunk，构造 30 条 mock query ...")

    rows = []
    for case in cases:
        gt_files = case.get("ground_truth_file", [])
        gt_files = gt_files or case.get("ground_truth_doc", [])
        if gt_files:
            # 正例：找包含 ground_truth_file 中任一关键词的文件
            cand_doc = None
            for fn in doc_names:
                if any(g.lower() in fn.lower() for g in gt_files):
                    cand_doc = fn
                    break
            if cand_doc is None:
                cand_doc = rng.choice(doc_names)
            idx = rng.choice(by_doc[cand_doc])
        else:
            # 负例：从 ground_truth_file 不命中的文件里挑
            while True:
                idx = rng.randrange(len(ids))
                fn = metas[idx].get("file_name", "")
                if not any(g.lower() in fn.lower() for g in gt_files):
                    break

        # 跑 retriever（注入 mock 向量）
        mock_vec = embs[idx]
        t0 = time.time()
        try:
            # 直接调内部函数注入向量，避免 embed_query
            result = retriever_fn(case["question"], mock_vec=mock_vec)
        except Exception as e:
            print(f"  [{case['id']}] retriever 异常: {e}")
            continue
        dt = (time.time() - t0) * 1000

        scored_list, reason = result
        retrieved_dump = [{"chunk": {
            "file_name": s.chunk.file_name, "chunk_id": s.chunk.chunk_id,
            "content": s.chunk.content}}
            for s in scored_list]
        hits = _hit(case, retrieved_dump)
        dense_top1 = scored_list[0].dense_score if scored_list else 0.0
        rows.append({
            "id": case["id"], "category": case["category"],
            "difficulty": case["difficulty"], "reason": reason,
            "hits": hits, "dense_top1": dense_top1,
            "latency_ms": round(dt, 1), "n_retrieved": len(scored_list),
        })

    summary = _summarize(rows)
    _print_table(summary, tag)
    out = REPORT_DIR / f"eval_{tag}_report.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n明细已写入: {out}")


def _run_online(retriever_fn, tag: str, limit_n: int | None = None,
                only: str | None = None, out_path: str | None = None) -> None:
    """在线语义评估：调真实 embed_query。"""
    from mini_rag.core import embedder
    cases = load_corpus()
    if only:
        # 只跑指定 case（逗号分隔），用于针对性复测失败 case
        wanted = {s.strip().upper() for s in only.split(",") if s.strip()}
        cases = [c for c in cases if c["id"].upper() in wanted]
        print(f"[{tag}] 只跑 {len(cases)} 条: {', '.join(c['id'] for c in cases)}")
    elif limit_n:
        cases = cases[:limit_n]
        print(f"[{tag}] 跑前 {limit_n} 条 query ...")
    else:
        print(f"[{tag}] 跑 {len(cases)} 条 query 真实 embedding ...")
    rows = []
    for case in cases:
        t0 = time.time()
        try:
            scored_list, reason = retriever_fn(case["question"])
        except Exception as e:
            print(f"  [{case['id']}] retriever 异常: {e}")
            rows.append({
                "id": case["id"], "category": case["category"],
                "difficulty": case["difficulty"], "reason": str(e),
                "hits": {f"hit@{k}": 0 for k in TOP_KS},
                "dense_top1": 0.0, "latency_ms": 0.0, "n_retrieved": 0,
            })
            continue
        dt = (time.time() - t0) * 1000
        retrieved_dump = [{"chunk": {
            "file_name": s.chunk.file_name, "chunk_id": s.chunk.chunk_id,
            "content": s.chunk.content}}
            for s in scored_list]
        hits = _hit(case, retrieved_dump)
        dense_top1 = scored_list[0].dense_score if scored_list else 0.0
        rows.append({
            "id": case["id"], "category": case["category"],
            "difficulty": case["difficulty"], "reason": reason,
            "hits": hits, "dense_top1": dense_top1,
            "latency_ms": round(dt, 1), "n_retrieved": len(scored_list),
        })
        print(f"  [{case['id']}] reason={reason or 'OK':<14} "
              f"top1={dense_top1:.3f} n={len(scored_list)} "
              f"hit@3={'Y' if hits['hit@3'] else 'N'} "
              f"{dt:.0f}ms :: {case['question'][:40]}")

    summary = _summarize(rows)
    _print_table(summary, tag)
    from pathlib import Path as _P
    out = _P(out_path) if out_path else REPORT_DIR / f"eval_{tag}_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n明细已写入: {out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("which", choices=["baseline", "optimized", "both"])
    ap.add_argument("--online", action="store_true",
                    help="真实 embedding 评估（需 Ollama）")
    ap.add_argument("--off-rewrite", action="store_true",
                    help="关闭 Query Rewrite（强制 baseline 行为）")
    ap.add_argument("--off-hyde", action="store_true",
                    help="关闭 HyDE（强制 baseline 行为）")
    ap.add_argument("--off-mmr", action="store_true",
                    help="关闭 MMR（强制 baseline 行为）")
    ap.add_argument("--limit", type=int,
                    help="只跑前 N 条 query（适合 HyDE 长任务分批跑）")
    ap.add_argument("--only", metavar="IDS",
                    help="只跑指定 case（逗号分隔，如 P15,P21,P25）—— 针对性复测失败 case")
    ap.add_argument("--out", metavar="PATH",
                    help="输出 JSON 路径（默认 _build/eval_<tag>_report.json）")
    args = ap.parse_args()

    # baseline = 关掉三层改造，等价 v0.1.0 行为
    if args.off_rewrite:
        settings.QUERY_REWRITE_ENABLED = False
    if args.off_hyde:
        settings.HYDE_ENABLED = False
    if args.off_mmr:
        settings.MMR_ENABLED = False
    active = [n for n, off in [("QUERY_REWRITE", not args.off_rewrite),
                                 ("HYDE", not args.off_hyde),
                                 ("MMR", not args.off_mmr)] if off]
    print(f"评估模式: {args.which} | online={args.online} | 开启层: {active or '无（baseline）'}")

    settings.ensure_dirs()
    # 跑前初始化 Chroma/SQLite 连接
    store._col()

    if args.which in ("baseline", "both"):
        from mini_rag.core import retriever as r
        fn = r.retrieve
        if args.online:
            _run_online(fn, "baseline", limit_n=args.limit, only=args.only,
                        out_path=args.out)
        else:
            # baseline：直接调内部函数注入 mock 向量
            def baseline_fn(q, mock_vec):
                # 改 retriever 不便，给 baseline 走单独路径
                dense_hits = store.dense_search(mock_vec, settings.DENSE_TOP_K)
                dense_valid = [(c, s) for c, s in dense_hits
                               if s >= settings.DENSE_MIN]
                if dense_valid:
                    ranked = sorted(dense_valid, key=lambda x: -x[1])[:settings.FINAL_TOP_N]
                    from mini_rag.core.schema import ScoredChunk
                    return [ScoredChunk(chunk=c, dense_score=s, sparse_score=0.0,
                                        rrf_score=s, matched_by="dense")
                            for c, s in ranked], ""
                terms = store.query_terms(q)
                sparse_hits = store.sparse_search(terms, settings.SPARSE_TOP_K)
                if sparse_hits:
                    from mini_rag.core.schema import ScoredChunk
                    return [ScoredChunk(chunk=c, dense_score=0.0, sparse_score=s,
                                        rrf_score=0.0, matched_by="sparse")
                            for c, s in sparse_hits[:settings.FINAL_TOP_N]], ""
                return [], "below_threshold"
            _run_mock(baseline_fn, "baseline")

    if args.which in ("optimized", "both"):
        from mini_rag.core import retriever as r
        fn = r.retrieve
        if args.online:
            _run_online(fn, "optimized", limit_n=args.limit, only=args.only,
                        out_path=args.out)
        else:
            # optimized 也走 mock，验证改造后逻辑
            def optimized_fn(q, mock_vec):
                return r.retrieve(q, mock_vec=mock_vec)
            _run_mock(optimized_fn, "optimized")

    return 0


if __name__ == "__main__":
    sys.exit(main())