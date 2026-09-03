DOC-06: 用户交互指南与溯源使用手册
文件编号: USER-GUIDE-006
版本: v1.1.0
适用范围: 终端用户使用操作、溯源校验及故障排查指引

1. 快速上手步骤
第一步：准备本地运行环境
确认本地 Ollama 服务正常运行，并拉取所需模型：

# 启动 Ollama 并拉取 7B 生成模型
ollama pull qwen2.5:7b-instruct
第二步：指定知识库并完成初始化构建
指定存放运维手册或技术文档的本地目录：

# 扫描目录并构建向量索引（初次运行或更新文档时使用 --reindex）
python rag_tool.py --folder "/home/user/Storage_Docs" --reindex
2. 交互式问答与溯源功能说明
场景 1：精准技术检索与溯源（命中知识库）
用户输入一个具体的 CLI 操作问题：

>>> [用户提问]: 如何在 Brocade 交换机上查看当前所有的有效 Zone 配置？

[系统检索中... 命中 2 个切片]

[AI 回答]:
在 Brocade FOS 系统中，您可以使用 `cfgactvshow` 或 `zoneshow` 命令查看当前已激活（Defined/Effective）的 Zone 配置。

常用命令如下：
```bash
# 查看当前激活生效的 Zone 配置文件及成员
cfgactvshow

# 查看完整的 Zone 数据库（包括定义但未生效的配置）
zoneshow
【参考来源与溯源】:

文档 1: Brocade_Fabric_OS_CLI_Reference.pdf | 页码: 第 142 页 | 路径: /home/user/Storage_Docs/Brocade_Fabric_OS_CLI_Reference.pdf
文档 2: Brocade_Zoning_Guide.md | 章节: ## Effective Configuration Show

---

#### 场景 2：超出范围/未收录技术问题（零幻觉拒答）
如果用户提问了知识库中未记载的内容或虚构命令：

```text
>>> [用户提问]: 如何在 PowerScale OneFS 上执行 isi_nonexistent_command --destroy-all？

[系统检索中... 相似度低于置信阈值]

[AI 回答]:
知识库中未找到相关信息。
设计说明：系统严格遵守“零幻觉”原则。只要知识库中没有对应说明，绝不利用先验知识推测命令参数，直接返回固定提示语。

3. 三步溯源核实验证法 (Verification Protocol)
在生产环境中执行任何 AI 推荐的 CLI 指令前，请务必执行以下核对流程：

       [步骤 1]                    [步骤 2]                   [步骤 3]
  查看回答底部的              根据标注的页码            确认官方文档中的
【参考来源】文档名称   ──►   打开本地原版 PDF/MD   ──►   【高风险警示 / Warning】
                              (快捷定位到页码)           及前置依赖条件
核对来源：查看回答最底部的 【参考来源】，确认文档名称是否与对应产品线一致。
快速跳转：打开本地对应的 PDF 或 Markdown 文件，直接翻阅到标明的 页码 或 章节。
安全确认：检查官方原文档中该命令是否有破坏性影响（如是否会导致节点重启、数据丢失等），确认无误后再行实施。
4. 常见问题排查 (FAQ)
Q: 为什么系统提示“知识库中未找到相关信息”，但我的文档里确实有这个命令？
排查 1：检查是否在添加新文档后未运行 --reindex 参数。
排查 2：检查该 PDF 文件是否为“扫描图片版”（无文字图层），系统当前仅支持可复制文本的 PDF。
Q: 运行问答时提示 Ollama 连接超时？
确保终端中 ollama list 能正常输出，且 Ollama 正在监听本地端口 http://127.0.0.1:11434。