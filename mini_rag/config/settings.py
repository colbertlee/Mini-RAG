"""Mini-RAG 全局配置 —— 精简：所有可调项集中在此，用户只改这一个文件。"""
from __future__ import annotations

import os
from pathlib import Path

# ---- 离线 / 遥测禁用（隐私合规）----
for _k, _v in {
    "ANONYMIZED_TELEMETRY": "False",
    "CHROMA_TELEMETRY": "False",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "NO_PROXY": "127.0.0.1,localhost",
    "no_proxy": "127.0.0.1,localhost",
}.items():
    os.environ.setdefault(_k, _v)

# ---- 路径 ----
BASE_DIR = Path(__file__).resolve().parent.parent          # mini_rag/
ROOT_DIR = BASE_DIR.parent                                 # 项目根（Mini-RAG/）
DATA_DIR = ROOT_DIR / "data"
VECTOR_DIR = DATA_DIR / "vector_db"
INDEX_DB = DATA_DIR / "index.db"
LOG_DIR = ROOT_DIR / "logs"
ERROR_LOG = LOG_DIR / "ingest_errors.jsonl"

# ---- Embedding ----
# 换 provider 会改变向量维度（qwen3=2560 / OpenAI small=1536 / bge-large=1024），
# 索引不兼容，必须 python -m mini_rag.cli index --rebuild 重建。
EMBED_PROVIDER = "ollama"        # ollama | openai | bge
OPENAI_EMBED_MODEL = "text-embedding-3-small"
OPENAI_API_KEY = ""              # 留空则从环境变量 OPENAI_API_KEY 读取
BGE_MODEL = "BAAI/bge-large-zh-v1.5"

# ---- Ollama ----
OLLAMA_URL = "http://127.0.0.1:11434"
EMBED_MODEL = "qwen3-embedding:4b"
LLM_MODEL = "qwen3.5:4b"
EMBED_BATCH = 32
EMBED_RETRIES = 3
REQUEST_TIMEOUT = 600

# ---- 吞吐预估（按可用内存自适应，供 pipeline._embed_rate 使用）----
# 两个实测锚点（2026-09-03 本机）：可用内存充足（模型完全驻留）≈ 6 chunks/s；
# 可用内存 < 模型大小（严重换页）≈ 0.37 chunks/s（60 文件啃了 64 分钟）。
EMBED_CHUNKS_PER_SEC = 6.0       # 理想吞吐：模型驻留、batch=32
EMBED_CHUNKS_PER_SEC_SWAP = 0.4  # 换页吞吐：模型装不进内存时的实测下限
EMBED_MODEL_GB = 2.5             # qwen3-embedding:4b 驻留内存占用
MEM_HEADROOM_GB = 1.5            # Ollama 进程 + 系统基础余量

# ---- 文档解析（类型路由 / 多栏 / 表格 / 公式）----
# 扫描页判定：单页文字层字符数 < 阈值 → 该页视为扫描页，路由到 OCR。
PDF_TEXT_LAYER_MIN_CHARS = 80
PDF_SCAN_RATIO_FOR_OCR = 0.30    # 扫描页占比 ≥ 此值 → 整份标记为扫描件（仍逐页处理）
OCR_ENABLED = True
OCR_PROVIDER = "rapidocr"        # rapidocr（本地，默认）| llamaparse（云端，预留桩，见 parsers.py）
OCR_DPI = 200                    # 150 省时 / 200 平衡 / 300 精度高但耗时约 2.3 倍
OCR_MAX_PAGES = 80               # 扫描页超过此数放弃 OCR（成本保护：千页文档别卡死）
OCR_MIN_CONFIDENCE = 0.50        # OCR 行置信度过滤
MULTICOLUMN_ENABLED = True       # 多栏恢复：关掉则退化为按 y 排序（左右栏会交错）
MULTICOLUMN_MIN_BLOCKS = 6       # 文本块少于此值不尝试分栏（封面 / 短页）
TABLE_ENABLED = True             # 表格提取：用 PyMuPDF find_tables，无需 pdfplumber
TABLE_CROSS_PAGE = True          # 跨页表格拼接（下一页表头重复即判为续表）
FORMULA_DETECT = True            # 公式启发式：保留原文并标 chunk_type=formula（不引 Nougat）

