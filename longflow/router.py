"""轻量总路由 Agent（Router）。

总路由只负责"看懂目标 + 选领域 + 判断风险/缺失 + 决定是否需要用户"，
**不**做网页搜索、工具执行、购买/付款、最终业务分析或价值判断。

输出固定结构（RouteDecision）：
  mode: activate_domain | clarify | multi_domain | handoff | await_confirmation
  domains: [{id, confidence}]
  known_slots / missing_slots / risk / needs_user / question / options / reason

路由不只是关键词：综合领域注册的触发词、语义提示重叠、实体抽取（金额/时间/地点）、
上下文（多轮已选领域）、领域能力与风险元数据与置信度阈值。
低置信度（无领域过阈值 / 多领域接近）→ clarify/multi_domain，而不是硬选。
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any

from . import subagents as sa
from .domains import DomainRegistry, DomainPack, load_default_registry

# 模式
MODE_ACTIVATE = "activate_domain"
MODE_CLARIFY = "clarify"
MODE_MULTI = "multi_domain"
MODE_HANDOFF = "handoff"
MODE_AWAIT = "await_confirmation"

# 置信度阈值：低于此视为不确定
CONF_DOMAIN = 0.62      # 单领域激活阈值
CONF_MULTI_GAP = 0.20   # 前两名差距小于此 -> 多领域/澄清
CONF_MULTI_MIN = 0.50   # 多领域中每个都需达到的最低置信

# 高风险信号（资金/下单/发送/删除/发布/权限）
_HIGH_RISK = ["下单", "购买", "采购", "付款", "支付", "转账", "发送", "通知", "发布",
              "删除", "清空", "授权", "审批", "退款", "报销"]
_MED_RISK = ["比较", "对比", "推荐", "选哪个", "分析"]
_HANDOFF_HINTS = ["人工", "真人", "客服", "转人", "投诉到底"]

# 金额/时间/地点实体（用于通用槽位预提取与风险判断）
_MONEY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万元|元|块|k|K|千|万)")
_TIME_RE = re.compile(r"(\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2})?|\d{1,2}\s*月\s*\d{1,2}?日?|今天|明天|本周|下周)")
_LOC_RE = re.compile(r"([一-龥A-Za-z]{2,8}?)(?:\d*\s*(?:公里|km|千米))?(?:附近|周边|一带)|(?:在|去|到)([一-龥A-Za-z]{2,8}?)(?:\d+\s*(?:公里|km|千米))?(?:找|内|的)")


@dataclasses.dataclass
class RouteDecision:
    mode: str
    domains: list[dict]
    known_slots: dict
    missing_slots: list[dict]
    risk: str
    needs_user: bool
    question: str = ""
    options: list[dict] = dataclasses.field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class RouterAgent:
    def __init__(self, registry: DomainRegistry | None = None) -> None:
        self.registry = registry or load_default_registry()

    # ---- 通用实体/槽位预提取（不绑定领域） ----
    @staticmethod
    def extract_entities(text: str) -> dict:
        slots: dict[str, Any] = {}
        m = _MONEY_RE.search(text or "")
        if m:
            num = float(m.group(1)); unit = m.group(2)
            if unit == "万": num *= 10000
            elif unit in ("千", "k", "K"): num *= 1000
            elif unit == "万元": num *= 10000
            slots["budget"] = num
        if _TIME_RE.search(text or ""):
            slots["time"] = _TIME_RE.search(text).group(0)
        lm = _LOC_RE.search(text or "")
        if lm:
            slots["location"] = lm.group(1) or lm.group(2)
        return slots

    # 不可逆动作信号：真正要执行（下单/付款/发送/删除/发布），而非询问/对比规则
    _IRREVERSIBLE = ["下单", "购买", "买", "付款", "支付", "转账", "发送", "发通知",
                     "通知", "发布", "删除", "清空", "退款", "提交报销", "提交采购",
                     "申请报销", "我要采购", "帮我采购", "去采购"]
    _QUERY_ONLY = ["规则", "政策", "标准", "是什么", "怎么", "如何", "对比", "比较",
                   "区别", "吗？", "吗?", "？", "?"]

    @classmethod
    def _assess_risk(cls, text: str, domain: DomainPack | None) -> str:
        t = text or ""
        action = any(h in t for h in cls._IRREVERSIBLE)
        query = any(h in t for h in cls._QUERY_ONLY)
        if action and not query:
            return "high"
        if any(h in t for h in _HIGH_RISK) and action:
            return "high"
        if any(h in t for h in _MED_RISK) or query:
            return "medium"
        return domain.risk if domain else "low"

    def route(self, text: str, *, context: dict | None = None) -> RouteDecision:
        text = (text or "").strip()
        context = context or {}

        # 0) 显式人工
        if any(h in text for h in _HANDOFF_HINTS):
            return RouteDecision(
                mode=MODE_HANDOFF, domains=[], known_slots={}, missing_slots=[],
                risk="high", needs_user=True,
                question="您要求人工处理。若系统未配置人工渠道，将如实告知无法转接。",
                reason="命中人工转接信号")

        # 1) 领域打分
        scored = self.registry.score(text, context=context)
        entities = self.extract_entities(text)
        # 领域相关槽位（物品/类别等）复用确定性抽取，与编排层保持一致
        domain_slots: dict = {}
        if scored:
            try:
                from .llm import LocalDriver
                domain_slots = LocalDriver.extract_slots(text, scored[0][0].slots) or {}
            except Exception:
                domain_slots = {}
        # 合并上下文已知槽位（上下文优先，再叠加本轮抽取）
        known_slots = {**entities, **domain_slots, **(context.get("known_slots") or {})}

        if not scored:
            # 无任何领域信号 -> 澄清（不硬选）
            return RouteDecision(
                mode=MODE_CLARIFY, domains=[], known_slots=known_slots, missing_slots=[],
                risk="low", needs_user=True,
                question="我没能确定这属于哪个领域。请告诉我您想做什么，或选择一个领域：",
                options=[{"id": p.id, "label": p.name, "hint": p.description[:40]}
                         for p in self.registry.all()],
                reason="无领域置信信号")

        top = scored[0]
        second = scored[1] if len(scored) > 1 else None
        top_pack, top_conf = top
        risk = self._assess_risk(text, top_pack)

        # 2) 多领域：前两名都够强且差距小
        is_multi = bool(second and top_conf >= CONF_MULTI_MIN
                        and second[1] >= CONF_MULTI_MIN
                        and (top_conf - second[1]) < CONF_MULTI_GAP)
        # 2a) 多领域且高风险：安全优先，先确认动作，不让用户在危险操作下选领域
        if is_multi and risk == "high":
            return RouteDecision(
                mode=MODE_AWAIT,
                domains=[{"id": p.id, "confidence": c} for p, c in scored[:3]],
                known_slots=known_slots, missing_slots=[], risk=risk, needs_user=True,
                question="该任务涉及高风险/不可逆动作，执行前需要您确认。",
                options=[{"id": "confirm", "label": "确认继续"},
                         {"id": "modify", "label": "调整范围/预算"},
                         {"id": "cancel", "label": "取消"}],
                reason="多领域且命中高风险信号，优先请求用户确认")
        if is_multi:
            doms = [{"id": p.id, "confidence": c} for p, c in scored[:3] if c >= CONF_MULTI_MIN]
            return RouteDecision(
                mode=MODE_MULTI, domains=doms, known_slots=known_slots, missing_slots=[],
                risk=risk, needs_user=True,
                question="这件事可能涉及多个领域，请确认主要方向（可多选并行处理）：",
                options=[{"id": p.id, "label": p.name, "confidence": c} for p, c in scored[:3]],
                reason=f"多领域置信接近（{top_conf} vs {second[1]}）")

        # 4) 单领域但置信不足 -> 澄清
        if top_conf < CONF_DOMAIN:
            return RouteDecision(
                mode=MODE_CLARIFY,
                domains=[{"id": top_pack.id, "confidence": top_conf}],
                known_slots=known_slots, missing_slots=[], risk=risk, needs_user=True,
                question=f"我不太确定您是不是要处理「{top_pack.name}」，请确认或补充您的目标：",
                options=[{"id": p.id, "label": p.name, "confidence": c} for p, c in scored[:3]],
                reason=f"最高置信 {top_conf} 低于阈值 {CONF_DOMAIN}")

        # 5) 领域明确：检查关键槽位缺失（只问影响范围/结论的）
        missing = self._missing_slots(top_pack, text, known_slots)
        if missing:
            return RouteDecision(
                mode=MODE_CLARIFY,
                domains=[{"id": top_pack.id, "confidence": top_conf}],
                known_slots=known_slots, missing_slots=missing, risk=risk, needs_user=True,
                question=missing[0].get("prompt", "请补充必要信息"),
                options=[],
                reason=f"领域 {top_pack.id} 关键槽位缺失: {[m['name'] for m in missing]}")

        # 6) 高风险/不可逆：关键信息齐备后仍须用户确认（不自行执行）
        if risk == "high":
            return RouteDecision(
                mode=MODE_AWAIT,
                domains=[{"id": top_pack.id, "confidence": top_conf}],
                known_slots=known_slots, missing_slots=[], risk=risk, needs_user=True,
                question="该任务涉及高风险/不可逆动作，执行前需要您确认。",
                options=[{"id": "confirm", "label": "确认继续"},
                         {"id": "modify", "label": "调整范围/预算"},
                         {"id": "cancel", "label": "取消"}],
                reason="命中高风险信号，需用户确认后执行")

        return RouteDecision(
            mode=MODE_ACTIVATE,
            domains=[{"id": top_pack.id, "confidence": top_conf}],
            known_slots=known_slots, missing_slots=[], risk=risk, needs_user=False,
            reason=f"激活领域 {top_pack.id}（置信 {top_conf}）")

    def _missing_slots(self, pack: DomainPack, text: str, known: dict) -> list[dict]:
        """只返回会影响任务范围/工具选择/结论的缺失槽位（每轮最多 2 个）。"""
        missing = []
        intent = self._intent_of(pack, text)
        for slot in pack.slots:
            name = slot.get("name")
            if known.get(name) or _slot_in_text(slot, text):
                continue
            required_for = slot.get("required_for", [])
            # required_for 为空表示非必填；命中当前意图才必填
            if required_for and (intent in required_for or "*" in required_for):
                missing.append({"name": name, "prompt": slot.get("prompt", f"请提供 {name}"),
                                "type": slot.get("type", "text")})
        return missing[:2]

    @staticmethod
    def _intent_of(pack: DomainPack, text: str) -> str | None:
        kw = pack.scenario_cfg.get("intent_keywords", {}) or {}
        best, best_n = None, 0
        for intent, words in kw.items():
            n = sum(1 for w in words if w in text)
            if n > best_n:
                best, best_n = intent, n
        return best


def _slot_in_text(slot: dict, text: str) -> bool:
    """轻量判断：枚举/布尔类槽位是否已在文本中出现其取值。"""
    for val in slot.get("enum", []) or []:
        if str(val) and str(val) in text:
            return True
    return False
