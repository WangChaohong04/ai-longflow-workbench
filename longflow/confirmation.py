"""用户确认优先原则与困难暂停机制（可控闭环核心）。

系统可自动完成低风险、可逆、已授权、证据充分的工作；遇到下列情况必须暂停并请用户
确认，不得自行猜测或替用户做不可逆决策：

1. 目标存在两种以上合理理解；
2. 缺少会明显改变范围/结论的关键信息；
3. 用户偏好/优先级/取舍不明确；
4. 不同来源存在无法自动解决的冲突；
5. 关键数据无法访问/已过期/证据不足；
6. Subagent 连续失败、超时或返回异常；
7. 任务超出当前领域能力或工具权限；
8. 需要新增预算/修改数量/时间/范围/授权；
9. 涉及资金/下单/发送/删除/发布/权限变更等不可逆动作；
10. 系统无法判断下一步是否符合用户原始意图。

暂停时用固定 5 段结构说明，不得用默认值掩盖关键不确定性：
  目前已确认的信息 / 当前遇到的问题 / 需要用户决定的事项 / 可选方案 / 每个方案的影响。
"""
from __future__ import annotations

import dataclasses
from typing import Any

# 暂停原因码（对应 10 类）
PAUSE_AMBIGUOUS_GOAL = "ambiguous_goal"
PAUSE_MISSING_INFO = "missing_info"
PAUSE_PREFERENCE = "preference_unclear"
PAUSE_CONFLICT = "evidence_conflict"
PAUSE_NO_EVIDENCE = "insufficient_evidence"
PAUSE_SUBAGENT_FAILED = "subagent_failed"
PAUSE_OUT_OF_SCOPE = "out_of_scope"
PAUSE_SCOPE_CHANGE = "scope_change"
PAUSE_IRREVERSIBLE = "irreversible_action"
PAUSE_INTENT_UNSURE = "intent_unsure"

PAUSE_REASONS = {
    PAUSE_AMBIGUOUS_GOAL: "目标存在多种合理解释",
    PAUSE_MISSING_INFO: "缺少影响范围或结论的关键信息",
    PAUSE_PREFERENCE: "用户偏好/优先级/取舍不明确",
    PAUSE_CONFLICT: "来源间存在无法自动裁决的冲突",
    PAUSE_NO_EVIDENCE: "关键数据无法访问/已过期/证据不足",
    PAUSE_SUBAGENT_FAILED: "Subagent 连续失败、超时或异常",
    PAUSE_OUT_OF_SCOPE: "任务超出当前领域能力或工具权限",
    PAUSE_SCOPE_CHANGE: "需要新增预算/数量/时间/范围/授权",
    PAUSE_IRREVERSIBLE: "涉及资金/下单/发送/删除/发布/权限变更等不可逆动作",
    PAUSE_INTENT_UNSURE: "无法判断下一步是否符合用户原始意图",
}


@dataclasses.dataclass
class ConfirmationOption:
    id: str
    label: str
    impact: str = ""           # 该方案的影响（必填，让用户知情）
    risk: str = "low"          # low|medium|high

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ConfirmationRequest:
    """5 段式用户确认请求。"""
    reason_code: str
    reason_label: str
    confirmed_info: list[str]          # 目前已确认的信息
    problem: str                       # 当前遇到的问题
    decision_needed: str               # 需要用户决定的事项
    options: list[ConfirmationOption]  # 可选方案 + 各方案影响
    question: str = ""                 # 一句话提问
    context: dict = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "reason_code": self.reason_code,
            "reason_label": self.reason_label,
            "confirmed_info": self.confirmed_info,
            "problem": self.problem,
            "decision_needed": self.decision_needed,
            "question": self.question or self.decision_needed,
            "options": [o.to_dict() for o in self.options],
            "context": self.context,
        }


