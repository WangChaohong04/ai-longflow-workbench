"""Domain Registry / Domain Pack —— 领域注册机制（核心不写死任何领域）。

领域通过"领域包"注册：可来自 config/scenarios/*.yaml（声明式）或插件（编程式）。
核心总路由器只面对 DomainPack 抽象，不 import 任何具体领域逻辑；新增领域只需
增加领域包/插件，不修改核心编排。

DomainPack 在既有场景配置（intent_keywords/slots/policies/subtask_templates）之上，
补充总路由与治理所需的通用元数据：语义描述、可调用的固定 subagent 白名单、
工具白名单、风险规则、暂停条件、最终输出结构。
"""
from __future__ import annotations

import dataclasses
import glob
import os
import re
from typing import Any

import yaml

from . import config as cfg_mod
from . import subagents as sa


@dataclasses.dataclass
class DomainPack:
    id: str
    name: str
    description: str
    trigger_keywords: list[str]            # 触发词（含意图关键词展开）
    semantic_hints: list[str]              # 语义描述/同义词（供置信度打分，非纯关键词）
    slots: list[dict]                      # 通用类型化槽位定义
    subagents: list[str]                   # 允许调用的固定能力 subagent 白名单
    tools: list[str] = dataclasses.field(default_factory=list)  # 领域可用工具（空=默认）
    risk: str = "low"                      # 领域默认风险基线 low|medium|high
    pause_conditions: list[str] = dataclasses.field(default_factory=list)
    output_schema: dict = dataclasses.field(default_factory=dict)
    scenario_cfg: dict = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d.pop("scenario_cfg", None)  # 场景原始配置不进路由输出
        return d


