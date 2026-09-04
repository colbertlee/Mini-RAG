DOC-04: 硬件配置、模型选型与基线容量规划
文件编号: INFRA-SIZING-004
版本: v2.0.0（与实现同步 2026-09-04，此前 v1.1.0 为立项规划态）
适用范围: 本地部署评估、模型适配与资源容量基线管理
对应实现: mini_rag/config/settings.py

> v1.1.0 的选型是**立项期推荐**（bge-small-zh / qwen2.5:7b-instruct），
> 实际落地时全部更换。本版记录真实在用的型号、本机实测容量与内存铁律。

1. 模型选型（实测在用）

| 角色 | 模型 | 维度 / 参数 | 内存占用 | 选型理由 |
|---|---|---|---|---|
| Embedding | `qwen3-embedding:4b` | **2560 维** | ~2.5 GB | 中英混排语料（英文技术手册 + 中文查询）单模型通吃，免去双模型切换 |
| 生成 | `qwen3.5:4b` | 4B | ~3.4 GB | 严格遵循 Prompt 负向指令（拒答率高），指令理解准确 |

1.1 为什么没用 v1.1.0 推荐的型号

| v1.1.0 推荐 | 未采用原因 |
|---|---|
| `bge-small-zh-v1.5`（512 维） | 语料主体是**英文** Dell 技术文档，纯中文优化的 small 模型在英文专名（svc_xxx / 4-Port Card）上召回明显弱；且 512 维对长技术段落区分度不足 |
| `nomic-embed-text`（768 维） | 备选可行，但中英混合场景下 qwen3 表现更稳 |
| `bge-m3`（1024 维，2.2GB） | 自带稀疏召回能力本可替代 FTS5，但 2.2GB 常驻对本机内存压力过大，且当前 dense+sparse 双路已验证有效（融合 73.3% > 单路 60/63.3%） |
| `qwen2.5:7b-instruct`（5.5GB） | 7B 常驻 5.5GB，与 2.5GB embedding 共存会打爆 15.7GB 本机内存；4B 的 qwen3.5 在拒答遵循度上已满足要求 |
| `llama3.1:8b-instruct` | 中文技术问答弱于 qwen 系 |

**核心约束不是精度，是内存。** 本机 15.7GB，embedding 2.5GB + 生成 3.4GB = 5.9GB，
两者不能同时常驻 —— 这直接决定了 `keep_alive=0` 策略与下面第 4 节的铁律。

2. 硬件配置基线

| 组件 | 实测环境（开发机） | 最低可行 |
|---|---|---|
| CPU | 本机（纯 CPU 推理） | 4 核 x86（需 AVX2） |
| 内存 | 15.7 GB | **8 GB 可跑，16 GB 推荐**（见第 4 节铁律） |
| GPU | RX 470 —— **实际不可用**，走 Vulkan 会崩 | 无需独立显卡 |
| 存储 | NVMe SSD | 10 GB 可用空间 |
| 系统 | Windows 11（亦支持 Ubuntu 20.04+ / macOS 13+） | — |

> **GPU 是负资产，不是加速器。** Ollama 会自动走 Vulkan 调用独显，本机 RX 470
> 直接报 `failed to allocate Vulkan0 buffer` 崩溃。所有请求强制 `num_gpu=0` 走纯 CPU。
> 若换成 NVIDIA ≥8GB 可尝试放开，但需重新验证稳定性。

3. 容量规划（实测换算）

**实测锚点**：Powerstore 60 文件采样 / **3469 chunk** →

| 项 | 实测占用 |
|---|---|
| `data/vector_db`（Chroma，2560 维向量 + HNSW + 元数据） | 81 MB |
| `data/index.db`（SQLite FTS5 稀疏索引 + manifest） | 8.2 MB |
| **合计** | **89 MB** |

换算系数：**约 25.7 MB / 1000 chunk**（2560 维 float32 向量本体约 10 KB/chunk，
其余为 HNSW 图与元数据开销，约为向量本体的 2.5 倍）。

| 语料规模 | 预估 chunk | 预估磁盘 |
|---|---|---|
| 60 文件（当前基准） | 3,469 | 89 MB（实测） |
| 331 文件（Powerstore 全量） | ~19,000 | ~0.5 GB |
| 1,000 篇 / 10,000 页 | 40,000 ~ 60,000 | **1.0 ~ 1.5 GB** |

v1.1.0 按 512 维估的「600 MB ~ 1.2 GB」在 2560 维下偏低约 5 倍，本表已按实测修正。

4. 内存铁律（踩坑总结，违反会崩）

| # | 铁律 | 违反后果 |
|---|---|---|
| 1 | **建索引 / 批量评估前，可用内存必须 ≥ 4GB** | <2.5GB 时 embedding 吞吐从 6 chunks/s 掉到 **0.4 chunks/s**（60 文件啃了 64 分钟） |
| 2 | **embedding 与生成模型不得同时常驻** | 2.5GB + 3.4GB 并存会打爆内存。生产 `EMBED_KEEP_ALIVE="0"` 用完即卸；批量场景设 `"10m"` |
| 3 | **所有 Ollama 请求强制 `num_gpu=0`** | 自动走 Vulkan 用 RX 470 → `failed to allocate Vulkan0 buffer` 崩溃 |

铁律 2 的踩坑现场：30 条评估 × 每条重新加载 2.5GB embedding = 30 次内存冲击，
Ollama 直接崩（WinError 10061 拒绝连接），**且崩前已跑的数据全丢**。
批量场景必须 `MINIRAG_EMBED_KEEP_ALIVE=10m`，跑完再调 `keep_alive=0` 卸载。

吞吐自适应：`pipeline._embed_rate()` 按可用内存预估
（≥4GB → 6.0 chunks/s；<2.5GB → 0.4 chunks/s）。

5. 延迟基线（30 条基准实测）

| 场景 | 延迟 | 备注 |
|---|---|---|
| 检索层（HyDE 关闭 / 缓存命中） | 平均 **801 ms**，中位 ~700 ms | 分类：en 457 / zh_mix 353 / zh_positive 1251 / negative 242 ms |
| 负例证据闸拒答 | **17 ~ 38 ms** | 零模型开销，embedding 之前就短路 |
| `ask` 全开（含 HyDE + 生成） | +~25 s/query | HyDE 模型加载税 5.2s 为固定项；LLM `keep_alive=0` 每次重载 3.4GB |
| 解析 | ~0.058 s/页 | 805 页文档 46 s |
| 完整回答生成 | 3 ~ 6 s | 视输出长度 |

> ⚠️ baseline 报告里的 7574 ms/query 与终态的 801 ms **不可直接对比**：
> 差额主要来自 embedding 模型是否常驻（baseline 那次每条重新加载），
> 不是管线本身的差。比较管线性能请在相同 `EMBED_KEEP_ALIVE` 下测。

6. 降本建议

- **不要为了召回质量上 rerank 模型** —— 当前设计明确不做（范围边界）。
  需要更高精度时优先调 `RRF_DOC_VOTE_CAP` / `FINAL_TOP_N` / 证据闸阈值。
- **批量入库放夜间跑**：单文件解析失败只记录跳过，整批不中断，
  失败明细落 `logs/ingest_errors.jsonl`，第二天补跑即可。
- **换语料必重标定**（`DENSE_MIN` / `SPARSE_MIN` / 证据闸 / `RRF_DOC_VOTE_CAP`），
  否则所有质量数字失效。标定脚本 `_build/calib_leak.py`。
