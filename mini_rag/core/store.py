"""持久化层：dense(ChromaDB cosine) + sparse(SQLite FTS5) + manifest，合并一处。

父块（is_parent=True）只入 Chroma 供按 id 取回作 LLM 上下文：
不进 FTS5，也不参与 dense 检索（where 过滤），否则与子块语义重叠会稀释召回。

向量库切换（Qdrant / Milvus）见文件末尾的 VectorStore 预留接口。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time

import chromadb
import jieba

from mini_rag.config import settings
from mini_rag.core.schema import Chunk

# ================= dense (ChromaDB) =================
_client: chromadb.PersistentClient | None = None
_collection = None


def _col():
    global _client, _collection
    # 以 _collection 为准判断是否要初始化（不能以 _client 为准）：
    # rebuild_all() 会先实例化 _client 再 delete_collection，之后 _collection=None。
    # 若这里只看 _client is None，则 _collection 保持 None，dense_upsert 会报
    # "'NoneType' object has no attribute 'upsert'"（2026-09-04 重建踩坑）。
    if _collection is None:
        settings.ensure_dirs()
        if _client is None:
            _client = chromadb.PersistentClient(path=str(settings.VECTOR_DIR))
        _collection = _client.get_or_create_collection(
            "mini_rag", metadata={"hnsw:space": "cosine"})
    return _collection


def _meta(c: Chunk) -> dict:
    """ChromaDB metadata。注意：不接受 None，页码无值必须写 -1。"""
    return {
        "file_path": c.file_path,
        "file_name": c.file_name,
        "page_start": c.page_start if c.page_start is not None else -1,
        "page_end": c.page_end if c.page_end is not None else -1,
        "doc_id": c.doc_id,
        "doc_title": c.doc_title,
        "section_path": c.section_path or "",
        "chunk_type": c.chunk_type,
        "chunk_index": c.chunk_index,
        "parent_chunk_id": c.parent_chunk_id or "",
        "is_parent": c.is_parent,
        "language": c.language,
        "created_at": c.created_at,
        "has_code": c.has_code,
        "has_warning": c.has_warning,
        "file_hash": c.file_hash,
        "token_estimate": c.token_estimate,
    }


def _chunk_from_meta(cid: str, doc: str, meta: dict) -> Chunk:
    ps = meta.get("page_start", -1)
    pe = meta.get("page_end", -1)
    return Chunk(
        chunk_id=cid, content=doc,
        file_path=meta.get("file_path", ""),
        file_name=meta.get("file_name", ""),
        page_start=ps if ps != -1 else None,
        page_end=pe if pe != -1 else None,
        chunk_index=meta.get("chunk_index", 0),
        doc_id=meta.get("doc_id", ""),
        doc_title=meta.get("doc_title", ""),
        section_path=meta.get("section_path", ""),
        chunk_type=meta.get("chunk_type", "text"),
        parent_chunk_id=meta.get("parent_chunk_id", ""),
        is_parent=meta.get("is_parent", False),
        language=meta.get("language", "en"),
        created_at=meta.get("created_at", ""),
        has_code=meta.get("has_code", False),
        has_warning=meta.get("has_warning", False),
        file_hash=meta.get("file_hash", ""),
        token_estimate=meta.get("token_estimate", 0),
    )


def dense_upsert(chunks: list[Chunk], vectors: list[list[float]]) -> None:
    _col().upsert(
        ids=[c.chunk_id for c in chunks],
        documents=[c.content for c in chunks],
        embeddings=vectors,
        metadatas=[_meta(c) for c in chunks],
    )


def dense_search(vec: list[float], top_k: int,
                 where: dict | None = None) -> list[tuple[Chunk, float]]:
    """dense 检索。默认排除父块；where 可追加 metadata 过滤（doc_id/chunk_type 等）。"""
    col = _col()
    cond = {"is_parent": False}
    if where:
        cond.update(where)
    res = col.query(query_embeddings=[vec], n_results=top_k, where=cond,
                    include=["documents", "metadatas", "distances"])
    out: list[tuple[Chunk, float]] = []
    for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0],
                                    res["metadatas"][0], res["distances"][0]):
        out.append((_chunk_from_meta(cid, doc, meta), 1.0 - dist))
    return out


def dense_get(ids: list[str]) -> dict[str, Chunk]:
    """按 id 批量取回（命中子块后取父块上下文用）。"""
    if not ids:
        return {}
    res = _col().get(ids=ids, include=["documents", "metadatas"])
    return {cid: _chunk_from_meta(cid, doc, meta)
            for cid, doc, meta in zip(res["ids"], res["documents"],
                                      res["metadatas"])}


def dense_delete_by_file(file_path: str) -> None:
    _col().delete(where={"file_path": file_path})


def dense_count() -> int:
    return _col().count()


# ================= sparse (SQLite FTS5) + manifest =================
_conn: sqlite3.Connection | None = None

_CHUNK_COLS = (
    "chunk_id, content, file_path, file_name, page_start, page_end,"
    " section_path, chunk_index, doc_id, doc_title, chunk_type,"
    " parent_chunk_id, is_parent, language, created_at,"
    " has_code, has_warning, file_hash, token_estimate"
)

# 旧列名 → 新列名：旧库迁移优先 rename（保留数据），而非空着加新列。
_RENAME_COLS = {
    "page_number": "page_start",
    "heading_path": "section_path",
}

# 旧库迁移用：新增列的类型定义
_EXTRA_COLS = {
    "page_start": "INTEGER", "page_end": "INTEGER", "section_path": "TEXT",
    "doc_id": "TEXT", "doc_title": "TEXT", "chunk_type": "TEXT",
    "parent_chunk_id": "TEXT", "is_parent": "INTEGER", "language": "TEXT",
    "created_at": "TEXT", "token_estimate": "INTEGER",
}


def _ensure_columns(db: sqlite3.Connection) -> None:
    """给旧版 index.db 补列/改列名，避免必须重建索引才能升级。

    旧 schema 用 page_number/heading_path，新 schema 用 page_start/section_path：
    先按 _RENAME_COLS 改名保留数据，再补 _EXTRA_COLS 里缺失的列。
    """
    cols = {r[1] for r in db.execute("PRAGMA table_info(chunks)")}
    for old, new in _RENAME_COLS.items():
        if old in cols and new not in cols:
            db.execute(f"ALTER TABLE chunks RENAME COLUMN {old} TO {new}")
            cols.discard(old)
            cols.add(new)
    for name, typ in _EXTRA_COLS.items():
        if name not in cols:
            db.execute(f"ALTER TABLE chunks ADD COLUMN {name} {typ}")
    db.commit()


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        settings.ensure_dirs()
        _conn = sqlite3.connect(str(settings.INDEX_DB))
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS manifest ("
            "file_path TEXT PRIMARY KEY, file_hash TEXT, size INTEGER,"
            "mtime REAL, ext TEXT, chunk_count INTEGER DEFAULT 0,"
            "status TEXT, error_msg TEXT, indexed_at REAL)")
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks ("
            "chunk_id TEXT PRIMARY KEY, content TEXT, file_path TEXT,"
            "file_name TEXT, page_start INTEGER, page_end INTEGER,"
            " section_path TEXT, chunk_index INTEGER, doc_id TEXT,"
            " doc_title TEXT, chunk_type TEXT, parent_chunk_id TEXT,"
            " is_parent INTEGER, language TEXT, created_at TEXT,"
            " has_code INTEGER, has_warning INTEGER, file_hash TEXT,"
            " token_estimate INTEGER)")
        _conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
            "tokens, chunk_id UNINDEXED)")
        _conn.commit()
        _ensure_columns(_conn)
    return _conn


def _tokenize(text: str) -> str:
    """jieba 中文分词 + 英文/命令 token 双写，保证 svc_journal 这类命令可精确命中。"""
    cjk = " ".join(jieba.cut(text))
    eng = " ".join(re.findall(r"[A-Za-z0-9_\-\.]{2,}", text.lower()))
    return (cjk + " " + eng).strip()


def query_terms(query: str) -> list[str]:
    """提取查询的内容词（长度 ≥2 且非停用词 + 英文/命令 token），去重保序。"""
    terms = [w.strip() for w in jieba.cut(query)
             if len(w.strip()) >= 2 and w.strip() not in settings.STOPWORDS]
    terms += re.findall(r"[A-Za-z0-9_\-\.]{2,}", query.lower())
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def sparse_upsert(chunks: list[Chunk]) -> None:
    """写入 chunks 表与 FTS。父块跳过：内容是子块并集，进 FTS 只会重复召回。"""
    db = _db()
    ph = ",".join("?" * 19)
    for c in chunks:
        if c.is_parent:
            continue
        db.execute(
            "INSERT OR REPLACE INTO chunks ("
            "chunk_id, content, file_path, file_name, page_start, page_end,"
            " section_path, chunk_index, doc_id, doc_title, chunk_type,"
            " parent_chunk_id, is_parent, language, created_at,"
            " has_code, has_warning, file_hash, token_estimate) "
            f"VALUES ({ph})",
            (c.chunk_id, c.content, c.file_path, c.file_name,
             c.page_start if c.page_start is not None else -1,
             c.page_end if c.page_end is not None else -1,
             c.section_path or "", c.chunk_index, c.doc_id, c.doc_title,
             c.chunk_type, c.parent_chunk_id, 1 if c.is_parent else 0,
             c.language, c.created_at, 1 if c.has_code else 0,
             1 if c.has_warning else 0, c.file_hash, c.token_estimate))
        db.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (c.chunk_id,))
        db.execute("INSERT INTO chunks_fts(tokens, chunk_id) VALUES (?,?)",
                   (_tokenize(c.content), c.chunk_id))
    db.commit()


def sparse_search(terms: list[str], top_k: int) -> list[tuple[Chunk, float]]:
    if not terms:
        return []
    db = _db()
    match = " OR ".join(f'"{t.replace(chr(34), "")}"' for t in terms)
    rows = db.execute(
        "SELECT chunk_id, bm25(chunks_fts) FROM chunks_fts "
        "WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
        (match, top_k)).fetchall()
    out: list[tuple[Chunk, float]] = []
    for cid, score in rows:
        r = db.execute(
            f"SELECT {_CHUNK_COLS} FROM chunks WHERE chunk_id=?", (cid,)).fetchone()
        if r is None:
            continue
        ps, pe = r[4], r[5]
        out.append((Chunk(
            chunk_id=r[0], content=r[1], file_path=r[2], file_name=r[3],
            page_start=ps if ps != -1 else None,
            page_end=pe if pe != -1 else None,
            section_path=r[6] or "", chunk_index=r[7], doc_id=r[8] or "",
            doc_title=r[9] or "", chunk_type=r[10] or "text",
            parent_chunk_id=r[11] or "", is_parent=bool(r[12]),
            language=r[13] or "en", created_at=r[14] or "",
            has_code=bool(r[15]), has_warning=bool(r[16]), file_hash=r[17],
            token_estimate=r[18] or 0), -score))   # 取反：越大越好（仅用于调试）
    return out


def sparse_count() -> int:
    return _db().execute("SELECT count(*) FROM chunks").fetchone()[0]


def sparse_delete_by_file(file_path: str) -> None:
    db = _db()
    cids = [r[0] for r in db.execute(
        "SELECT chunk_id FROM chunks WHERE file_path=?", (file_path,))]
    for cid in cids:
        db.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (cid,))
    db.execute("DELETE FROM chunks WHERE file_path=?", (file_path,))
    db.commit()


# ---- manifest ----
def manifest_get() -> dict[str, tuple]:
    rows = _db().execute(
        "SELECT file_path, file_hash, size, mtime, status FROM manifest").fetchall()
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


def manifest_get_hash(path: str) -> str:
    r = _db().execute(
        "SELECT file_hash FROM manifest WHERE file_path=?", (path,)).fetchone()
    return r[0] if r else ""


def manifest_mark_done(path: str, file_hash: str, chunk_count: int,
                       size: int, mtime: float, ext: str) -> None:
    _db().execute(
        "INSERT OR REPLACE INTO manifest "
        "(file_path, file_hash, size, mtime, ext, chunk_count, status,"
        " error_msg, indexed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (path, file_hash, size, mtime, ext, chunk_count, "done", None, time.time()))
    _db().commit()


def manifest_mark_error(path: str, file_hash: str, err: str) -> None:
    _db().execute(
        "INSERT OR REPLACE INTO manifest "
        "(file_path, file_hash, size, mtime, ext, chunk_count, status,"
        " error_msg, indexed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (path, file_hash, 0, 0, "", 0, "error", err, time.time()))
    _db().commit()


def manifest_remove(path: str) -> None:
    _db().execute("DELETE FROM manifest WHERE file_path=?", (path,))
    _db().commit()


def log_error(path: str, msg: str) -> None:
    settings.ensure_dirs()
    with open(settings.ERROR_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "file": path, "error": msg},
                           ensure_ascii=False) + "\n")


def rebuild_all() -> None:
    """清空所有索引（Chroma + FTS + chunks + manifest）。"""
    global _client, _collection
    # 关键：CLI 每次 `index --rebuild` 都是新进程，_client 此时为 None。
    # 必须先实例化 client 再 delete_collection，否则旧 collection 残留、
    # 旧向量污染 dense 索引（曾导致 71 个过期 chunk 混入语料）。
    if _client is None:
        settings.ensure_dirs()
        _client = chromadb.PersistentClient(path=str(settings.VECTOR_DIR))
    try:
        _client.delete_collection("mini_rag")
    except Exception:
        pass
    _collection = None
    db = _db()
    db.execute("DELETE FROM chunks_fts")
    db.execute("DELETE FROM chunks")
    db.execute("DELETE FROM manifest")
    db.commit()
    _col()  # 重建 collection


# ================= 向量库预留接口 =================
# 生产环境需要 Qdrant / Milvus 时，实现下面四个方法并把 _col() 换掉即可，
# 检索侧（retriever）无需改动。依赖不预装，缺包时给出明确安装提示。
class VectorStore:
    """向量库抽象。ChromaStore 是默认实现；Qdrant / Milvus 按需替换。"""

    def upsert(self, ids, docs, vectors, metas) -> None:
        raise NotImplementedError

    def query(self, vec, top_k, where) -> list[tuple[str, str, dict, float]]:
        raise NotImplementedError

    def delete_by_file(self, file_path: str) -> None:
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError


class QdrantStore(VectorStore):
    """预留：pip install qdrant-client 后可用，需先启动 Qdrant 服务。"""

    def __init__(self, url: str = "http://127.0.0.1:6333",
                 collection: str = "mini_rag"):
        try:
            from qdrant_client import QdrantClient
        except ImportError as e:
            raise RuntimeError(
                "未安装 qdrant-client：`pip install qdrant-client`") from e
        self.client = QdrantClient(url=url)
        self.collection = collection
        raise NotImplementedError(
            "QdrantStore 为预留实现：需补充 collection 建表与维度配置，"
            "且换库后向量维度可能变化，必须重建索引。")


class MilvusStore(VectorStore):
    """预留：pip install pymilvus 后可用，需先启动 Milvus 服务。"""

    def __init__(self, uri: str = "http://127.0.0.1:19530",
                 collection: str = "mini_rag"):
        try:
            from pymilvus import MilvusClient
        except ImportError as e:
            raise RuntimeError("未安装 pymilvus：`pip install pymilvus`") from e
        self.client = MilvusClient(uri=uri)
        self.collection = collection
        raise NotImplementedError(
            "MilvusStore 为预留实现：需补充 schema 与索引参数，"
            "且换库后向量维度可能变化，必须重建索引。")
