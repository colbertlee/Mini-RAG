DOC-06: 用户交互指南与溯源使用手册
文件编号: USER-GUIDE-006
版本: v2.0.0（与实现同步 2026-09-04，此前 v1.1.0 为立项规划态）
适用范围: 终端用户使用操作、溯源校验及故障排查指引
对应实现: mini_rag/cli.py

> v1.1.0 写的是 `rag_tool.py --folder ... --reindex`，**该命令不存在**，
> 模型名也停留在 qwen2.5:7b-instruct。本版全部替换为真实命令。

1. 环境准备

```bash
ollama pull qwen3-embedding:4b     # 向量化（2560 维）
ollama pull qwen3.5:4b             # 生成

python -m mini_rag.cli status      # 确认模型、切分档位、语料目录、索引规模
```

`status` 不加载模型，可随时执行，是排查问题的第一步。

2. 命令一览

| 命令 | 用途 | 常用参数 |
|---|---|---|
| `status` | 状态总览（模型 / 阈值 / 索引规模 / 语料目录） | — |
| `index` | 构建 / 增量索引 | `--rebuild` 清空重建 · `--limit N` 只处理前 N 个 · `--update` 强制重建 · `-v` 逐文件明细 |
| `preview <文件>` | 单文件 解析→清洗→切片 预览（不加载模型） | `-n N` 打印前 N 块 · `--segments N` 看解析 block |
| `ask "<问题>"` | 单次问答 | `--debug` 打印召回明细 · `--top-n N` 指定进 LLM 的片段数 |
| `chat` | 交互式问答 | — |
| `purge` | 清空索引 | `--yes` 跳过确认 |

```bash
# 首次使用：改语料目录（settings.py 的 INCLUDE_DIRS）后建索引
python -m mini_rag.cli index --limit 5 --verbose   # 先小批量试
python -m mini_rag.cli index                        # 再全量

# 提问
python -m mini_rag.cli ask "如何配置快照？"
python -m mini_rag.cli ask "svc_factory_reset 的参数有哪些？" --debug
```

⚠️ **建索引前先腾内存到 4GB 以上**。可用内存 <2.5GB 时 embedding 吞吐从 6 chunks/s
掉到 0.4 chunks/s，60 个文件可能啃一小时。

3. 问答场景

3.1 命中知识库（P01 类）

```
>>> 如何使用 svc_factory_reset 命令将 PowerStore 系统重置为出厂状态？

[AI 回答]:
可以使用 svc_factory_reset 命令将系统重置为出厂状态。

```bash
svc_factory_reset -h
svc_factory_reset -p <password> -c
```
（结论 + 参数 + 命令块 + 4 条引用，L3 校验放行）
```

3.2 零幻觉拒答（负例）

```
>>> 如何在 Brocade 交换机上配置 zone？

[AI 回答]:
知识库中未找到相关信息。
```

拒答有四种来源，**都是正确行为**：

| 拒答来源 | 触发条件 | 响应速度 |
|---|---|---|
| 证据闸 | 查询点名的专名在语料中零出现（如 Brocade） | 17~38 ms（不等 embedding） |
| 硬阈值短路 | 所有候选相似度 < `DENSE_MIN` | ~0.2 s |
| LLM 语义拒答 | 检索有内容但 LLM 判定与问题无关 / 信息不完整 | 含生成时间 |
| L3 降级 | 生成内容疑似超出上下文 → **降级为原文摘录**（不是拒答） | 含生成时间 |

3.3 ⚠️ 读输出时注意：显示「检索: 0 片段」≠ 检索 0 命中

`generate()` 一旦判定为拒答路径，会清空 `retrieved` / `citations` 再展示。
所以 CLI 打印「检索: 0 片段」实际含义是「本次走拒答路径，抑制了引用展示」，
**不代表检索层真的没召回**。要看真实召回，加 `--debug`。

4. 三步溯源核验证法 (Verification Protocol)

       [步骤 1]                    [步骤 2]                   [步骤 3]
  查看回答底部的              根据标注的页码            确认官方文档中的
【参考来源】文档名称   ──►   打开本地原版 PDF/MD   ──►   【高风险警示 / Warning】
                              (Ctrl+Click 直达)         及前置依赖条件

- **核对来源**：确认引用文档名与产品线一致（PowerStore ≠ Unity ≠ PowerScale）。
- **快速跳转**：引用是 OSC 8 超链接，终端 Ctrl+Click 直达原文件对应页
  （`file:///` URI + `#page=N`，链接目标与中文显示名分离）。
- **安全确认**：检查原文档中该命令是否会导致节点重启 / 数据丢失。
  上下文含 WARNING 时，答案会在命令前单独一行加粗输出 **【高风险警示】**。

引用由系统**程序化生成**（不是 LLM 自造），因此不会出现「看起来很像但不存在」的引用。

5. 性能与开关

| 场景 | 建议 |
|---|---|
| 高频实时查询 | 在 `settings.py` 设 `HYDE_ENABLED = False`，省掉约 25 s/query 的 HyDE 与模型加载开销 |
| 短查询 / 口语化查询 | 保持 `HYDE_ENABLED = True` 召回更好 |
| 批量评估 | 设环境变量 `MINIRAG_EMBED_KEEP_ALIVE=10m`，否则 Ollama 会被反复加载拖崩 |
| 上下文够大 | 可调 `FINAL_TOP_N`（默认 4）到 6~8 |

⚠️ `--off-hyde` / `--off-rewrite` / `--off-mmr` 是 `scripts/eval_retrieval.py`
的评估参数，**不是 CLI 参数**。生产环境开关请改 `settings.py`。

6. 常见问题排查 (FAQ)

**Q: 为什么提示“知识库中未找到相关信息”，但我文档里确实有这个命令？**

1. 先加 `--debug` 看真实召回 —— 「检索: 0 片段」可能是拒答路径的显示抑制（见 3.3）。
2. **换语料 / 新加文档后没有重建索引**：增量用 `index`，改过 schema 必须 `index --rebuild`。
3. **Chroma schema 未迁移**：改 metadata 字段后不 rebuild，`where={'is_parent': False}`
   会全部空匹配返回 0 条 —— dense 主路变空气，系统静默走 sparse 兜底，看似正常实则残废。
4. 该 PDF 是**扫描图片版**：无文字层会走 OCR，未装 rapidocr 则整页跳过（看 `logs/`）。
5. 查询用了**英文专名但语料里没有**：这是证据闸在正常工作（知识库确实没覆盖）。

**Q: 为什么 P02 这类问题检索命中了还是拒答？**

这是**刻意设计**。召回内容只有告警与识别说明、没有完整操作步骤时，
LLM 按 SYSTEM_PROMPT 规则 2 拒答。零幻觉严格性优先于可用性（见 DOC-03 第 2.1 节）。
对照 P01（svc_factory_reset）能完整作答，说明链路健康。

**Q: Ollama 连接超时 / 拒绝连接（WinError 10061）？**

1. 确认 `ollama list` 能正常输出、监听 `http://127.0.0.1:11434`。
2. 若是批量跑评估时崩的 —— 内存冲击导致，设 `MINIRAG_EMBED_KEEP_ALIVE=10m` 重跑。
3. 代理问题：`settings.py` 已设 `NO_PROXY=127.0.0.1,localhost`，若仍 502 检查系统代理。

**Q: 索引很慢？**

看 `status` 的 chunk 数与可用内存。<2.5GB 会掉到 0.4 chunks/s。
先腾内存到 4GB 以上，或分批 `--limit` 跑。
