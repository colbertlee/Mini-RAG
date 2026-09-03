"""自适应切分：按文档页数选档 + 父子分层 + 表格/代码原子 + overlap 落句子边界。

边界优先级：标题层级 > 段落空行 > 句子边界 > 空格 > 字符兜底。
窗口语义（与 LangChain 一致）：窗口大小 = chunk_size，步长 = chunk_size - overlap，
因此「含 overlap 后的最终 chunk」仍不超过 chunk_size 上限。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path

from mini_rag.config import settings
from mini_rag.core import cleaner, tokenizer
from mini_rag.core.parsers import Segment
from mini_rag.core.schema import Chunk

WARN_RE = re.compile(r"(WARNING|CAUTION|高风险|警告|注意[:：]?)", re.I)
FIG_RE = re.compile(r"^\s*(figure|fig\.|table|图|表)\s*[\d\.\-]+[\.:：]", re.I)
_SENT = re.compile(r"(?<=[.!?。！？])\s+(?=[A-Za-z0-9\u4e00-\u9fff])")
_ABBREV = {"e.g", "i.e", "fig", "no", "vs", "etc", "al", "mr", "dr", "st",
           "inc", "ltd", "fig", "sec", "eq", "ref", "approx"}


def count_tokens(text: str) -> int:
    return tokenizer.count_tokens(text)


def tier_for(page_count: int) -> tuple[str, int, int, int]:
    """按页数选档 → (档位名, 子块 token, overlap token, 父块 token)。"""
    tiers = settings.SPLIT_TIERS
    names = ["short", "medium", "long"]
    for i, (maxp, size, ov, parent) in enumerate(tiers):
        if page_count <= maxp:
            return names[i] if i < len(names) else f"tier{i}", size, ov, parent
    last = tiers[-1]
    return "long", last[1], last[2], last[3]


def _ends_with_abbrev(s: str) -> bool:
    s = s.strip()
    m = re.search(r"([A-Za-z]+)\.$", s)
    if m:
        w = m.group(1).lower()
        if w in _ABBREV or len(w) == 1:
            return True
    return bool(re.search(r"\b\d+\.$", s))     # "3." 这类列表编号


def split_sentences(text: str) -> list[str]:
    """句子切分，带缩写保护（e.g. / Fig. 1 / 单字母 / 数字编号不切）。"""
    raw = [s for s in _SENT.split(text) if s.strip()]
    out: list[str] = []
    for s in raw:
        if out and _ends_with_abbrev(out[-1]):
            out[-1] = out[-1] + " " + s
        else:
            out.append(s)
    return out or ([text] if text.strip() else [])


def _pack(units: list[str], size: int, overlap: int, joiner: str = "\n\n") -> list[str]:
    """滑动窗口打包：窗口 ≤ size，回退量 ≈ overlap 且落在 unit 边界（句子/段落）。"""
    if not units:
        return []
    out: list[str] = []
    i, n = 0, len(units)
    jt = count_tokens(joiner)            # 连接符本身占 token，必须计入窗口
    while i < n:
        buf: list[str] = []
        total = 0
        j = i
        while j < n:
            t = count_tokens(units[j]) + (jt if buf else 0)
            if buf and total + t > size:
                break
            buf.append(units[j])
            total += t
            j += 1
        if not buf:                              # 单个 unit 就超限（已预切过，理论上不会）
            buf = [units[i]]
            j = i + 1
        out.append(joiner.join(buf))
        if j >= n:
            break
        acc, back = 0, 0
        for k in range(len(buf) - 1, -1, -1):    # 从末尾回退，累计 ≤ overlap
            t = count_tokens(buf[k])
            if back and acc + t > overlap:
                break
            acc += t
            back += 1
            if acc >= overlap:
                break
        nxt = j - back
        i = nxt if nxt > i else j
    return out


def _split_oversize(text: str, size: int) -> list[str]:
    """单段落超限：句子 → 空格 → 字符兜底。"""
    sents = split_sentences(text)
    if len(sents) > 1:
        return _pack(sents, size, 0, " ")
    words = text.split(" ")
    if len(words) > 1:
        return _pack(words, size, 0, " ")
    return [tokenizer.truncate(text, size)]


def _units(text: str, size: int) -> list[tuple[str, int]]:
    """段落 → [(文本, token数)]。超长段落先按句子/空格/字符降级切开。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[tuple[str, int]] = []
    for p in paras:
        n = count_tokens(p)
        if n <= size:
            out.append((p, n))
        else:
            for piece in _split_oversize(p, size):
                out.append((piece, count_tokens(piece)))
    return out


