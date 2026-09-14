"""Coordinator 协议：领域主 Agent 的"有限、结构化、可校验"任务图规划器。

与旧的静态 ``subtask_templates`` 不同，Coordinator 根据**用户目标 + 已确认槽位 +
领域包允许的固定 Subagent + 工具/风险规则**，动态生成一张有界任务图：

    PlanGraph(nodes=[PlanNode ...], clarify=[...], branches=[...])

硬性约束（可内测安全边界）：
- 每个 node 只能分配**系统已注册的固定能力 Subagent**，且必须在该 DomainPack 的
  ``subagents`` 白名单内；Coordinator 不能自行发明 subagent 或扩大工具范围；
- 不产生任何高风险副作用节点（只读研究/分析/汇总）；下单/付款/外发必须走审批，
  不由 Coordinator 自动编排；
- 关键槽位缺失时不产图，返回 clarify（进入 waiting_user），不用默认值掩盖；
- 支持 branch_on：一个槽位（如 energy_type）取值为"全部/都可以"时，展开为多个
  **并行研究分支**，分支共享后续 normalizer/verifier/comparison 汇聚节点；
- 图有界（节点数上限），依赖只能引用图内已声明节点，无环（按声明顺序依赖）。

任务图随后由 orchestrator 物化为 tasks 行；并行分支可部分失败
（partially_completed），研究节点可等待用户/外部事件，失败可恢复重试。
"""
from __future__ import annotations

import dataclasses
from typing import Any

from . import subagents as sa

MAX_NODES = 24

# 已确认用户条件 -> 研究请求提示语（维度中性，领域包可扩展）
_CONDITION_LABELS = {
    "budget": "预算约", "price_max": "预算上限约", "pref": "偏好：", "preference": "偏好：",
    "用途": "用途：", "region": "地域：", "城市": "城市：", "city": "城市：",
    "location": "地点：", "center": "中心：", "brand": "品牌：", "category": "类别：",
    "time_range": "时间范围：",
}
_TIME_KEYS = {"time_range", "year", "年限"}


# 分支槽位取这些值时展开为"全部比较"
ALL_VALUES = {"all", "全部", "都可以", "全部比较", "对比全部", "不限", "所有"}

# 分支槽位 -> 可能的并行支（由领域包声明，核心不写死汽车；这里仅作顺序约定）
READONLY_KIND = {"research", "normalize", "verify", "compare"}


class PlanError(ValueError):
    pass


@dataclasses.dataclass
class PlanNode:
    key: str
    subagent: str
    kind: str                      # research | normalize | verify | compare
    label: str
    request: dict                  # SubagentRequest 入参（query/domains/fields/extra...）
    depends_on: list[str] = dataclasses.field(default_factory=list)
    branch: str | None = None      # 所属分支 id（汇聚节点为 None）
    parallel_group: str | None = None  # 同组可并行

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class PlanGraph:
    domain: str
    nodes: list[PlanNode]
    clarify: list[dict] = dataclasses.field(default_factory=list)
    branches: list[dict] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict:
        return {"domain": self.domain,
                "nodes": [n.to_dict() for n in self.nodes],
                "clarify": self.clarify, "branches": self.branches,
                "notes": self.notes}


