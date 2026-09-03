#!/usr/bin/env python
"""切片质量验证脚本：对单个文件执行 解析 → 清洗 → 切片，打印每个 chunk 的
token 数、完整元数据与文本预览，供人工检查切片质量。

不加载任何模型，无需 Ollama 在跑，可在任意环境单独使用。

用法示例：
    python scripts/preview_chunks.py docs/a.pdf
    python scripts/preview_chunks.py docs/a.pdf -n 20 --chars 300
    python scripts/preview_chunks.py docs/a.pdf --segments 10   # 看解析后的 block
    python scripts/preview_chunks.py docs/a.pdf --tier long     # 强制按 >100 页档位切
    python scripts/preview_chunks.py docs/a.pdf --check         # 跑验收断言
    python scripts/preview_chunks.py docs/a.pdf --json out.json # 导出结构化结果

退出码：--check 模式下任一断言失败返回 1。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mini_rag.config import settings                       # noqa: E402
from mini_rag.core import tokenizer                       # noqa: E402
from mini_rag.core.parsers import (file_hash, parse_file,  # noqa: E402
                                   pdf_page_count)
from mini_rag.core.splitter import split_document, tier_for  # noqa: E402

VALID_TYPES = {"text", "table", "formula", "figure_caption", "code", "heading"}
SEP = "=" * 78


def run(path: Path, args) -> dict:
    print(f"文件      : {path}")
    print(f"大小      : {path.stat().st_size / 1024:.0f} KB")
    print(f"tokenizer : {tokenizer.backend()}")

    page_count = pdf_page_count(path)
    tier, size, overlap, parent = tier_for(page_count)
    if args.tier:                                  # 强制档位：用于对比不同策略
        forced = {"short": 0, "medium": 50, "long": 500}[args.tier]
        tier, size, overlap, parent = tier_for(forced)
        print(f"档位      : {tier}（强制，真实页数 {page_count}）")
    else:
        print(f"页数      : {page_count}")
        print(f"档位      : {tier}")
    print(f"参数      : 子块上限 {size} token | overlap {overlap} | "
          f"父块 {parent or '不生成'}")
    print(f"策略      : {settings.SPLIT_TIERS}")
    print(SEP)

    fh, segs = parse_file(path)
    print(f"[解析] {len(segs)} 个 block: {dict(Counter(s.kind for s in segs))}")
    if args.segments:
        for i, s in enumerate(segs[:args.segments]):
            print(f"  [{i}] {s.kind:14} p{s.page}-{s.page_end} "
                  f"sec={s.heading[:38]!r}")
            print(f"      {s.text[:90]!r}")
        print()

    chunks = split_document(fh, str(path), path.name, segs, page_count=page_count)
    kids = [c for c in chunks if not c.is_parent]
    pars = [c for c in chunks if c.is_parent]
    toks = [c.token_estimate for c in kids]

    print(f"[切片] 子块 {len(kids)} / 父块 {len(pars)} / 合计 {len(chunks)}")
    if toks:
        print(f"       子块 token: min={min(toks)} max={max(toks)} "
              f"均值={sum(toks) / len(toks):.0f} | "
              f"超过上限({size})的有 {sum(1 for t in toks if t > size)} 个 | "
              f"低于 50 的碎片 {sum(1 for t in toks if t < 50)} 个")
        print(f"       类型分布: {dict(Counter(c.chunk_type for c in kids))}")
    if pars:
        pt = [c.token_estimate for c in pars]
        linked = sum(1 for c in kids if c.parent_chunk_id)
        print(f"       父块 token: min={min(pt)} max={max(pt)} "
              f"均值={sum(pt) / len(pt):.0f}")
        print(f"       子块已关联父块: {linked}/{len(kids)}")
    print(SEP)

    show = kids if args.n == 0 else kids[:args.n]
    for c in show:
        print(f"#{c.chunk_index:<4} id={c.chunk_id} [{c.chunk_type}] "
              f"{c.token_estimate} tok")
        print(f"     doc_id={c.doc_id}  doc_title={c.doc_title!r}")
        print(f"     page={c.page_start}-{c.page_end}  "
              f"section={c.section_path[:58]!r}")
        print(f"     parent={c.parent_chunk_id or '-'}  lang={c.language}  "
              f"has_code={c.has_code}  has_warning={c.has_warning}  "
              f"created={c.created_at}")
        print(f"     text: {c.content[:args.chars].replace(chr(10), ' ⏎ ')}")
        print("-" * 78)

    if args.json:
        data = {
            "file": str(path), "page_count": page_count, "tier": tier,
            "chunk_size": size, "overlap": overlap, "parent_size": parent,
            "file_hash": fh,
            "chunks": [{
                "chunk_id": c.chunk_id, "chunk_index": c.chunk_index,
                "chunk_type": c.chunk_type, "token": c.token_estimate,
                "page_start": c.page_start, "page_end": c.page_end,
                "section_path": c.section_path, "doc_id": c.doc_id,
                "doc_title": c.doc_title, "parent_chunk_id": c.parent_chunk_id,
                "is_parent": c.is_parent, "language": c.language,
                "has_code": c.has_code, "has_warning": c.has_warning,
                "content": c.content,
            } for c in chunks],
        }
        Path(args.json).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已导出 {len(chunks)} 个 chunk 到 {args.json}")

    return {"chunks": chunks, "kids": kids, "parents": pars,
            "tier": tier, "size": size, "page_count": page_count}


def check(res: dict) -> int:
    """验收断言：对应需求的四条验收标准。"""
    kids, pars = res["kids"], res["parents"]
    size, tier = res["size"], res["tier"]
    fails: list[str] = []
    warns: list[str] = []

    over = [c for c in kids if c.token_estimate > size]
    if over:
        fails.append(
            f"{len(over)} 个子块超过 token 上限 {size}"
            f"（最大 {max(c.token_estimate for c in over)}）")

    bad_type = [c for c in kids if c.chunk_type not in VALID_TYPES]
    if bad_type:
        fails.append(f"{len(bad_type)} 个 chunk 的 chunk_type 非法")

    for c in kids:
        if not c.doc_id or not c.doc_title:
            fails.append(f"chunk {c.chunk_id} 缺 doc_id/doc_title")
            break
    for c in kids:
        if c.page_start is None:
            warns.append(f"chunk {c.chunk_id} 无页码（非 PDF 属正常）")
            break

    if tier == "long":
        if not pars:
            fails.append("long 档未生成父块")
        else:
            pids = {p.chunk_id for p in pars}
            orphan = [c for c in kids
                      if c.parent_chunk_id and c.parent_chunk_id not in pids]
            if orphan:
                fails.append(f"{len(orphan)} 个子块的 parent_chunk_id 指向不存在的父块")
            unlinked = [c for c in kids if not c.parent_chunk_id]
            if unlinked:
                warns.append(f"{len(unlinked)} 个子块未关联父块（孤立小节属正常）")
            too_big = [p for p in pars if p.token_estimate > 2048]
            if too_big:
                fails.append(
                    f"{len(too_big)} 个父块超过 2048 token"
                    f"（最大 {max(p.token_estimate for p in too_big)}）")
            # 父块内容必须是其所有子块内容的并集
            by_parent: dict[str, list] = {}
            for c in kids:
                if c.parent_chunk_id:
                    by_parent.setdefault(c.parent_chunk_id, []).append(c)
            bad_parent = 0
            for p in pars:
                for child in by_parent.get(p.chunk_id, []):
                    if child.content[:80] not in p.content:
                        bad_parent += 1
                        break
            if bad_parent:
                fails.append(f"{bad_parent} 个父块未包含其子块内容")

    tables = [c for c in kids if c.chunk_type == "table"]
    broken = [t for t in tables
              if not t.content.strip().startswith("|")
              or "| --- " not in t.content]
    if broken:
        warns.append(f"{len(broken)} 个表格 chunk 的 Markdown 结构不完整")

    small = [c for c in kids if c.token_estimate < 20]
    if small:
        warns.append(f"{len(small)} 个 chunk 小于 20 token（可能是碎片）")

    print(SEP)
    print("验收检查：")
    if fails:
        for f in fails:
            print(f"  [FAIL] {f}")
    else:
        print("  [PASS] 子块未超 token 上限")
        print("  [PASS] chunk_type 全部合法")
        print("  [PASS] doc_id / doc_title 齐全")
        if tier == "long":
            print("  [PASS] 父子关系完整且父块包含子块内容")
            print("  [PASS] 父块未超 2048 token")
    for w in warns:
        print(f"  [WARN] {w}")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="解析→清洗→切片 的切片质量验证脚本（不加载模型）")
    ap.add_argument("file", help="待检查的文件（pdf/md/docx/html/txt）")
    ap.add_argument("-n", type=int, default=10,
                    help="打印前 N 个 chunk，0 表示全部（默认 10）")
    ap.add_argument("--chars", type=int, default=220, help="每个 chunk 的预览字符数")
    ap.add_argument("--segments", type=int, default=0,
                    help="额外打印前 N 个解析 block")
    ap.add_argument("--tier", choices=["short", "medium", "long"],
                    help="强制按指定档位切分，用于对比策略（不改变真实页数）")
    ap.add_argument("--check", action="store_true", help="跑验收断言并以退出码反映结果")
    ap.add_argument("--json", help="导出结构化结果到 JSON 文件")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"文件不存在: {path}")
        return 1

    res = run(path, args)
    if args.check:
        return check(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