def _split_table(md: str, size: int) -> list[str]:
    """表格优先整体保留；超限才按行切，每片都带表头。"""
    if count_tokens(md) <= size:
        return [md]
    lines = md.split("\n")
    if len(lines) < 3:
        return [tokenizer.truncate(md, size)]
    head, body = lines[:2], lines[2:]
    out: list[str] = []
    buf: list[str] = []
    for ln in body:
        cand = buf + [ln]
        if not buf or count_tokens("\n".join(head + cand)) <= size:
            buf = cand
        else:
            out.append("\n".join(head + buf))
            buf = [ln]
    if buf:
        out.append("\n".join(head + buf))
    return out


def _split_code(code: str, size: int) -> list[str]:
    """代码块整体保留；超限按行切，续块头部补注释说明。"""
    if count_tokens(code) <= size:
        return [code]
    lines = code.split("\n")
    first = lines[0] if lines else ""
    out: list[str] = []
    buf = ""
    for ln in lines:
        cand = buf + "\n" + ln if buf else ln
        if count_tokens(cand) <= size:
            buf = cand
        else:
            if buf:
                out.append(buf)
            buf = f"# 续：{first}\n{ln}" if first else ln
    if buf:
        out.append(buf)
    return out


def _make(cid: str, content: str, file_path: str, file_name: str,
          page_start: int | None, page_end: int | None, idx: int,
          section: str, kind: str, doc_id: str, doc_title: str,
          file_hash: str, lang: str, now: str,
          parent_id: str = "", is_parent: bool = False) -> Chunk:
    return Chunk(
        chunk_id=cid, content=content, file_path=file_path, file_name=file_name,
        page_start=page_start, page_end=page_end, chunk_index=idx,
        doc_id=doc_id, doc_title=doc_title, section_path=section,
        chunk_type=kind, parent_chunk_id=parent_id, is_parent=is_parent,
        language=lang, created_at=now, has_code=(kind == "code"),
        has_warning=bool(WARN_RE.search(content)),
        file_hash=file_hash, token_estimate=count_tokens(content),
    )


def _child_id(file_path: str, page_start: int | None, idx: int) -> str:
    return hashlib.sha256(
        f"{file_path}|{page_start}|{idx}".encode()).hexdigest()[:16]


def _attach_parents(children: list[Chunk], parent_size: int, file_path: str,
                    file_name: str, doc_id: str, doc_title: str,
                    file_hash: str, lang: str, now: str) -> list[Chunk]:
    """把连续 text 子块按 parent_size 归组为父块，并回填子块的 parent_chunk_id。

    父块只入库、不参与检索（is_parent=True），命中子块后作为上下文送 LLM。
    """
    groups: list[list[Chunk]] = []
    cur: list[Chunk] = []
    total = 0
    for c in children:
        # 表格/代码也参与归组：只在表格处断开会让父块碎成一地
        # （实测覆盖率掉到 10%），而父块本来就该把相邻表格一起给 LLM。
        if cur and total + c.token_estimate > parent_size:
            groups.append(cur)
            cur, total = [], 0
        cur.append(c)
        total += c.token_estimate
    if cur:
        groups.append(cur)

    parents: list[Chunk] = []
    for gi, g in enumerate(groups):
        if len(g) < 2:                      # 只有一块就不必再套父层
            continue
        pid = "P" + hashlib.sha256(
            f"{file_path}|parent|{gi}".encode()).hexdigest()[:15]
        content = "\n\n".join(x.content for x in g)
        parents.append(_make(
            pid, content, file_path, file_name,
            g[0].page_start, g[-1].page_end, gi,
            g[0].section_path, "text", doc_id, doc_title, file_hash, lang, now,
            is_parent=True))
        for x in g:
            x.parent_chunk_id = pid
    return parents


