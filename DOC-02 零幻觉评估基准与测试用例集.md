DOC-02: 零幻觉评估基准与测试用例集
文件编号: QA-BENCH-002
版本: v2.0.0（与实现同步 2026-09-04，此前 v1.1.0 为立项规划态）
适用范围: 检索质量量化验收、自动化回归测试、拒答逻辑验证
对应实现: scripts/eval_retrieval.py · scripts/test_retrieval_logic.py · _build/eval_corpus.json

> **v1.1.0 的指标是规划目标，不是实测基线。** 本版替换为当前真正可自动化测量的口径，
> 并说明原指标为何无法落地。基准集与 ground_truth 一律以 `_build/eval_corpus.json` 为准。

1. 评估指标（可自动化口径）

v1.1.0 设定「Context Recall ≥ 92% / Context Precision ≥ 90% / Citation Hit Rate ≥ 98%」。
这三个指标在当前基准下**无法客观测量**，原因与替代方案如下：

| 原指标 | 问题 | v2.0.0 替代口径 |
|---|---|---|
| Context Recall ≥92% | 需要 chunk 级 ground truth（标注每个问题该召回哪几个 chunk）。当前基准只标到**文件级**（ground_truth_file） | 文件级 hit@1 / hit@3 / hit@10 |
| Context Precision ≥90% | 同上，且「精确」需判定每个召回块是否相关，无标注则只能主观打分 | 负例命中率（应为 0）+ 人工抽检 |
| Citation Hit Rate ≥98% | 引用由 `generator._citations()` 从召回块**程序化生成**，非 LLM 自造，天然 100% 可溯源；「准确」需人工核验 | OSC 8 file:/// 链接 + `#page=N`，人工抽检 |
| Faithfulness = 100% | ✅ 可测，且必须达标 | L3 规则校验（见第 4 节），不通过即降级原文摘录 |
| Refusal Accuracy = 100% | ✅ 可测，且必须达标 | 负例 hit@1/@3/@10 全为 0 = 全拒 |

**当前准入基线（30 条基准，v0.2.1 实测）：**

| 口径 | hit@1 | hit@3 | hit@10 | 判定 |
|---|---|---|---|---|
| 整体 | 66.7% | 76.7% | 80.0% | 准入通过 |
| negative（5 条） | 0% | 0% | 0% | **必须全 0**（全拒），任一非零即回归失败 |
| Faithfulness | — | — | — | **100%**（L3 校验强制） |

负例 hit 全为 0 是期望结果，不是缺陷 —— 它意味着检索层一无所获，
后续由 LLM 输出固定拒答串。

1.1 分类基线

| 分类 | n | hit@1 | hit@3 | hit@10 | 平均延迟 | 特征 |
|---|---|---|---|---|---|---|
| en_positive | 5 | 100% | 100% | 100% | 457 ms | 纯英文专名查询（含 svc_* 命令） |
| zh_mix | 5 | 80% | 100% | 100% | 353 ms | 中英混排（中文句式 + 英文专名） |
| zh_positive | 15 | 73.3% | 86.7% | 93.3% | 1251 ms | 纯中文查询，依赖中→英术语映射 |
| negative | 5 | 0% | 0% | 0% | 242 ms | 应全部拒答 |

整体平均延迟 801 ms/query（baseline 为 7574 ms，差额主要来自 embedding 模型是否常驻，
非管线本身差异）。

融合 vs 单路（30 条中 29 条含英文专名）：dense-only 60.0% / sparse-only 63.3% /
**RRF 融合 73.3%**。

2. 基准集 (Test Corpus)

文件：`_build/eval_corpus.json`（v2，30 条）。字段：
`id` / `question` / `category` / `difficulty` / `ground_truth_file` / `expected_hits`。
ground_truth_file 为**文件名子串**，命中即算 hit（文件级口径）。

2.1 正例（25 条）

| ID | 查询 | ground truth（文件名子串） |
|---|---|---|
| P01 | 如何使用 svc_factory_reset 命令将 PowerStore 系统重置为出厂状态？ | svc_factory_reset |
| P02 | PowerStore 怎么更换 BBU？ | BBU |
| P03 | PowerStore 节点 BBU 告警怎么处理？ | Node BBU |
| P04 | PowerStore cache state change 告警 | Cache State Change |
| P05 | PowerStore 数据库卷故障告警 | Database Volume State |
| P06 | PowerStore HA 事件 data path 异常 | HA events, data path |
| P07 | PowerStore 4-Port Card 怎么换？ | 4-Port Card |
| P08 | PowerStore base enclosure 更换流程 | base enclosure |
| P09 | PowerStore IO Module 更换 | IO Module |
| P10 | PowerStore Embedded Module 更换 | Embedded Module |
| P11 | PowerStore Node 更换 | Replace a Node |
| P12 | PowerStore OS 升级前的健康检查 | Pre-Upgrade Health Check |
| P13 | PowerStore NTP 时间差异告警 | NTP |
| P14 | PowerStore 时间偏差 BMC | time skew |
| P15 | PowerStore 4.3.0.0 release note 变更内容 | pwrstr-4-3-0-0-rn |
| P16 | svc_factory_reset command how to use | svc_factory_reset |
| P17 | svc_db_recovery service script | svc_db_recovery |
| P18 | How to update drive DB using svc_update_drive_db | svc_update_drive_db |
| P19 | NDU from version 3.x to 3.x performance metrics not displayed | Following NDU |
| P20 | PowerStore factory reset to preceding version | Downgrading a factory |
| P21 | PowerStore 服务脚本 svc_diag 诊断命令怎么用 | service_scripts_guide |
| P22 | BBU end-of-life state alert | BBU end-of-life |
| P23 | WinSCP executable permissions svc upload | WinSCP |
| P24 | stale DriveDB drives reported as failed | stale DriveDB |
| P25 | TRIF Metro SCSI Persistent Reservations | Metro SCSI |