def build_confirmation(
    reason_code: str,
    *,
    confirmed_info: list[str] | None = None,
    problem: str = "",
    decision_needed: str = "",
    options: list[ConfirmationOption] | None = None,
    question: str = "",
    context: dict | None = None,
) -> ConfirmationRequest:
    if reason_code not in PAUSE_REASONS:
        raise ValueError(f"未知暂停原因码: {reason_code}")
    return ConfirmationRequest(
        reason_code=reason_code,
        reason_label=PAUSE_REASONS[reason_code],
        confirmed_info=confirmed_info or [],
        problem=problem or PAUSE_REASONS[reason_code],
        decision_needed=decision_needed or "请确认如何继续",
        options=options or [],
        question=question,
        context=context or {},
    )


# ---- 常见确认请求的工厂（供编排/路由调用，保持 5 段结构一致） ----

def irreversible_action_confirmation(action_desc: str, known: list[str],
                                     *, scope_change: bool = False) -> ConfirmationRequest:
    """高风险/不可逆动作执行前确认。"""
    options = [
        ConfirmationOption("confirm", f"确认执行：{action_desc}",
                           impact="将立即产生外部副作用且不可自动撤销", risk="high"),
        ConfirmationOption("modify", "调整范围/预算/数量后再执行",
                           impact="暂停执行，回到澄清/修改，不产生副作用", risk="low"),
        ConfirmationOption("cancel", "取消", impact="不执行任何动作", risk="low"),
    ]
    return build_confirmation(
        PAUSE_SCOPE_CHANGE if scope_change else PAUSE_IRREVERSIBLE,
        confirmed_info=known,
        problem=f"下一步「{action_desc}」属于不可逆/高风险动作。",
        decision_needed="是否在当前信息下执行该动作？",
        options=options,
        question="请确认是否执行该高风险动作：")


def evidence_conflict_confirmation(conflicts: list[dict], known: list[str]) -> ConfirmationRequest:
    desc = "；".join(f"{c.get('entity')}.{c.get('field')}: {c.get('values')}" for c in conflicts[:3])
    options = [
        ConfirmationOption("use_latest", "采用最新/现行版本",
                           impact="以生效时间最新的来源为准，旧版本归档", risk="medium"),
        ConfirmationOption("show_both", "并列展示冲突，由我判断",
                           impact="不自动取舍，结论中明确标注冲突来源", risk="low"),
        ConfirmationOption("abort", "暂停，等我核实", impact="不给结论", risk="low"),
    ]
    return build_confirmation(
        PAUSE_CONFLICT, confirmed_info=known,
        problem=f"来源间存在无法自动裁决的冲突：{desc}",
        decision_needed="冲突数据应如何处理？", options=options)


def insufficient_evidence_confirmation(what: str, known: list[str]) -> ConfirmationRequest:
    options = [
        ConfirmationOption("broaden", "放宽条件/扩大检索范围再找",
                           impact="会展开更多检索分支，耗时增加但仍不编造", risk="low"),
        ConfirmationOption("state_gap", "明确告诉我哪些无法确认",
                           impact="只回答有据部分，无法确认的内容显式标注", risk="low"),
        ConfirmationOption("provide", "我来补充资料/文件",
                           impact="您上传或给出资料后再分析", risk="low"),
    ]
    return build_confirmation(
        PAUSE_NO_EVIDENCE, confirmed_info=known,
        problem=f"关于「{what}」证据不足、无法访问或已过期。",
        decision_needed="证据不足时如何继续？", options=options)


def preference_confirmation(decision: str, choices: list[tuple[str, str, str]],
                            known: list[str]) -> ConfirmationRequest:
    """choices: [(id, label, impact), ...] 用户偏好不明确时让其取舍。"""
    options = [ConfirmationOption(cid, label, impact=impact, risk="low")
               for cid, label, impact in choices]
    options.append(ConfirmationOption("compare_all", "全部比较后再决定",
                                      impact="展开多个研究/对比分支，不替您做最终选择", risk="low"))
    return build_confirmation(
        PAUSE_PREFERENCE, confirmed_info=known,
        problem=f"您的偏好/取舍标准不明确，无法替您决定。",
        decision_needed=decision, options=options)