def is_all_branches(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {v.lower() for v in ALL_VALUES}


class Coordinator:
    """领域主 Agent：输出有限可校验任务图。"""

    def __init__(self, pack):
        self.pack = pack

    # ---------- 对外主入口 ----------

    def plan(self, goal: str, slots: dict | None = None,
             *, missing_slots: list[dict] | None = None) -> PlanGraph:
        slots = dict(slots or {})
        # 1) 关键槽位缺失 -> 澄清，不产图
        missing = [m for m in (missing_slots or []) if not slots.get(m.get("name"))]
        if missing:
            return PlanGraph(domain=self.pack.id, nodes=[], clarify=missing,
                             notes=["关键槽位缺失，进入 waiting_user"])

        # 2) 分支展开（branch_on 声明）
        branches = self._expand_branches(slots)

        # 3) 组装节点
        nodes: list[PlanNode] = []
        research_keys = self._research_nodes(goal, slots, branches, nodes)
        self._analysis_nodes(goal, slots, branches, research_keys, nodes)

        graph = PlanGraph(domain=self.pack.id, nodes=nodes, branches=branches)
        self.validate(graph)
        return graph

    # ---------- 分支展开 ----------

    def _branch_spec(self) -> dict | None:
        return (self.pack.output_schema or {}).get("branch_on") or \
               (self.pack.scenario_cfg.get("domain", {}) or {}).get("branch_on")

    def _expand_branches(self, slots: dict) -> list[dict]:
        spec = self._branch_spec()
        if not spec:
            return []
        slot_name = spec.get("slot")
        options = spec.get("options", [])
        val = slots.get(slot_name)
        if is_all_branches(val):
            return [{"id": str(o["value"]), "label": o.get("label", str(o["value"])),
                     "value": o["value"], "slot": slot_name} for o in options]
        if val:
            o = next((x for x in options if str(x["value"]) == str(val)), None)
            if o:
                return [{"id": str(o["value"]), "label": o.get("label", str(val)),
                         "value": o["value"], "slot": slot_name}]
            return [{"id": str(val), "label": str(val), "value": val, "slot": slot_name}]
        return []

    # ---------- 研究节点（支持并行分支） ----------

    def _researchers(self) -> list[str]:
        """领域包可用的研究型 subagent（保持领域包声明顺序）。"""
        research = {sa.SA_WEB_RESEARCHER, sa.SA_OFFICIAL_RESEARCHER, sa.SA_FORUM_RESEARCHER,
                    sa.SA_RAG_RESEARCHER, sa.SA_FILE_RESEARCHER, sa.SA_GEO_RESEARCHER}
        return [s for s in self.pack.subagents if s in research]

    def _select_researchers(self, slots: dict) -> list[str]:
        """按目标/证据需求选择必要研究员，不默认跑满领域包全部研究员。

        领域规则由领域包声明驱动（不在核心写死某一场景）：
        - research_mode=minimal  -> 强制只跑知识类研究（RAG/文件/或声明中第一个）；
        - research_mode=full     -> 强制多来源（官网/网页/论坛 全部声明研究员）；
        - 缺省判定：有 branch_on 或 comparison_fields 或 multi_source=true -> 多来源；
          否则视为简单知识/单来源查询，只跑必要研究员（优先 RAG/文件）。
        - GEO 仅当存在空间需求（location/address/center 槽位）或领域包 geo_always 时才激活；
          领域包只有 GEO 一个研究员时视为地理域，总是启用。
        """
        soul = dict(slots or {})
        declared = self._researchers()
        if not declared:
            return [sa.SA_RAG_RESEARCHER]
        dom = self.pack.scenario_cfg.get("domain", {}) or {}
        policy = dom.get("research_mode")
        branch_on = self._branch_spec()
        comp_fields = dom.get("comparison_fields") or []
        multi = bool(branch_on) or bool(comp_fields) or bool(dom.get("multi_source"))
        if policy == "minimal":
            multi = False
        if policy == "full":
            multi = True
        if not multi:
            # 简单/单来源查询：不启动全部研究员
            for pref in (sa.SA_RAG_RESEARCHER, sa.SA_FILE_RESEARCHER):
                if pref in declared:
                    return [pref]
            return declared[:1]
        # 多来源：采用声明的研究集；GEO 按需激活
        geo = sa.SA_GEO_RESEARCHER
        has_spatial = any(soul.get(k) for k in ("location", "address", "center", "radius_km"))
        geo_always = bool(dom.get("geo_always")) or set(declared) == {geo}
        picked = [r for r in declared if r != geo or (geo_always or has_spatial)]
        order = {s: i for i, s in enumerate(declared)}
        return sorted(picked, key=lambda s: order.get(s, 99))

    def _research_nodes(self, goal, slots, branches, nodes) -> list[str]:
        researchers = self._select_researchers(slots) or [sa.SA_RAG_RESEARCHER]
        targets = branches or [{"id": "main", "label": "", "value": None}]
        keys: list[str] = []
        group_id = "research"
        for b in targets:
            bq = self._branch_query(goal, b)
            for idx, rs in enumerate(researchers):
                key = f"res_{b['id']}_{rs}" if branches else f"res_{rs}"
                req = self._research_request(rs, bq, slots, b)
                nodes.append(PlanNode(
                    key=key, subagent=rs, kind="research",
                    label=self._research_label(rs, b),
                    request=req, branch=(b["id"] if branches else None),
                    parallel_group=group_id))
                keys.append(key)
        return keys

    def _branch_query(self, goal: str, branch: dict) -> str:
        if branch["id"] == "main" or not branch.get("label"):
            return goal
        return f"{goal}｜研究分支：{branch['label']}"

    def _research_label(self, rs: str, branch: dict) -> str:
        spec = sa.get_spec(rs)
        base = spec.name if spec else rs
        return f"{base}·{branch['label']}" if branch.get("label") else base

    def _condition_context(self, slots: dict) -> tuple[str, str | None]:
        """把已确认细分条件转成 请求上下文(文本) + 时间范围；缺失条件不参与。"""
        parts = []
        time_range = None
        for name, hint in _CONDITION_LABELS.items():
            v = (slots or {}).get(name)
            if v is None or str(v).strip() == "":
                continue
            parts.append(f"{hint}{v}")
            if name in _TIME_KEYS:
                time_range = str(v)
        return ("，".join(parts)), time_range

    def _research_request(self, rs: str, query: str, slots: dict, branch: dict) -> dict:
        req = {"query": query, "max_sources": 8}
        # 官方来源：官网/授权 API（target_sites 由领域配置给出）
        dom = self.pack.scenario_cfg.get("domain", {}) or {}
        sites = dom.get("target_sites", {}) or {}
        if rs == sa.SA_OFFICIAL_RESEARCHER and sites.get("official"):
            req["allowed_domains"] = sites["official"]
        if rs == sa.SA_FORUM_RESEARCHER and sites.get("forum"):
            req["allowed_domains"] = sites["forum"]
        if rs == sa.SA_WEB_RESEARCHER and sites.get("web"):
            req["allowed_domains"] = sites["web"]
        # 已确认的用户条件进入研究请求（可追溯、影响后续查询）
        ctx, time_range = self._condition_context(slots)
        if ctx:
            req["query"] = f"{query}｜{ctx}"
        if time_range:
            req["time_range"] = time_range
        # 领域自定义请求参数（extra），受 subagent 协议字段约束
        extra = dict(dom.get("request_extra", {}) or {})
        if branch.get("value") is not None:
            extra.setdefault("branch_value", branch["value"])
            if branch.get("slot"):
                extra[branch["slot"]] = branch["value"]
        if rs == sa.SA_GEO_RESEARCHER:
            extra.setdefault("center", slots.get("location"))
            extra.setdefault("radius_km", slots.get("radius_km", 2))
        if extra:
            req["extra"] = {k: v for k, v in extra.items() if v is not None}
        return req

    # ---------- 汇聚：归一化 / 核验 / 对照 ----------

    def _analysis_nodes(self, goal, slots, branches, research_keys, nodes):
        have = set(self.pack.subagents)
        if sa.SA_NORMALIZER in have:
            nodes.append(PlanNode("normalize", sa.SA_NORMALIZER, "normalize",
                                  "标准化（单位/别名/型号/版本）",
                                  {"query": goal}, depends_on=list(research_keys)))
        dep_norm = ["normalize"] if any(n.key == "normalize" for n in nodes) else list(research_keys)
        if sa.SA_EVIDENCE_VERIFIER in have:
            nodes.append(PlanNode("verify", sa.SA_EVIDENCE_VERIFIER, "verify",
                                  "证据核验（来源/冲突/层级）",
                                  {"query": goal}, depends_on=dep_norm))
        if sa.SA_COMPARISON in have:
            crit = (self.pack.scenario_cfg.get("domain", {}) or {}).get(
                "comparison_fields", []) or self._default_criteria()
            nodes.append(PlanNode("compare", sa.SA_COMPARISON, "compare",
                                  "事实对照表（按用户确认维度）",
                                  {"query": goal, "required_fields": crit},
                                  depends_on=["verify"] if
                                  any(n.key == "verify" for n in nodes) else dep_norm))

    def _default_criteria(self) -> list[str]:
        # 不含主观推荐；领域包可用 comparison_fields 覆盖
        return ["price", "value", "distance_km"]

    # ---------- 校验 ----------

    def validate(self, graph: PlanGraph) -> None:
        if len(graph.nodes) > MAX_NODES:
            raise PlanError(f"任务图节点超过上限 {MAX_NODES}")
        keys = set()
        for n in graph.nodes:
            if n.key in keys:
                raise PlanError(f"节点 key 重复: {n.key}")
            keys.add(n.key)
            spec = sa.get_spec(n.subagent)
            if spec is None:
                raise PlanError(f"节点 {n.key} 分配了未注册 subagent: {n.subagent}")
            if n.subagent not in self.pack.subagents:
                raise PlanError(
                    f"领域 {self.pack.id} 越权调用不在白名单的 subagent: {n.subagent}")
            if spec.returns_decision:
                raise PlanError(f"subagent {n.subagent} 不允许产出最终决策")
            if n.kind not in READONLY_KIND:
                raise PlanError(f"Coordinator 只编排只读节点，收到 kind={n.kind}")
        declared = {n.key for n in graph.nodes}
        for n in graph.nodes:
            for d in n.depends_on:
                if d not in declared:
                    raise PlanError(f"节点 {n.key} 依赖了不存在的节点 {d}")
        # 无环：依赖必须在自身之前声明
        pos = {n.key: i for i, n in enumerate(graph.nodes)}
        for n in graph.nodes:
            if any(pos[d] >= pos[n.key] for d in n.depends_on):
                raise PlanError(f"任务图存在非法依赖顺序（环）: {n.key}")


def coordinate(pack, goal: str, slots: dict | None = None,
               missing_slots: list[dict] | None = None) -> PlanGraph:
    return Coordinator(pack).plan(goal, slots, missing_slots=missing_slots)