class DomainRegistry:
    def __init__(self) -> None:
        self._domains: dict[str, DomainPack] = {}

    def register(self, pack: DomainPack, *, on_conflict: str = "error") -> None:
        if pack.id in self._domains:
            if on_conflict == "override":
                pass
            elif on_conflict == "error":
                raise ValueError(f"领域重复注册: {pack.id}")
            else:
                return
        # 校验 subagent 白名单都属于系统固定能力
        for s in pack.subagents:
            if sa.get_spec(s) is None:
                raise ValueError(f"领域 {pack.id} 引用了未知固定 subagent: {s}")
        self._domains[pack.id] = pack

    def get(self, domain_id: str) -> DomainPack | None:
        return self._domains.get(domain_id)

    def all(self) -> list[DomainPack]:
        return list(self._domains.values())

    # ---- 置信度打分：综合触发词 + 语义提示 token 重叠 + 实体/上下文 ----
    def score(self, text: str, *, context: dict | None = None) -> list[tuple[DomainPack, float]]:
        text_l = (text or "").lower()
        scored: list[tuple[DomainPack, float]] = []
        for pack in self._domains.values():
            hits = 0.0
            matched: list[str] = []
            for kw in pack.trigger_keywords:
                if kw and kw.lower() in text_l:
                    hits += 1.0
                    matched.append(kw)
            # 意图名本身（如"采购/选址"）作为触发词
            for intent in (pack.scenario_cfg.get("intent_keywords", {}) or {}).keys():
                if len(intent) >= 2 and intent in text:
                    hits += 0.8
                    matched.append(f"intent:{intent}")
            # 语义提示：按字符/词重叠给部分分（弱信号），避免纯精确关键词的硬边界
            hint_overlap = 0
            for hint in pack.semantic_hints:
                for tok in _hint_tokens(hint):
                    if len(tok) >= 2 and tok.lower() in text_l:
                        hint_overlap += 1
            hits += min(hint_overlap, 3) * 0.3
            # 上下文已指定领域（如多轮）给强先验
            if context and context.get("domain") == pack.id:
                hits += 2.0
            if hits > 0:
                # 归一到 0..1：触发词命中越多置信越高，封顶 0.98
                conf = min(0.72 + 0.12 * max(0.0, hits - 1.0), 0.98)
                scored.append((pack, round(conf, 3)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored


def _hint_tokens(hint: str) -> list[str]:
    return [t for t in re.split(r"[\s,，、/|]+", hint) if t]


# ---------- 从场景 YAML 装载领域包 ----------

# 领域扩展默认值：可在场景 YAML 用 domain: 段覆盖
_DEFAULT_SUBAGENTS = [
    sa.SA_RAG_RESEARCHER, sa.SA_OFFICIAL_RESEARCHER, sa.SA_NORMALIZER,
    sa.SA_EVIDENCE_VERIFIER,
]
_DEFAULT_PAUSE = [
    "关键槽位缺失", "证据冲突", "证据不足/来源过期", "subagent 连续失败或超时",
    "超出领域工具权限", "需要新增预算/数量/时间/范围/授权", "不可逆高风险动作",
]


def pack_from_scenario(cfg: dict) -> DomainPack:
    """把既有场景配置封装成 DomainPack（向后兼容：无 domain 段时用合理默认）。"""
    did = cfg.get("name", "unknown")
    triggers: list[str] = []
    for words in (cfg.get("intent_keywords", {}) or {}).values():
        triggers.extend(words)
    dom = cfg.get("domain", {}) or {}
    semantic = list(dom.get("semantic_hints", []))
    # 描述与意图名也作为弱语义提示
    if cfg.get("description"):
        semantic.append(cfg["description"])
    semantic.extend((cfg.get("intent_keywords", {}) or {}).keys())
    slots = _typed_slots(cfg.get("slots", []) or [])
    sub = dom.get("subagents") or _DEFAULT_SUBAGENTS
    return DomainPack(
        id=did,
        name=dom.get("name", did),
        description=cfg.get("description", ""),
        trigger_keywords=triggers,
        semantic_hints=semantic,
        slots=slots,
        subagents=sub,
        tools=dom.get("tools", []),
        risk=dom.get("risk", "low"),
        pause_conditions=dom.get("pause_conditions", _DEFAULT_PAUSE),
        output_schema=dom.get("output_schema", {}),
        scenario_cfg=cfg,
    )


def _typed_slots(raw_slots: list[dict]) -> list[dict]:
    """给槽位补通用类型（缺省 text）；类型见 slots.py 支持集合。"""
    out = []
    for s in raw_slots:
        s2 = dict(s)
        s2.setdefault("type", "text")
        out.append(s2)
    return out


def pack_from_dict(data: dict) -> DomainPack:
    """把插件返回的 dict 归一为 DomainPack（字段缺失给安全默认，subagent 白名单仍校验）。"""
    required = ["id", "name", "description"]
    for k in required:
        if not data.get(k):
            raise ValueError(f"插件领域包缺少字段: {k}")
    return DomainPack(
        id=str(data["id"]),
        name=str(data["name"]),
        description=str(data.get("description") or data["name"]),
        trigger_keywords=list(data.get("trigger_keywords") or data.get("keywords") or []),
        semantic_hints=list(data.get("semantic_hints") or data.get("hints") or []),
        slots=_typed_slots(data.get("slots") or []),
        subagents=list(data.get("subagents") or []),
        tools=list(data.get("tools") or []),
        risk=str(data.get("risk") or "low"),
        pause_conditions=list(data.get("pause_conditions") or []),
        output_schema=dict(data.get("output_schema") or {}),
        scenario_cfg=dict(data.get("scenario_cfg") or {}),
    )


def load_default_registry(scenarios_dir: str | None = None,
                          plugin_domains: list | None = None) -> DomainRegistry:
    """扫描 config/scenarios/*.yaml，并合并插件声明的领域包。

    核心不写死任何领域：领域既可来自 YAML（声明式），也可来自可信本地插件（编程式）。
    """
    reg = DomainRegistry()
    scenarios_dir = scenarios_dir or str(cfg_mod.SCENARIOS_DIR)
    for path in sorted(glob.glob(os.path.join(scenarios_dir, "*.yaml"))):
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        if not cfg.get("name"):
            continue
        reg.register(pack_from_scenario(cfg), on_conflict="override")
    for item in plugin_domains or []:
        pack = item if isinstance(item, DomainPack) else pack_from_dict(dict(item))
        reg.register(pack, on_conflict="override")
    return reg