# ---- 内容清洗 ----
CLEAN_ENABLED = True
HEADER_FOOTER_RATIO = 0.30       # 某行在 ≥30% 的页里出现于页首/页尾 → 判为页眉页脚
HEADER_FOOTER_ZONE = 0.12        # 页面上下各 12% 高度为页眉页脚候选区
DEDUP_PARAGRAPH = True           # 连续重复段落去重

# 目录页（TOC）整页跳过：目录是导航不是正文，混入会污染检索。
# 判据（清洗前用原始块判断）：显式 "Table of Contents" 标题，或目录条目行
# （纯章节号 "1.1" / 点线+页码 "......2"）占比 ≥ 阈值。
TOC_DETECT = True
TOC_LINE_RATIO = 0.35            # 目录条目行占比阈值，调低更激进、调高更保守

NOISE_PATTERNS = [
    r"^\s*(?:page\s+)?\d+\s*(?:of\s+\d+)?\s*$",   # 纯页码 / Page 3 of 20
    r"^\s*[-–—.]\s*\d+\s*[-–—.]\s*$",             # - 12 -
    r"\.{4,}\s*\d*\s*$",                          # 目录条目：Additional Resources......12
    r"^\s*\.{3,}\s*$",                            # 纯点线填充
    r"©.*?(?:reserved|inc\.|corp\.|ltd)",         # 版权行
    r"\ball rights reserved\b",
    r"\bconfidential(?:ity)?\b",
    r"this document is provided.{0,40}\bas is\b",
    r"\bdell\s+(?:inc\.|technologies)\b.{0,40}\ball rights\b",
    r"do not (?:copy|distribute|reproduce)",
    r"^\s*draft\s*$",
]

# ---- 切分（按文档页数自适应三档）----
TOKENIZER_ENCODING = "cl100k_base"   # tiktoken 编码；不可用时降级为字符启发式
# (页数上限, 子块 token, overlap token, 父块 token)。父块 0 = 不生成父层。
SPLIT_TIERS = (
    (19,     512,  64, 0),     # <20 页：按标题层级切，overlap 64（需求 50~80）
    (100,    768,  96, 0),     # 20~100 页：按页/章节切，overlap 96 ≈ 块大小 12.5%
    (10 ** 9, 512, 64, 1536),  # >100 页：父子分层，父块 1536（1024~2048 中值）
)
# 标题作为切分边界的触发阈值：buffer 已占满该比例时才在标题处断开。
# 设 0 = 每个标题都硬切（小节短时会碎成几十 token 的碎片）；设 1 = 永不因标题断开。
SPLIT_HEADING_BREAK_RATIO = 0.6

# 图注/表注只有 ≥ 此 token 数才独立成 figure_caption 块，更短的并入正文，
# 避免长篇技术手册里海量「Table 1. xxx」短图注各自成块、把索引碎片化。
FIGURE_CAPTION_MIN_TOKENS = 40

CODE_MAX_TOKENS = 768         # 代码块原子上限，超限按行切
TABLE_MAX_TOKENS = 2048       # 表格原子上限，超限按行切
MIN_CHUNK_TOKENS = 15         # 低于此值的碎片丢弃

# ---- 幂等 ----
ON_DUPLICATE = "skip"         # skip | update：doc_id + file_hash 均未变时的行为

# ---- 检索 ----
DENSE_TOP_K = 20
SPARSE_TOP_K = 20
RRF_K = 60
FINAL_TOP_N = 4