def split_document(file_hash: str, file_path: str, file_name: str,
                   segments: list[Segment], page_count: int = 0) -> list[Chunk]:
    """解析结果 → chunk 列表（含父块）。page_count 决定切分档位。"""
    tier, size, overlap, parent_size = tier_for(page_count)
    doc_id = file_hash[:16]
    doc_title = Path(file_name).stem
    now = datetime.now().isoformat(timespec="seconds")
    sample = "\n".join(s.text for s in segments[:60])
    lang = cleaner.detect_language(sample)

    # buffer：连续的文本单元；遇到表格/代码/公式强制 flush，遇到标题按阈值决定
    buf: list[str] = []
    buf_tokens = 0
    buf_start: int | None = None
    buf_end: int | None = None
    buf_section = ""
    children: list[Chunk] = []
    idx = 0

    def flush() -> None:
        nonlocal buf, buf_start, buf_end, idx, buf_tokens
        if not buf:
            return
        for piece in _pack(buf, size, overlap):
            if count_tokens(piece) < settings.MIN_CHUNK_TOKENS:
                continue
            children.append(_make(_child_id(file_path, buf_start, idx), piece,
                                  file_path, file_name, buf_start, buf_end,
                                  idx, buf_section, "text", doc_id, doc_title,
                                  file_hash, lang, now))
            idx += 1
        buf, buf_start, buf_end, buf_tokens = [], None, None, 0

    for seg in segments:
        if seg.kind == "heading":
            # 标题是最高优先级边界，但只在 buffer 已累积够多时才断：
            # Dell 这类文档小节普遍只有几十 token，每见标题就硬切会碎成渣。
            if buf and buf_tokens >= size * settings.SPLIT_HEADING_BREAK_RATIO:
                flush()
            n = count_tokens(seg.text)
            buf.append(seg.text)                      # 标题本身入 chunk，保证可被检索
            buf_tokens += n
            if buf_start is None:
                buf_start = seg.page
            buf_end = seg.page_end or seg.page
            buf_section = seg.heading
            continue

        p_start, p_end = seg.page, seg.page_end

        if seg.kind == "table":
            flush()
            # 表格整体保留，但硬上限仍受当前档位 size 约束：
            # 超过 embedding 模型的有效长度会让向量质量塌掉，宁可按行切。
            for piece in _split_table(seg.text, min(settings.TABLE_MAX_TOKENS, size)):
                children.append(_make(_child_id(file_path, p_start, idx), piece,
                                      file_path, file_name, p_start, p_end,
                                      idx, seg.heading, "table", doc_id,
                                      doc_title, file_hash, lang, now))
                idx += 1
        elif seg.kind == "code":
            flush()
            for piece in _split_code(seg.text, min(settings.CODE_MAX_TOKENS, size)):
                children.append(_make(_child_id(file_path, p_start, idx), piece,
                                      file_path, file_name, p_start, p_end,
                                      idx, seg.heading, "code", doc_id,
                                      doc_title, file_hash, lang, now))
                idx += 1
        elif seg.kind == "formula":
            flush()
            lim = min(settings.CODE_MAX_TOKENS, size)
            piece = seg.text if count_tokens(seg.text) <= lim \
                else tokenizer.truncate(seg.text, lim)
            children.append(_make(_child_id(file_path, p_start, idx), piece,
                                  file_path, file_name, p_start, p_end,
                                  idx, seg.heading, "formula", doc_id,
                                  doc_title, file_hash, lang, now))
            idx += 1
        else:
            match = FIG_RE.match(seg.text.strip())
            tok = count_tokens(seg.text)
            # 图注/表注：只有「够长」的才独立成 figure_caption 块；短图注
            # （如 "Table 1. Installing expansion enclosures..."）并入正文，
            # 否则长篇手册里几百条短图注各自成块，会把索引碎片化。
            kind = ("figure_caption" if match
                    and settings.FIGURE_CAPTION_MIN_TOKENS <= tok < 120
                    else "text")
            if kind == "figure_caption":
                flush()
                children.append(_make(_child_id(file_path, p_start, idx),
                                      seg.text.strip(), file_path, file_name,
                                      p_start, p_end, idx, seg.heading, kind,
                                      doc_id, doc_title, file_hash, lang, now))
                idx += 1
                continue
            if buf_start is None:
                buf_start = p_start
            buf_end = p_end if p_end is not None else p_start
            buf_section = seg.heading or buf_section
            for u, n in _units(seg.text, size):
                buf.append(u)
                buf_tokens += n
    flush()

    parents: list[Chunk] = []
    if parent_size > 0 and children:
        parents = _attach_parents(children, parent_size, file_path, file_name,
                                  doc_id, doc_title, file_hash, lang, now)
    return children + parents
