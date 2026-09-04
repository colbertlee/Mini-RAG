"""L1 Query Rewriting —— 同义词扩展 + 关键术语翻译。

零额外模型、纯词典法。针对跨语言结构性失效问题：
  语料：全英文 Dell PowerStore 文档
  查询：中文
  → 中文「快照」对应的英文术语是 "snapshot"，FTS5 救不了语义。
  → 用词典把 "快照" 翻成 "snapshot" 参与 sparse 路；dense 仍然原文。

扩展方式：
  1. 中文 → 英文术语映射（快照:snapshot 等）
  2. 英文命令 → 同义命令归一（svc_db_recovery:db_recovery）
  3. 关键词 → 同义词组（BBU:battery backup unit）
  4. 拆分长查询为子查询（用 "和"/"、" 分隔）

返回原 query 与展开后的多 query 列表，原 query 永远保留（dense 主力）。
"""
from __future__ import annotations

import re

from mini_rag.config import settings


# 中英术语映射（只收强信号词——多义词不收，避免误翻）
# 弱信号动词（查看/状态等）例外放行：rewrite 是 additive 的，多产一个 query
# 只会多花一次 embedding，不会破坏主路（dense 有 DENSE_MIN 阈值把关），
# 收益（召回命中）大于风险（多 ~1s latency）。
_ZH_TO_EN = {
    "快照": "snapshot",
    "克隆": "clone",
    "复制": "replication",
    "复制技术": "replication",
    "卷": "volume",
    "精简": "thin",
    "精简克隆": "thin clone",
    "存储": "storage",
    "存储池": "storage pool",
    "共享": "share",
    "文件共享": "file sharing",
    "升级": "upgrade",
    "降级": "downgrade",
    "健康检查": "health check",
    "告警": "alert",
    "事件": "event",
    "日志": "journal log",
    "硬件": "hardware",
    "更换": "replace replacement",
    "电池": "BBU battery",
    "电池组": "BBU",
    "BBU": "BBU battery backup",
    "节点": "node",
    "模块": "module",
    "机箱": "enclosure",
    "扩展柜": "expansion enclosure",
    "底座": "base enclosure",
    "IO 模块": "IO module",
    "接口卡": "port card",
    "4 端口卡": "4-port card",
    "集群": "cluster",
    "镜像": "mirror replication",
    "VMware 集成": "VMware vSphere virtualization",
    "快照策略": "snapshot policy",
    "快照计划": "snapshot schedule",
    "导入": "import",
    "外部存储": "external storage",
    "容量": "capacity capacity expansion",
    "扩容": "expansion capacity",
    "性能": "performance",
    "监控": "monitor",
    "REST API": "REST API",
    "命令行": "CLI command",
    "脚本": "script",
    "服务脚本": "service script svc",
    "出厂": "factory",
    "出厂状态": "factory state",
    "重置": "reset factory reset",
    "故障": "fault faulted failure",
    "故障处理": "troubleshoot",
    "卸载": "unmount detach",
    "挂载": "mount attach",
    "备份": "backup",
    "恢复": "recovery restore",
    "时间同步": "NTP time sync",
    "时间偏差": "time skew",

    # —— 2026-09-04 扩充：通用运维动词 / 产品词（原缺失，导致多类查询零变体）——
    "失败": "failure failed error",
    "重启": "reboot restart",
    "型号": "model model number",
    "规格": "specifications spec",
    "查看": "view check display",
    "状态": "status state",
    "变更": "change",
    "变更内容": "what's new change list",
    "版本": "version release",
    "升级失败": "upgrade failure failed upgrade",
    "安装": "install installation",
    "配置": "configure configuration",
    "创建": "create",
    "删除": "delete remove",
    "端口": "port",
    "磁盘": "disk drive",
    "控制器": "controller",
    "电源": "power supply PSU",
    "风扇": "fan",
    "温度": "temperature thermal",
    "许可证": "license",
    "密码": "password",
    "证书": "certificate",
    "主机": "host",
    "保护": "protection",
    "迁移": "migration migrate",
}