# 2026-09-04 深度优化新增开关（默认全开；任一关掉即降级为对应子层的 dense 单路）。
# 设计原则：改造是叠加式的，关掉任何一层不破坏既有行为。
QUERY_REWRITE_ENABLED = True   # 关 = 不做 L1 同义词扩展 / svc 归一 / 子查询拆分
HYDE_ENABLED = True            # 关 = 不调 LLM 生成假设性段落
MMR_ENABLED = True             # 关 = 不做 MMR 去冗余，top-N 按 RRF 分数直接切

# 融合策略开关（2026-09-03 架构评审后落地）：
#   True  = dense 主力 + sparse 兜底（当前语料组合的正确姿势）。
#           查询中文 vs 语料英文时，FTS5 稀疏路跨语言结构性失效（纯中文查询召回 0），
#           而 RRF 只用名次丢弃分数，sparse rank1 会压过 dense rank2 把噪声挤进 top4。
#           因此 dense 一旦有通过 DENSE_MIN 的候选就只信 dense；仅当 dense 空手时才
#           回退 sparse（救英文词/命令精确匹配）。
#   False = 保留旧的全 RRF 融合（语料换成中文、或需要英文术语精确匹配时切回）。
SPARSE_FALLBACK_ONLY = True

# ---- 阈值（初始值，可 CLI 覆盖。检索不到就短路拒答，宁可拒答不可编造）----
# 2026-09-03 换真实语料（Powerstore 57 文件/1352 chunk）后重标定：
#   正例 top1 ∈ [0.706, 0.902]，负例 top1 ∈ [0.313, 0.436]，无重叠（比旧语料干净得多）。
#   负例最高 0.436、正例最低 0.706 → 取 0.60：负例侧余量 0.164（100% 拒答），正例侧余量 0.106。
#   全量语料（1000+ 文件）入库后分布可能再漂移，需重跑标定。
DENSE_MIN = 0.60

# ---- 生成 ----
NUM_CTX = 4096
MAX_CONTEXT_TOKENS = 2800

# ---- L3 生成后校验（纯规则第三道防线，零幻觉的最后闸门）----
# 回答里命中任一话术 → 判定疑似引用上下文之外的知识，降级为「原文摘录」而非拒答。
# 刻意只收「诉诸外部知识 / 不确定推断」的强信号词，不收「可能/应该」这类
# 在忠实转述上下文中也会正常出现的弱词，避免误伤。
INFERENCE_PHRASES = [
    "据我所知", "根据我的了解", "根据我的知识", "根据我的经验",
    "我推测", "我猜", "我估计", "我印象中", "我记得",
    "一般来说", "通常情况", "理论上", "普遍认为",
]

# ---- 语料白名单（用户按需修改）----
# 只索引这些目录下的文档。空列表 = 不索引任何内容，运行 index 前必须先配置。
# 示例（Windows 绝对路径，用正斜杠）：
#   INCLUDE_DIRS = [r"C:/path/to/your/docs"]
INCLUDE_DIRS = [
    r"C:/BaiduSyncdisk/Works/Powerstore",
]
EXCLUDE_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__", ".git",
    "site-packages", ".workbuddy", "_build", "data", "logs", "mini_rag",
}
EXT_ALLOWLIST = {".pdf", ".md", ".txt", ".docx", ".html", ".htm"}
MAX_DOCS_PER_DIR = 500      # Powerstore 331 个文件全进
MAX_FILE_SIZE_MB = 50

# ---- 中文停用词（query 提取内容词时过滤）----
STOPWORDS = {
    "的", "了", "和", "是", "在", "我", "你", "他", "它", "有", "与", "及", "或",
    "等", "就", "都", "而", "该", "其", "对", "为", "被", "把", "吗", "呢", "啊",
    "怎么", "如何", "什么", "哪些", "哪个", "为什么", "多少", "一个", "一下", "这个",
    "那个", "我们", "你们", "他们", "这里", "那里", "还", "要", "会", "能", "可以",
    "请", "用", "给", "让", "不", "没", "很", "也", "又", "再", "才", "只", "最",
}


def ensure_dirs() -> None:
    for d in (DATA_DIR, VECTOR_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
