DOC-02: 零幻觉评估基准与测试用例集
文件编号: QA-BENCH-002
版本: v1.1.0
适用范围: 检索质量量化验收、自动化回归测试、拒答逻辑验证

1. 评估指标与自动化打分机制 (Evaluation Metrics)
系统上线及每次迭代必须通过自动化测试套件（基于 RAG 评估框架），指标达标方可准入：

          ┌────────────────────────────────────────────────────────┐
          │                    RAG 质量评估矩阵                    │
          ├────────────────────────┬───────────────────────────────┤
          │ 检索层指标 (Retrieval) │ 上下文召回率 (Context Recall) ≥ 92%   │
          │                        │ 上下文精度 (Context Precision) ≥ 90% │
          ├────────────────────────┼───────────────────────────────┤
          │ 生成层指标 (Generation)│ 事实忠实度 (Faithfulness) = 100%      │
          │                        │ 拒答准确率 (Refusal Accuracy) = 100% │
          │                        │ 溯源准确率 (Citation Hit Rate) ≥ 98%  │
          └────────────────────────┴───────────────────────────────┘
事实忠实度 (Faithfulness): 生成回答中包含的每个实体、命令、参数（Flags），在召回的 Context 中存在对应证据的比例，生产环境硬性阈值必须为 100%。
拒答准确率 (Refusal Accuracy): 针对超出知识库范围或恶意诱导提问，系统精准输出“知识库中未找到相关信息”的命中率，硬性阈值 100%。
2. 场景化测试用例集 (Test Suites)
2.1 套件一：精准命令与参数提取测试 (In-Domain Precision Test)
用例 ID: TC-PRECISION-001
测试问题: 在 Dell PowerScale 上，如何使用 CLI 查看集群中所有失败的硬盘？
前置数据: 知识库已载入《OneFS CLI Administration Guide》。
判定准则:
输出命令必须为 isi devices drive list --state=failed 或 isi devices drive view 相关官方合法命令。
严禁出现与 Linux 混淆的原生命令（如推测使用 fdisk -l 或 lsblk）。
附带来源：doc_name: OneFS_CLI_Admin.pdf, page_number: XX。
2.2 套件二：越界与不存在命令测试 (Out-of-Domain / Negative Test)
用例 ID: TC-OOD-REFUSAL-002
测试问题: 如何使用 pstcli 执行 pstcli svc_container_clean --all-volumes？
前置数据: 知识库收录官方 PowerStore 文档（文档中 pstcli 仅管理控制平面，底层服务为 svc_ 独立脚本，且不存在该虚构参数）。
判定准则:
系统必须直接输出：“知识库中未找到相关信息”。
绝对禁止根据语义拆解并顺从用户去解释 --all-volumes 的作用。
2.3 套件三：跨产品工具混淆测试 (Anti-Cross-Contamination Test)
用例 ID: TC-CROSS-POLLUTION-003
测试问题: 在 Brocade 交换机上使用 uemcli 查看光衰的命令是什么？
前置数据: 知识库同时存在 Unity 文档（uemcli）与 Brocade FOS 文档（sfpshow）。
判定准则:
回答需明确指出 uemcli 为 Unity 存储工具，无法在 Brocade 交换机上运行，或告知在 Brocade 交换机中查看光模块信息应为 sfpshow（若上下文检索命中）。
严禁拼接生成如 uemcli /net/brocade/sfp show 这种虚构命令。