# 英文命令归一：svc_xxx ↔ xxx（PowerStore 服务脚本大量用下划线形式，
# 但 KB 文章里常脱去 svc_ 前缀。归一后用纯词匹配可命中两种写法）
_CMD_NORM = re.compile(r"^svc[_\-](.+)$", re.I)

# 一条 query 最多翻译几个中文术语（防止词袋过散，embedding 语义被稀释）
_MAX_TERMS = 4


def _normalize_svc(name: str) -> list[str]:
    """给一个 svc_xxx 命令返回多种变体。"""
    m = _CMD_NORM.match(name.strip())
    if m:
        body = m.group(1)
        return [name.strip(), body, body.replace("_", " ")]
    return [name.strip()]


def rewrite(query: str) -> list[str]:
    """返回多 query 列表，原 query 永远在第一位（dense 主力）。

    策略：
      1. 原 query 保留
      2. 中文术语 → 英文（构造英文版 query 参与 dense 召回）
      3. svc_xxx 命令归一
      4. 长 query 按 "和"/"、" 拆分（多意图）
    """
    out: list[str] = [query]
    seen: set[str] = {query}

    def _add(q: str) -> None:
        q = q.strip()
        if q and q not in seen and len(q) >= 2:
            seen.add(q)
            out.append(q)

    # 1. 中文 → 英文（仅当 query 含中文且匹配术语表）
    #    关键：英文专名（PowerStore / 版本号 / svc_xxx / NAS server）必须保留。
    #    旧实现只输出中文术语的英文翻译，把 query 里的英文强信号全丢了 ——
    #    "PowerStore 4.3.0.0 release note 变更内容" 只能翻出 "change"，
    #    "svc_journalctl 怎么看日志" 翻出 "journal log" 却丢了 svc_journalctl。
    #    这类查询的 ground_truth 恰恰就是那些英文专名，所以必须留住。
    if re.search(r"[\u4e00-\u9fff]", query):
        zh_terms = [zh for zh in _ZH_TO_EN if zh in query]
        # 按在 query 中首次出现位置排序，靠前的通常是主语（更重要）
        zh_terms.sort(key=query.index)
        if zh_terms:
            # 非中文片段 = 英文专名 / 版本号 / svc 命令 → 强信号，保留
            non_zh_tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-\.]*", query)
            non_zh = " ".join(non_zh_tokens)
            already = {t.lower() for t in non_zh_tokens}
            en_tokens: list[str] = []
            for zh in zh_terms[:_MAX_TERMS]:
                for w in _ZH_TO_EN[zh].split():
                    # 已在原 query 的非中文片段里 → 不重复（避免 "BBU ... BBU battery"）
                    if w.lower() in already:
                        continue
                    if w not in en_tokens:
                        en_tokens.append(w)
            merged = " ".join(en_tokens)
            if non_zh:
                # 主改写：英文专名 + 翻译词（最强，救 P15/P21 这类）
                _add(f"{non_zh} {merged}".strip())
            # 纯翻译版：中文全剥，语域更贴近全英文语料
            _add(merged)

    # 2. svc_xxx 命令归一
    for m in re.finditer(r"\bsvc[_\-][a-zA-Z_]+", query):
        for variant in _normalize_svc(m.group(0))[1:]:  # 第一个就是原词，跳过
            _add(variant)

    # 3. 长 query 拆分：用 "和"/"、" 分隔
    if len(query) > 20 and re.search(r"[和、]", query):
        for part in re.split(r"[和、]", query):
            part = part.strip(" ?？，,。.")
            if len(part) >= 4:
                _add(part)

    return out


def is_enabled() -> bool:
    return getattr(settings, "QUERY_REWRITE_ENABLED", True)