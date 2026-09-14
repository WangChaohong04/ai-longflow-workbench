"""固定能力型 Subagent 注册机制（通用、与具体领域解耦）。

Subagent 由系统固定定义、独立评测，不绑定某个具体领域。领域主 Agent（domain pack）
只声明"可调用哪些 subagent"，并传入任务参数（查询、目标站点、允许域名、必填字段、
时间范围、最大来源数、是否可追问、完成标准、授权范围）。

关键约束：
- Subagent 只返回**证据和结构化结果**，不直接替用户做最终决策（无审批/下单权限）；
- Subagent 不能扩大自己的工具范围：每个 subagent 声明 allowed_tools 白名单，
  运行时只能调到白名单内的工具；
- Subagent 的输入输出走固定协议（SubagentRequest / SubagentResult）。
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable


# ---- 固定能力型 Subagent 标识（系统固定，不由领域任意新增） ----
SA_WEB_RESEARCHER = "web_researcher"
SA_OFFICIAL_RESEARCHER = "official_source_researcher"
SA_FORUM_RESEARCHER = "forum_researcher"
SA_FILE_RESEARCHER = "file_researcher"
SA_RAG_RESEARCHER = "rag_researcher"
SA_GEO_RESEARCHER = "geo_researcher"
SA_NORMALIZER = "normalizer"
SA_EVIDENCE_VERIFIER = "evidence_verifier"
SA_COMPARISON = "comparison_agent"

ALL_SUBAGENTS = [
    SA_WEB_RESEARCHER, SA_OFFICIAL_RESEARCHER, SA_FORUM_RESEARCHER,
    SA_FILE_RESEARCHER, SA_RAG_RESEARCHER, SA_GEO_RESEARCHER,
    SA_NORMALIZER, SA_EVIDENCE_VERIFIER, SA_COMPARISON,
]


@dataclasses.dataclass
class SubagentSpec:
    """固定能力 Subagent 的声明。"""
    id: str
    name: str
    description: str
    allowed_tools: list[str]          # 该 subagent 允许调用的工具白名单（不可越权）
    required_params: list[str]        # 领域主 Agent 必须传入的参数
    optional_params: list[str]
    returns_decision: bool = False    # 是否允许产出最终决策（固定 subagent 一律 False）
    source_type: str | None = None    # 证据来源类型（official/forum/document/api/mock/...）

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class SubagentRequest:
    """领域主 Agent 调 subagent 时的固定入参协议。"""
    query: str = ""
    target_sites: list[str] = dataclasses.field(default_factory=list)   # 目标网站/论坛
    allowed_domains: list[str] = dataclasses.field(default_factory=list)  # 允许访问域名
    required_fields: list[str] = dataclasses.field(default_factory=list)  # 必须返回字段
    time_range: str | None = None       # 时间范围（如 "2024-01..2025-12"）
    max_sources: int = 8                # 最大来源数量
    allow_followup: bool = False        # 是否允许继续追问
    completion_criteria: str = ""       # 完成标准
    authorization: str = ""             # 当前授权范围（只读/可写...）
    extra: dict = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SubagentRequest":
        known = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in (d or {}).items() if k in known}
        extra = {k: v for k, v in (d or {}).items() if k not in known}
        kwargs["extra"] = {**kwargs.get("extra", {}), **extra}
        return cls(**kwargs)


@dataclasses.dataclass
class SubagentResult:
    """Subagent 固定返回协议：只含证据与结构化结果，不含最终业务决策。"""
    subagent: str
    ok: bool
    evidence: list[dict] = dataclasses.field(default_factory=list)  # EvidenceRecord 列表
    normalized: list[dict] = dataclasses.field(default_factory=list)
    findings: dict = dataclasses.field(default_factory=dict)
    limitations: list[str] = dataclasses.field(default_factory=list)  # 能力/数据边界
    needs_user: bool = False
    question: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# ---- 注册表 ----

#: 系统固定能力规格（id -> SubagentSpec）
_REGISTRY: dict[str, SubagentSpec] = {}


def register_subagent(spec: SubagentSpec) -> None:
    if spec.id in _REGISTRY:
        raise ValueError(f"Subagent 重复注册: {spec.id}")
    _REGISTRY[spec.id] = spec


def get_spec(subagent_id: str) -> SubagentSpec | None:
    return _REGISTRY.get(subagent_id)


def all_specs() -> list[SubagentSpec]:
    return [_REGISTRY[i] for i in ALL_SUBAGENTS if i in _REGISTRY]


def assert_tool_allowed(subagent_id: str, tool_name: str) -> None:
    """运行时越权检查：subagent 只能调用其白名单内工具。"""
    spec = _REGISTRY.get(subagent_id)
    if spec is None:
        raise PermissionError(f"未知 subagent: {subagent_id}")
    if tool_name not in spec.allowed_tools:
        raise PermissionError(
            f"Subagent {subagent_id} 无权调用工具 {tool_name}（白名单: {spec.allowed_tools}）")


def validate_request(subagent_id: str, req: SubagentRequest) -> list[str]:
    """校验必填参数；返回缺失参数名列表（空表示通过）。"""
    spec = _REGISTRY.get(subagent_id)
    if spec is None:
        return [f"<unknown subagent {subagent_id}>"]
    missing = []
    for p in spec.required_params:
        if not getattr(req, p, None) and not req.extra.get(p):
            missing.append(p)
    return missing


def _bootstrap_registry() -> None:
    """系统固定能力 subagent（与领域无关）。工具映射到内置/插件工具白名单。"""
    register_subagent(SubagentSpec(
        SA_WEB_RESEARCHER, "网页检索员",
        "访问允许的网站收集公开信息，只返回带来源的证据。",
        allowed_tools=["http_get"], required_params=["query"],
        optional_params=["allowed_domains", "max_sources", "time_range"],
        source_type="api"))
    register_subagent(SubagentSpec(
        SA_OFFICIAL_RESEARCHER, "官方资料检索员",
        "收集官方/权威来源资料（官网、政策、公告），source_type=official。",
        allowed_tools=["http_get", "kb_search"], required_params=["query"],
        optional_params=["target_sites", "allowed_domains", "max_sources", "time_range"],
        source_type="official"))
    register_subagent(SubagentSpec(
        SA_FORUM_RESEARCHER, "论坛观点检索员",
        "收集论坛/社区观点与常见问题，观点须标注为主观观点而非事实。",
        allowed_tools=["http_get"], required_params=["query"],
        optional_params=["target_sites", "allowed_domains", "max_sources"],
        source_type="forum"))
    register_subagent(SubagentSpec(
        SA_FILE_RESEARCHER, "文件检索员",
        "检索用户上传并已激活的文件知识（带页码/标题/版本），不碰未激活文件。",
        allowed_tools=["kb_search"], required_params=["query"],
        optional_params=["required_fields", "max_sources"], source_type="document"))
    register_subagent(SubagentSpec(
        SA_RAG_RESEARCHER, "知识库检索员",
        "检索已激活知识库（BM25+字段过滤+分段检索），只返回已导入知识。",
        allowed_tools=["kb_search"], required_params=["query"],
        optional_params=["max_sources", "required_fields"], source_type="document"))
    register_subagent(SubagentSpec(
        SA_GEO_RESEARCHER, "地理分析员",
        "坐标/半径/Haversine/GeoJSON 与地点周边分析；直线距离与道路路线、评分、营业状态分开标注。",
        allowed_tools=["geo_geocode", "geo_radius_search", "geo_distance"],
        required_params=["query"],
        optional_params=["target_sites", "required_fields"], source_type="api"))
    register_subagent(SubagentSpec(
        SA_NORMALIZER, "标准化员",
        "统一对象/规格/单位/版本/字段为可比较结构，不产生新事实。",
        allowed_tools=[], required_params=[],
        optional_params=["required_fields"], source_type=None))
    register_subagent(SubagentSpec(
        SA_EVIDENCE_VERIFIER, "证据核验员",
        "检查来源、时间、冲突与证据强度，输出四档核验结论，不替用户下业务决策。",
        allowed_tools=[], required_params=[],
        optional_params=[], source_type=None))
    register_subagent(SubagentSpec(
        SA_COMPARISON, "对比分析员",
        "按用户已确认的标准做方案对比，输出结构化对照表与权衡，不替用户做最终选择。",
        allowed_tools=[], required_params=["required_fields"],
        optional_params=["completion_criteria"], source_type=None))


_bootstrap_registry()
