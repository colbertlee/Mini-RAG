"""CLI 入口：python -m mini_rag.cli <command>"""
from __future__ import annotations

import argparse
import sys
import time


def cmd_index(args) -> None:
    from mini_rag.config import settings
    from mini_rag.core import pipeline
    if args.update:
        settings.ON_DUPLICATE = "update"
    pipeline.index(rebuild=args.rebuild, limit=args.limit, yes=args.yes,
                   verbose=args.verbose)


def cmd_preview(args) -> None:
    """对单个文件跑 解析→清洗→切片，打印 chunk 明细供人工检查。

    不加载任何模型，可在没有 Ollama 的环境里验证切片质量。
    """
    from mini_rag.core import pipeline as _p  # noqa: F401  确保配置已初始化
    from mini_rag.core.parsers import file_hash, parse_file, pdf_page_count
    from mini_rag.core.splitter import split_document, tier_for
    from mini_rag.core import tokenizer
    from pathlib import Path

    p = Path(args.file)
    if not p.exists():
        print(f"文件不存在: {p}")
        return
    print(f"文件: {p}")
    print(f"tokenizer: {tokenizer.backend()}")
    pc = pdf_page_count(p)
    tier, size, overlap, parent = tier_for(pc)
    print(f"页数: {pc} | 档位: {tier} | 子块上限 {size} token | "
          f"overlap {overlap} | 父块 {parent or '无'}")

    fh, segs = parse_file(p)
    from collections import Counter
    print(f"解析出 {len(segs)} 个 block: {dict(Counter(s.kind for s in segs))}")
    if args.segments:
        for i, s in enumerate(segs[:args.segments]):
            print(f"  [{i}] {s.kind} p{s.page}-{s.page_end} "
                  f"sec={s.heading[:40]!r} :: {s.text[:80]!r}")

    chunks = split_document(fh, str(p), p.name, segs, page_count=pc)
    kids = [c for c in chunks if not c.is_parent]
    pars = [c for c in chunks if c.is_parent]
    print(f"\n切片结果: 子块 {len(kids)} / 父块 {len(pars)} / 合计 {len(chunks)}")
    toks = [c.token_estimate for c in kids]
    if toks:
        print(f"子块 token: min={min(toks)} max={max(toks)} "
              f"均值={sum(toks) / len(toks):.0f} | "
              f"超过上限({size})的有 {sum(1 for t in toks if t > size)} 个")
        print(f"chunk 类型: {dict(Counter(c.chunk_type for c in kids))}")
    if pars:
        pt = [c.token_estimate for c in pars]
        print(f"父块 token: min={min(pt)} max={max(pt)} "
              f"均值={sum(pt) / len(pt):.0f} | "
              f"子块已关联父块 {sum(1 for c in kids if c.parent_chunk_id)}/{len(kids)}")

    show = kids[:args.n] if args.n else kids
    print(f"\n{'=' * 78}")
    for c in show:
        print(f"#{c.chunk_index} id={c.chunk_id} [{c.chunk_type}] "
              f"{c.token_estimate} tok")
        print(f"   doc_id={c.doc_id} doc_title={c.doc_title!r}")
        print(f"   page={c.page_start}-{c.page_end} "
              f"section={c.section_path[:60]!r}")
        print(f"   parent={c.parent_chunk_id or '-'} lang={c.language} "
              f"code={c.has_code} warn={c.has_warning}")
        prev = c.content[:args.chars].replace("\n", " ⏎ ")
        print(f"   text: {prev}")
        print("-" * 78)


def cmd_status(args) -> None:
    from mini_rag.config import settings
    from mini_rag.core import store
    man = store.manifest_get()
    done = sum(1 for r in man.values() if r[3] == "done")
    err = sum(1 for r in man.values() if r[3] == "error")
    from mini_rag.core import tokenizer
    print("== Mini-RAG 状态 ==")
    print(f"生成模型   : {settings.LLM_MODEL}")
    print(f"Embedding  : {settings.EMBED_PROVIDER}/{settings.EMBED_MODEL}")
    print(f"Tokenizer  : {tokenizer.backend()}")
    print(f"切分档位   : " + " | ".join(
        f"≤{t[0]}页→{t[1]}tok/ov{t[2]}" + (f"/父{t[3]}" if t[3] else "")
        for t in settings.SPLIT_TIERS))
    print(f"向量 chunk : {store.dense_count()}")
    print(f"稀疏 chunk : {store.sparse_count()}")
    print(f"清单       : {len(man)} 文件（成功 {done} / 失败 {err}）")
    print(f"dense 阈值 : {settings.DENSE_MIN}")
    print(f"语料目录   :")
    for d in settings.INCLUDE_DIRS:
        print(f"  - {d}")


