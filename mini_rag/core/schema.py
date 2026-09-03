"""数据契约 —— 精简：用 dataclass，不引 pydantic。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Chunk:
    chunk_id: str
    content: str
    file_path: str
    file_name: str
    page_start: int | None = None    # 起始物理页（1 起）；非 PDF 为 None
    page_end: int | None = None      # 结束物理页；跨页表格 / 父块会与 page_start 不同
    chunk_index: int = 0
    doc_id: str = ""                 # 文档唯一 id（file_hash 前 16 位）
    doc_title: str = ""              # 文件名去扩展名
    section_path: str = ""           # "Chapter 3 > 3.2 Revenue Analysis"
    chunk_type: str = "text"         # text | table | formula | figure_caption
    parent_chunk_id: str = ""        # 父块 id；无父层时为空
    is_parent: bool = False          # True = 父块：只入库不参与检索，命中子块后取回作上下文
    language: str = "en"
    created_at: str = ""             # ISO8601
    has_code: bool = False
    has_warning: bool = False
    file_hash: str = ""
    token_estimate: int = 0

    # 兼容性别名：旧代码（generator / cli）按 page_number / heading_path 读取。
    # 注意是只读属性，构造时必须用 page_start / section_path。
    @property
    def page_number(self) -> int | None:
        return self.page_start

    @property
    def heading_path(self) -> str:
        return self.section_path


@dataclass
class ScoredChunk:
    chunk: Chunk
    dense_score: float = 0.0
    sparse_score: float = 0.0
    rrf_score: float = 0.0
    matched_by: str = ""         # dense | sparse | both


@dataclass
class Citation:
    file_name: str
    page_number: int | None
    chunk_id: str
    file_uri: str
    snippet: str = ""


@dataclass
class QAResponse:
    query: str
    answer: str
    is_fallback: bool = False
    rejected_by: str = ""        # "" | no_candidate | below_threshold
    degraded_reason: str = ""    # 非空 = L3 校验失败，降级为原文摘录（第三态）
    citations: list[Citation] = field(default_factory=list)
    retrieved: list[ScoredChunk] = field(default_factory=list)
    latency_ms: dict = field(default_factory=dict)
