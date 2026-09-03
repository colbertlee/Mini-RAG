"""索引编排：扫描 → diff → 解析 → 切分 → 向量化 → 双写落库。"""
from __future__ import annotations

import ctypes
import time
from pathlib import Path

from mini_rag.config import settings
from mini_rag.core import embedder, store
from mini_rag.core.parsers import (ParseError, file_hash, parse_file,
                                   pdf_page_count)
from mini_rag.core.splitter import split_document


def scan() -> list[Path]:
    """白名单扫描：按目录取修改时间倒序前 N 个，套用排除规则与大小上限。"""
    files: list[Path] = []
    for d in settings.INCLUDE_DIRS:
        root = Path(d)
        if not root.exists():
            continue
        per_dir: list[Path] = []
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if set(p.parts) & settings.EXCLUDE_DIRS:
                continue
            if p.suffix.lower() not in settings.EXT_ALLOWLIST:
                continue
            try:
                if p.stat().st_size > settings.MAX_FILE_SIZE_MB * 1024 * 1024:
                    continue
                per_dir.append(p)
            except OSError:
                continue
        per_dir.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        files.extend(per_dir[: settings.MAX_DOCS_PER_DIR])
    return files


def _estimate(files: list[Path]) -> int:
    """按实测锚点估算 chunk 数：PDF ≈ 3.0 chunk/页（805 页实测 2427 chunk）。"""
    est = 0
    for p in files:
        if p.suffix.lower() == ".pdf":
            est += int(pdf_page_count(p) * 3.0)
        else:
            try:
                est += max(1, p.stat().st_size // 700)
            except OSError:
                est += 1
    return est


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _avail_mem_gb() -> float | None:
    """可用物理内存（GB）；探测失败返回 None（调用方按最保守处理）。"""
    try:
        m = _MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullAvailPhys / (1024 ** 3)
    except Exception:
        return None


def _embed_rate() -> float:
    """按可用内存自适应 embedding 吞吐（chunks/s）。

    锚点：可用内存 ≥ 模型 + 余量（模型完全驻留）→ 理想吞吐；
    可用内存 < 模型大小（严重换页）→ 换页吞吐下限；中间线性插值。
    """
    avail = _avail_mem_gb()
    if avail is None:
        return settings.EMBED_CHUNKS_PER_SEC_SWAP  # 拿不到内存信息 → 最保守
    need = settings.EMBED_MODEL_GB + settings.MEM_HEADROOM_GB
    if avail >= need:
        return settings.EMBED_CHUNKS_PER_SEC
    if avail < settings.EMBED_MODEL_GB:
        return settings.EMBED_CHUNKS_PER_SEC_SWAP
    ratio = (avail - settings.EMBED_MODEL_GB) / (need - settings.EMBED_MODEL_GB)
    return settings.EMBED_CHUNKS_PER_SEC_SWAP + ratio * (
        settings.EMBED_CHUNKS_PER_SEC - settings.EMBED_CHUNKS_PER_SEC_SWAP)


def _err_kind(msg: str) -> str:
    """失败原因归类，用于汇总报告（取冒号前的错误类型）。"""
    head = str(msg).split(":")[0].strip()
    return head[:60] if head else "unknown"


def index(rebuild: bool = False, limit: int | None = None,
          yes: bool = False, verbose: bool = False) -> dict:
    settings.ensure_dirs()
    store._db()  # 建表
    if rebuild:
        store.rebuild_all()

    files = scan()
    if limit:
        files = files[:limit]

    est_chunks = _estimate(files)
    rate = _embed_rate()
    avail = _avail_mem_gb()
    secs = est_chunks / rate
    mem = f"{avail:.1f}GB" if avail is not None else "未知"
    print(f"待索引文件 {len(files)} 个 | 预估 chunk ≈ {est_chunks} | "
          f"可用内存 {mem} | 吞吐 ~{rate:.1f} chunks/s | "
          f"预估耗时 ≈ {secs / 60:.1f} 分钟")
    if not yes:
        a = input("继续？[y/N] ").strip().lower()
        if a != "y":
            print("已取消")
            return {"aborted": True}

    manifest = store.manifest_get()
    cur_paths = {str(p) for p in files}

    # 删除已从语料中消失的文件
    removed = 0
    for path in list(manifest):
        if path not in cur_paths:
            store.dense_delete_by_file(path)
            store.sparse_delete_by_file(path)
            store.manifest_remove(path)
            removed += 1

    new = updated = skipped = failed = 0
    errors: list[tuple[str, str]] = []
    t0 = time.time()
    for i, p in enumerate(files, 1):
        sp = str(p)
        try:
            st = p.stat()
        except OSError:
            continue
        try:
            fhash = file_hash(p)
        except OSError as e:                      # 文件被占用/已删除，不算解析失败
            errors.append((sp, f"read_error: {e}"))
            failed += 1
            continue

        rec = manifest.get(sp)
        is_update = False
        if rec and rec[3] == "done":
            if rec[0] == fhash and settings.ON_DUPLICATE == "skip":
                skipped += 1                      # doc_id + file_hash 均未变 → 跳过
                continue
            # 内容变了（或配置了 update）→ 先清旧数据再整体重建，避免残留孤儿 chunk
            store.dense_delete_by_file(sp)
            store.sparse_delete_by_file(sp)
            is_update = True

        try:
            pc = pdf_page_count(p)
            _, segments = parse_file(p, fhash)
            if not segments:
                raise ParseError("空文档")
            chunks = split_document(fhash, sp, p.name, segments, page_count=pc)
            if not chunks:
                raise ParseError("切分后无有效 chunk")
            vecs = embedder.embed_texts([c.content for c in chunks])
            store.dense_upsert(chunks, vecs)
            store.sparse_upsert(chunks)
            store.manifest_mark_done(sp, fhash, len(chunks),
                                     st.st_size, st.st_mtime, p.suffix.lower())
            if is_update:
                updated += 1
            else:
                new += 1
            if verbose:
                kinds = {}
                for c in chunks:
                    kinds[c.chunk_type] = kinds.get(c.chunk_type, 0) + 1
                print(f"  [{i}/{len(files)}] {p.name[:52]} 页数={pc} "
                      f"chunk={len(chunks)} {kinds}")
        except Exception as e:
            store.manifest_mark_error(sp, fhash, str(e))
            store.log_error(sp, str(e))
            errors.append((sp, str(e)))
            failed += 1
            if verbose:
                print(f"  [FAIL] {p.name[:52]}: {str(e)[:90]}")

        if i % 10 == 0:
            elapsed = time.time() - t0
            done_n = new + updated + skipped + failed
            eta = elapsed / max(1, done_n) * (len(files) - i)
            print(f"  进度 {i}/{len(files)} | 新增 {new} 更新 {updated} "
                  f"跳过 {skipped} 失败 {failed} | 已用 {elapsed:.0f}s "
                  f"预计剩余 {eta / 60:.1f} 分钟")

    elapsed = time.time() - t0
    kinds: dict[str, int] = {}
    for _, msg in errors:
        k = _err_kind(msg)
        kinds[k] = kinds.get(k, 0) + 1
    report = {
        "files": len(files), "new": new, "updated": updated,
        "skipped": skipped, "failed": failed, "removed": removed,
        "elapsed_s": round(elapsed, 1),
        "dense_chunks": store.dense_count(),
        "sparse_chunks": store.sparse_count(),
        "errors": [{"file": f, "error": m} for f, m in errors[:20]],
        "error_kinds": kinds,
    }

    print(f"\n完成：新增 {new} / 更新 {updated} / 跳过(未变) {skipped} / "
          f"失败 {failed} / 清理 {removed}")
    print(f"耗时 {elapsed:.0f}s | dense chunk {report['dense_chunks']} | "
          f"sparse chunk {report['sparse_chunks']}")
    if kinds:
        print("失败原因分布：")
        for k, v in sorted(kinds.items(), key=lambda x: -x[1]):
            print(f"  {v:>3} × {k}")
        print(f"（完整错误列表：{len(errors)} 条，已写入 logs/ingest_errors.jsonl）")
    return report
