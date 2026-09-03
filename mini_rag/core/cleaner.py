"""内容清洗：页眉页脚学习 + 模板噪声清除 + 乱码/重复段落处理。

两阶段设计（页眉页脚必须跨页统计才准，逐页无法判断）：
  1) learn_noise(pages)            —— 统计页首/页尾跨页高频行，得到噪声集合
  2) clean_blocks(blocks, noise)   —— 逐块清洗，不破坏正文语义

页码归一化是关键：把数字替换为 # 后，"Page 1 of 20" 与 "Page 2 of 20"
会归并为同一个 key，否则每页都不重复、学不到页脚。
"""
from __future__ import annotations

import re
from collections import Counter

from mini_rag.config import settings

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd]")
_DEHYPH = re.compile(r"([a-zA-Z])-\n([a-z])")     # 英文断词换行：config-\nuration
_WS_LINE = re.compile(r"[ \t]+")
_MULTI_NL = re.compile(r"\n{3,}")
_DIGIT = re.compile(r"\d+")

_NOISE_RE: list[re.Pattern] | None = None


def _noise_res() -> list[re.Pattern]:
    global _NOISE_RE
    if _NOISE_RE is None:
        _NOISE_RE = [re.compile(p, re.I) for p in settings.NOISE_PATTERNS]
    return _NOISE_RE


def norm_key(line: str) -> str:
    """归一化：小写 + 数字替换为 # + 压缩空白，用于跨页比对。"""
    return _WS_LINE.sub(" ", _DIGIT.sub("#", line.lower())).strip()


def _is_noise_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    for r in _noise_res():
        if r.search(s):
            return True
    return False


def learn_noise(pages: list[dict]) -> set[str]:
    """跨页统计页首/页尾高频行。页数过少时直接返回空集，避免误删正文。"""
    if not settings.CLEAN_ENABLED or len(pages) < 3:
        return set()
    zone = settings.HEADER_FOOTER_ZONE
    cnt: Counter[str] = Counter()
    for pg in pages:
        h = pg.get("height") or 0
        if not h:
            continue
        top, bottom = h * zone, h * (1 - zone)
        seen: set[str] = set()
        for b in pg["blocks"]:
            y0, y1 = b["bbox"][1], b["bbox"][3]
            if y1 <= top or y0 >= bottom:
                first = b["text"].split("\n")[0]
                key = norm_key(first)
                if key and key not in seen:
                    seen.add(key)
                    cnt[key] += 1
    thr = max(3, int(len(pages) * settings.HEADER_FOOTER_RATIO))
    return {k for k, v in cnt.items() if v >= thr}


def scrub(text: str) -> str:
    """乱码 / 不间断空格 / 断词换行 / 多余空行。"""
    text = _CTRL.sub("", text)
    text = text.replace("\u00a0", " ")
    text = _DEHYPH.sub(r"\1\2", text)
    text = "\n".join(_WS_LINE.sub(" ", ln).strip() for ln in text.split("\n"))
    text = _MULTI_NL.sub("\n\n", text)
    return text.strip()


def clean_blocks(blocks: list[dict], noise: set[str], height: float) -> list[dict]:
    """逐块清洗：丢页眉页脚整块、按行过滤模板噪声、规整空白。"""
    if not settings.CLEAN_ENABLED:
        return [b for b in blocks if b["text"].strip()]
    zone = settings.HEADER_FOOTER_ZONE
    top, bottom = height * zone, height * (1 - zone)
    out: list[dict] = []
    for b in blocks:
        text = b["text"]
        if not text.strip():
            continue
        lines = text.split("\n")
        # 页眉页脚区且首行命中跨页噪声 → 整块丢弃
        if noise and height:
            y0, y1 = b["bbox"][1], b["bbox"][3]
            if (y1 <= top or y0 >= bottom) and norm_key(lines[0]) in noise:
                continue
        kept = [ln for ln in lines if not _is_noise_line(ln)]
        text = scrub("\n".join(kept))
        if not text:
            continue
        b = dict(b)
        b["text"] = text
        out.append(b)
    return out


def clean_text(text: str) -> str:
    """非 PDF 源（md/txt/docx/html）的清洗：只做行级噪声过滤与规整。"""
    if not settings.CLEAN_ENABLED:
        return text
    kept = [ln for ln in text.split("\n") if not _is_noise_line(ln)]
    return scrub("\n".join(kept))


def dedup_paragraphs(text: str) -> str:
    """连续重复段落去重（PDF 分栏重叠或解析重复会产生）。"""
    if not settings.DEDUP_PARAGRAPH:
        return text
    paras = text.split("\n\n")
    out: list[str] = []
    for p in paras:
        s = p.strip()
        if not s:
            continue
        if out and norm_key(out[-1]) == norm_key(s):
            continue
        out.append(s)
    return "\n\n".join(out)


def detect_language(text: str) -> str:
    """粗判语言：CJK 占比 > 5% 记 zh，否则 en。"""
    sample = text[:4000]
    if not sample:
        return "en"
    cjk = sum(1 for c in sample if "\u4e00" <= c <= "\u9fff")
    return "zh" if cjk / len(sample) > 0.05 else "en"
