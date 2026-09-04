"""重建索引后验证：dense 主路是否恢复（is_parent 字段是否已正确入库）。

跑法：python scripts/verify_dense.py
预期：dense_search 返回 >0 结果，且 is_parent 字段存在。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_rag.config import settings
from mini_rag.core import store, embedder


def main() -> int:
    settings.ensure_dirs()
    store._col()
    col = store._col()

    # 1. 字段检查
    meta = col.get(limit=5, include=["metadatas"])
    keys = set()
    for m in meta["metadatas"]:
        keys.update(m.keys())
    need = {"is_parent", "chunk_type", "doc_id", "page_start", "section_path"}
    missing = need - keys
    print(f"collection 字段数: {len(keys)}")
    print(f"必需字段: {sorted(need)}")
    if missing:
        print(f"❌ 缺失字段: {sorted(missing)}")
        return 1
    print("✓ 必需字段全部入库")

    # 2. dense 主路检查（svc_factory_reset 已知能命中）
    qv = embedder.embed_query("svc_factory_reset")
    hits = store.dense_search(qv, settings.DENSE_TOP_K)
    print(f"\ndense_search top-{settings.DENSE_TOP_K}: {len(hits)} 个结果")
    valid = [s for _, s in hits if s >= settings.DENSE_MIN]
    print(f"≥ DENSE_MIN({settings.DENSE_MIN}): {len(valid)} 个")
    for c, s in hits[:5]:
        mark = "✓" if s >= settings.DENSE_MIN else "✗"
        print(f"  {mark} dense={s:.3f} is_parent={c.is_parent} "
              f"chunk_type={c.chunk_type} :: {c.file_name[:50]}")

    if not hits:
        print("❌ dense_search 仍返回 0，主路未恢复")
        return 1
    if valid:
        print("\n✓ dense 主路已恢复（有 ≥ 阈值的候选）")
        return 0
    print("\n⚠️ dense 有结果但都低于阈值（可能是 embedding 语义问题，非字段 bug）")
    return 0


if __name__ == "__main__":
    sys.exit(main())