def cmd_ask(args) -> None:
    from mini_rag.core import retriever, generator

    t0 = time.time()
    scored, reason = retriever.retrieve(args.question)
    t_ret = time.time() - t0

    if reason:
        print("知识库中未找到相关信息")
        if args.debug:
            print(f"（拒答原因: {reason}）")
        return

    if args.top_n:
        scored = scored[:args.top_n]

    latency = {"retrieve": t_ret}
    t0 = time.time()
    resp = generator.generate(args.question, scored, latency)
    latency["llm"] = time.time() - t0

    print(f"检索: {len(resp.retrieved)} 片段 | 检索 {latency['retrieve']:.1f}s "
          f"| 生成 {latency['llm']:.1f}s")
    print()
    print(resp.answer)
    print()
    if resp.citations:
        print("【参考来源】:")
        for i, c in enumerate(resp.citations, 1):
            pg = f" | 第{c.page_number}页" if c.page_number else ""
            label = f"{c.file_name}{pg}"
            # OSC 8：终端里 Ctrl+Click 打开正确 URI，但显示中文文件名。
            # 非 tty（管道/重定向）降级为纯文本，避免转义序列污染输出。
            if sys.stdout.isatty():
                print(f"[{i}] {generator.osc8_link(c.file_uri, label)} | {c.file_uri}")
            else:
                print(f"[{i}] {label} | {c.file_uri}")
    if args.debug and resp.retrieved:
        print()
        for s in resp.retrieved:
            print(f"  -- {s.chunk.chunk_id} dense={s.dense_score:.3f} "
                  f"sparse={s.sparse_score:.3f} rrf={s.rrf_score:.4f} "
                  f"by={s.matched_by} :: {s.chunk.content[:80]!r}")


def cmd_chat(args) -> None:
    from mini_rag.config import settings
    from mini_rag.core import retriever, generator
    print(f"Mini-RAG 交互模式（模型 {settings.LLM_MODEL}），输入 :quit 退出")
    while True:
        try:
            q = input("\n>>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q in (":quit", ":exit", ":q"):
            break
        if q == ":status":
            cmd_status(args)
            continue
        if q.startswith(":threshold"):
            try:
                settings.DENSE_MIN = float(q.split()[-1])
            except (ValueError, IndexError):
                pass
            print(f"dense_min = {settings.DENSE_MIN}")
            continue
        if q.startswith(":topn"):
            try:
                settings.FINAL_TOP_N = int(q.split()[-1])
            except (ValueError, IndexError):
                pass
            print(f"final_top_n = {settings.FINAL_TOP_N}")
            continue

        scored, reason = retriever.retrieve(q)
        if reason:
            print("知识库中未找到相关信息")
            continue
        resp = generator.generate(q, scored, {})
        print()
        print(resp.answer)
        print()


def cmd_purge(args) -> None:
    from mini_rag.config import settings
    from mini_rag.core import store
    if not args.yes:
        a = input("将清空 data/ 与 logs/ 下的索引。确认？[y/N] ").strip().lower()
        if a != "y":
            print("已取消")
            return
    store.rebuild_all()
    import shutil
    shutil.rmtree(settings.LOG_DIR, ignore_errors=True)
    print("已清空索引。")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mini_rag", description="本地轻量化 RAG 检索")
    sub = p.add_subparsers(dest="cmd")

    pi = sub.add_parser("index", help="构建/增量索引")
    pi.add_argument("--rebuild", action="store_true", help="清空后全量重建")
    pi.add_argument("--limit", type=int, help="最多处理 N 个文件")
    pi.add_argument("--yes", "-y", action="store_true", help="跳过确认")
    pi.add_argument("--verbose", "-v", action="store_true",
                    help="逐文件输出页数/chunk 数/类型分布")
    pi.add_argument("--update", action="store_true",
                    help="已入库且 hash 未变的文件也强制重建（默认跳过）")

    pv = sub.add_parser("preview",
                        help="单文件 解析→清洗→切片 预览（不加载模型）")
    pv.add_argument("file", help="待检查的 PDF / md / docx 等文件")
    pv.add_argument("-n", type=int, default=10, help="打印前 N 个 chunk（0=全部）")
    pv.add_argument("--chars", type=int, default=220, help="文本预览字符数")
    pv.add_argument("--segments", type=int, default=0,
                    help="额外打印前 N 个解析 block")

    pa = sub.add_parser("ask", help="单次问答")
    pa.add_argument("question")
    pa.add_argument("--debug", action="store_true", help="打印召回明细")
    pa.add_argument("--top-n", type=int, help="进入 LLM 的片段数")

    sub.add_parser("chat", help="交互式问答")
    sub.add_parser("status", help="状态总览")

    pp = sub.add_parser("purge", help="清空索引")
    pp.add_argument("--yes", "-y", action="store_true")

    args = p.parse_args(argv)
    if args.cmd is None:
        p.print_help()
        return 1
    {"index": cmd_index, "ask": cmd_ask, "chat": cmd_chat,
     "status": cmd_status, "purge": cmd_purge,
     "preview": cmd_preview}[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