2.2 负例（5 条）· 判定准则：必须输出「知识库中未找到相关信息。」

| ID | 查询 | 难度 | 设计意图 | 实际拦截层 |
|---|---|---|---|---|
| N01 | 红烧肉怎么做？ | easy | 完全无关领域 | 证据闸（规则 F） |
| N02 | 如何在 Brocade 交换机上配置 zone？ | hard | 语料无 Brocade 文档（专名 DF=0） | 证据闸（规则 G），17~38ms |
| N03 | PowerStore 如何安装 Docker？ | hard | 虚构功能（词法上 docker 在语料有出现） | LLM 语义拒答 |
| N04 | Unity XT 的存储池怎么配置？ | hard | 跨产品混淆（Unity ≠ PowerStore） | LLM 语义拒答 |
| N05 | PowerStore 量子加密 | easy | 虚构术语（量子/加密 DF=0 且无词典映射） | 证据闸（规则 F） |

N03/N04 是**最有价值的两条**：它们在检索层会泄漏真实内容块（语料确实提到 docker / unity），
但仍被 LLM 判定为「上下文与问题无关」而拒答 —— 证明防线不依赖检索层完美。

3. 自动化入口

```bash
# 单元测试（不依赖任何模型，可接 CI）—— 当前 38/38
python scripts/test_retrieval_logic.py

# 检索基准真机跑（需 Ollama + 已建索引）
python scripts/eval_retrieval.py both --online --off-hyde

# 只复测指定 case / 分批跑长任务
python scripts/eval_retrieval.py optimized --online --only P08,P15,P24
python scripts/eval_retrieval.py optimized --online --limit 10

# 输出 JSON 对比（默认 _build/eval_<tag>_report.json）
python scripts/eval_retrieval.py optimized --online --out _build/my_run.json
```

`--off-hyde` / `--off-rewrite` / `--off-mmr` 是**评估脚本参数**，用于关闭对应层做对照，
不是 CLI 参数。离线 mock 模式（不传 `--online`）用确定性假向量，只验证管线逻辑与
融合顺序，不代表召回质量。

⚠️ 批量评估前必须设 `MINIRAG_EMBED_KEEP_ALIVE=10m`，否则每条重新加载 2.5GB
embedding 模型，30 次内存冲击会把 Ollama 搞崩（WinError 10061，崩前数据全丢）。

4. 零幻觉四道防线（逐层可验证）

| # | 防线 | 可验证性 | 当前状态 |
|---|---|---|---|
| 1 | 语料证据闸（查询级，零模型） | N01/N02/N05 命中，17~38ms | ✅ |
| 2 | DENSE_MIN 硬阈值短路 | 正例最低 0.706 / 负例最高 0.436，无重叠 | ✅ |
| 3 | LLM 语义拒答（SYSTEM_PROMPT 规则 2） | N03/N04 命中 | ✅ |
| 4 | L3 生成后校验 → 降级原文摘录 | 引用越界 / 推断话术 / 命令逐字比对 / 版本号 | ✅ |
| — | 引用溯源（OSC 8 file:/// + #page=N） | 程序化生成，天然 100% | ✅ |

端到端 5 条负例真机验证：**5/5 全绿**。

5. 已知 miss（已诊断，明确接受，勿重复排查）

| Case | 根因 | 决策 |
|---|---|---|
| P08 | BM25 长度归一 + 低 IDF 歧视长步骤操作手册：GT 文档单词 `enclosure` BM25 仅 rank28-33（5.05），被安装手册/规格页高密度块（5.27~5.53）压制，融合后 GT 只剩 dense 单票 vs 对手双路票 → 挤出 top10 | 接受残留。修复候选：file_name 文件级加权（推荐）/ FTS5 列加权（要 rebuild）/ dense rank1 保底票 |
| P15 | 版本号共识 vs 单票（`4-3-0-0` 文件名 vs `4.3.0.0` 查询，连字符错配），票数封顶救不了 | 维持残留 |
| P02 | 检索 hit@1 正确，但上下文无完整更换步骤 → LLM 按规则 2 拒答 | **已拍板维持严格**：零幻觉优先于可用性 |

6. 换语料后的强制动作

所有阈值与语料强绑定，换语料必须重标定，否则数字无意义：
`DENSE_MIN(0.60)` / `SPARSE_MIN(8.0)` / 证据闸规则 / `RRF_DOC_VOTE_CAP(3)` /
`SPARSE_UBIQUITOUS_RATIO(0.10)`。标定脚本见 `_build/calib_leak.py`。
当前基线只对 Powerstore 60 文件采样（3469 chunk）成立。
