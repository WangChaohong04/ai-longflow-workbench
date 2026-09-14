"""事实-证据核验（确定性，不把词语重叠当事实正确）。

核验粒度是"论断（claim）"而非整段答案：
- 数字论断：答案中的 数值+单位（如 500元、300公里、2km）必须能在其引用证据中找到
  数值与单位一致的支撑；证据里出现相关词但数值不同 => 核验失败（金额错误）。
- 条件/极性论断：答案中的 必须/不得/禁止/可以/无需 等约束，其极性必须与证据一致，
  证据说"不得"而答案说"可以" => 条件相反，核验失败。
- 存在性：答案引用的 chunk_id 必须存在；有事实性答案但零引用 => 证据不足。

四档结论：
  verified        所有可确定性核验的论断都有证据支撑，且引用存在
  partial         部分论断有据，部分无法确认（证据未覆盖，而非被证伪）
  insufficient    没有足以支撑结论的证据（零引用 / 证据与问题无关）
  failed          存在被证伪的论断（数字错误 / 条件相反 / 引用不存在）

语义关系（证据"隐含"但未显式写出的结论）确定性方法无法判定，保持 unconfirmed，
不计入 verified 也不直接判 failed——不把词重叠当证明，也不假装能证明语义。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 结论档位
VERIFIED = "verified"
PARTIAL = "partial"
INSUFFICIENT = "insufficient"
FAILED = "failed"

# 数字 + 单位（货币/距离/比例/时长等常见单位）
_NUM_UNIT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(元|块|万元|千元|公里|千米|km|KM|米|m|%|％|分钟|小时|天|个|次|人|岁)?"
)
# 条件 / 极性词
_POLARITY_POS = ["可以", "允许", "支持", "应当", "必须", "需要", "需"]
_POLARITY_NEG = ["不得", "禁止", "不能", "不可以", "无需", "不用", "不予", "不允许"]
# 数字附近的指标名（用于把数字论断对齐到证据中的同一指标）
_METRIC_HINTS = ["标准", "限额", "上限", "额度", "预算", "报销", "住宿", "补贴",
                 "距离", "半径", "金额", "费用", "价格", "比例", "时长", "时间"]


@dataclass
class Claim:
    kind: str                 # "number" | "polarity"
    text: str
    value: float | None = None
    unit: str | None = None
    polarity: str | None = None  # "pos" | "neg"
    metric: str | None = None
    status: str = "unconfirmed"  # supported | contradicted | unconfirmed
    evidence: str | None = None


@dataclass
class VerificationResult:
    verdict: str = INSUFFICIENT
    claims: list[dict] = field(default_factory=list)
    problems: list[dict] = field(default_factory=list)
    supported: int = 0
    contradicted: int = 0
    unconfirmed: int = 0
    missing_citations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "passed": self.verdict == VERIFIED,
            "claims": self.claims,
            "problems": self.problems,
            "counts": {"supported": self.supported, "contradicted": self.contradicted,
                       "unconfirmed": self.unconfirmed},
            "missing_citations": self.missing_citations,
        }


def _norm_unit(u: str | None) -> str | None:
    if not u:
        return None
    table = {"块": "元", "千米": "公里", "KM": "km", "％": "%", "m": "米"}
    return table.get(u, u)


def _metric_near(text: str, start: int) -> str | None:
    window = text[max(0, start - 12): start + 12]
    for h in _METRIC_HINTS:
        if h in window:
            return h
    return None


def extract_claims(text: str) -> list[Claim]:
    """从答案文本抽取可确定性核验的论断（数字+单位、条件极性）。"""
    claims: list[Claim] = []
    for m in _NUM_UNIT.finditer(text or ""):
        num, unit = m.group(1), m.group(2)
        # 纯编号（无单位且不像指标值）跳过，避免把"第3条"当数字论断
        if not unit:
            continue
        try:
            value = float(num)
        except ValueError:
            continue
        claims.append(Claim(
            kind="number", text=m.group(0), value=value,
            unit=_norm_unit(unit), metric=_metric_near(text, m.start()),
        ))
    for w in _POLARITY_NEG:
        if w in (text or ""):
            claims.append(Claim(kind="polarity", text=w, polarity="neg"))
    for w in _POLARITY_POS:
        if w in (text or ""):
            claims.append(Claim(kind="polarity", text=w, polarity="pos"))
    return claims


def _numbers_in(text: str) -> list[tuple[float, str | None]]:
    out = []
    for m in _NUM_UNIT.finditer(text or ""):
        try:
            out.append((float(m.group(1)), _norm_unit(m.group(2))))
        except ValueError:
            continue
    return out


# 指标近义词分组：同一业务指标的不同写法
_METRIC_SYNONYMS = {
    "住宿": {"住宿", "住宿标准", "住宿限额", "住宿上限", "酒店", "房费"},
    "采购": {"采购", "采购预算", "采购上限", "购买"},
    "预算": {"预算", "额度", "上限", "限额", "费用"},
    "报销": {"报销", "报销额度", "报销标准", "补贴", "补助"},
    "距离": {"距离", "半径", "里程", "路程"},
    "价格": {"价格", "售价", "报价", "单价", "落地价"},
}
# 明显跨业务、绝不可互相支持的指标对
_METRIC_INCOMPATIBLE = [
    ({"住宿", "酒店", "房费", "差旅", "报销"}, {"采购", "购买", "供应商"}),
    ({"距离", "半径", "里程"}, {"预算", "价格", "金额"}),
]
_YEAR_RE = re.compile(r"(20\d{2})\s*年?")


def _metric_tokens(metric: str | None) -> set[str]:
    if not metric:
        return set()
    for key, syns in _METRIC_SYNONYMS.items():
        if metric in syns:
            return set(syns)
    return {metric}


def _detect_metrics(text: str) -> set[str]:
    """抽取文本中出现的指标相关词（含近义词）。"""
    found = set()
    for key, syns in _METRIC_SYNONYMS.items():
        for w in syns:
            if w and w in text:
                found.add(key)
                found.add(w)
    return found


def _years_in(text: str) -> set[str]:
    return set(_YEAR_RE.findall(text or ""))


def _metrics_compatible(claim_metric: str | None, claim_text_ctx: str,
                        evidence_text: str) -> bool:
    """判断论断指标/实体是否与证据兼容（防止住宿500支持采购500）。"""
    claim_metrics = _detect_metrics(claim_text_ctx) | _metric_tokens(claim_metric)
    ev_metrics = _detect_metrics(evidence_text)
    if not claim_metrics or not ev_metrics:
        return True  # 任一侧无指标线索时不强行否决（交 unconfirmed 处理）
    # 硬不兼容
    for a, b in _METRIC_INCOMPATIBLE:
        if (claim_metrics & a) and (ev_metrics & b):
            return False
        if (claim_metrics & b) and (ev_metrics & a):
            return False
    # 要求至少一个指标同义词重合（同业务指标）
    if claim_metrics & ev_metrics:
        return True
    # 两侧都是通用额度词但具体业务域不同：判不兼容
    return False


def _check_number_claim(claim: Claim, evidence_text: str, *,
                        claim_context: str = "", claim_years: set[str] | None = None) -> str:
    """数字论断：证据中须存在
    1) 数值与单位一致；2) 业务指标/实体兼容；3) 时间（年度）/版本不冲突。

    数值同但指标不同（如"住宿上限500元" vs "采购预算500元"）不构成支撑。
    """
    claim_years = claim_years or _years_in(claim_context)
    ev_years = _years_in(evidence_text)
    year_conflict = bool(claim_years and ev_years and not (claim_years & ev_years))
    compatible = _metrics_compatible(claim.metric, claim_context or claim.text, evidence_text)
    ev_nums = _numbers_in(evidence_text)

    same_metric_match = False
    same_unit_any = False
    for val, unit in ev_nums:
        same_unit = (claim.unit is None) or (unit is None) or (unit == claim.unit)
        if not same_unit:
            continue
        same_unit_any = True
        if abs(val - (claim.value or 0)) < 1e-6 and compatible and not year_conflict:
            same_metric_match = True
    if same_metric_match:
        return "supported"
    # 同单位同数值但指标/时间不匹配 -> 不能算证伪（不是同一论断），也不能支撑 -> unconfirmed
    if same_unit_any:
        # 若证据同指标且同单位但数值不同（同年），才算真正证伪
        if compatible and not year_conflict:
            for val, unit in ev_nums:
                same_unit = (claim.unit is None) or (unit is None) or (unit == claim.unit)
                if same_unit and abs(val - (claim.value or 0)) >= 1e-6:
                    return "contradicted"
        return "unconfirmed"
    return "unconfirmed"


def _check_polarity_claim(claim: Claim, evidence_text: str) -> str:
    ev_neg = any(w in evidence_text for w in _POLARITY_NEG)
    ev_pos = any(w in evidence_text for w in _POLARITY_POS)
    if claim.polarity == "neg":
        if ev_neg and not ev_pos:
            return "supported"
        if ev_pos and not ev_neg:
            return "contradicted"
    else:
        if ev_pos and not ev_neg:
            return "supported"
        if ev_neg and not ev_pos:
            return "contradicted"
    return "unconfirmed"


def verify_answer(answer_text: str, citation_ids: list[str], chunks: list[dict],
                  *, has_factual_answer: bool = True) -> VerificationResult:
    """核验答案对证据的可支撑性。

    citation_ids: 答案声明引用的 chunk_id 列表。
    chunks: 同根任务检索到的全部证据 chunk（dict 含 chunk_id/text）。
    """
    res = VerificationResult()
    by_id = {c["chunk_id"]: c for c in chunks}

    # 1) 引用存在性
    valid_cite_texts = []
    for cid in citation_ids or []:
        ch = by_id.get(cid)
        if ch is None:
            res.missing_citations.append(cid)
            res.problems.append({"chunk_id": cid, "problem": "引用不存在"})
        else:
            valid_cite_texts.append(ch.get("text", ""))
    if res.missing_citations:
        res.verdict = FAILED

    # 2) 零引用但有事实性答案 => 证据不足（不自动补引用）
    if not citation_ids and has_factual_answer:
        res.verdict = FAILED if res.verdict == FAILED else INSUFFICIENT
        res.problems.append({"problem": "答案包含事实性论断但未提供任何引用"})
        return _finalize(res)

    evidence_blob = "\n".join(valid_cite_texts)

    # 3) 论断级核验
    claims = extract_claims(answer_text or "")
    for cl in claims:
        if not valid_cite_texts:
            cl.status = "unconfirmed"
        else:
            # 在所有引用证据中寻找：任一支撑即 supported；否则若任一证伪即 contradicted
            statuses = []
            support_text = None
            for et in valid_cite_texts:
                if cl.kind == "number":
                    s = _check_number_claim(cl, et, claim_context=answer_text or "")
                else:
                    s = _check_polarity_claim(cl, et)
                statuses.append(s)
                if s == "supported":
                    support_text = et[:120]
            if "supported" in statuses:
                cl.status, cl.evidence = "supported", support_text
            elif "contradicted" in statuses:
                cl.status = "contradicted"
            else:
                cl.status = "unconfirmed"
        res.claims.append({
            "kind": cl.kind, "text": cl.text, "value": cl.value, "unit": cl.unit,
            "polarity": cl.polarity, "status": cl.status,
        })
        if cl.status == "supported":
            res.supported += 1
        elif cl.status == "contradicted":
            res.contradicted += 1
            res.problems.append({"claim": cl.text, "problem": "证据与论断矛盾（数值/条件不一致）"})
        else:
            res.unconfirmed += 1

    return _finalize(res)


def _finalize(res: VerificationResult) -> VerificationResult:
    if res.missing_citations or res.contradicted > 0:
        res.verdict = FAILED
    elif res.supported > 0 and res.unconfirmed == 0:
        res.verdict = VERIFIED
    elif res.supported > 0:
        res.verdict = PARTIAL
    elif res.unconfirmed > 0:
        # 有论断但证据无法确定性支撑：部分核验（语义关系留待人工/模型，不当证明）
        res.verdict = PARTIAL
    else:
        res.verdict = INSUFFICIENT
    return res


# ---------- 槽位 / 动作约束核验（执行前把关，独立于起草） ----------

# 从采购知识文本抽取"金额超过 X 元须审批"阈值；无法确定则不设硬阈值。
_AMOUNT_LINE = re.compile(r"(?:超过|大于|≥|>=?)\s*(\d+(?:\.\d+)?)\s*元")
# 供应商名录：从"名录包含/名录内"段落提取形如 vendor-name 的条目
_VENDOR_RE = re.compile(r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:-vendor|-mart|-it))\b", re.IGNORECASE)


def extract_amount_threshold(chunks: list[dict]) -> float | None:
    """从证据抽取金额审批线（取出现的下限阈值）。无证据返回 None（不硬猜）。"""
    blob = "\n".join(c.get("text", "") for c in chunks)
    vals = [float(m.group(1)) for m in _AMOUNT_LINE.finditer(blob)]
    return min(vals) if vals else None


def extract_approved_vendors(chunks: list[dict]) -> set[str]:
    """从证据抽取标准供应商名录条目。"""
    blob = "\n".join(c.get("text", "") for c in chunks)
    return {m.group(1).lower() for m in _VENDOR_RE.finditer(blob)}


def check_purchase_constraints(args: dict, chunks: list[dict]) -> dict:
    """执行前核验采购动作的硬约束（金额阈值/供应商名录）。

    返回 {ok, problems:[...], amount_threshold, vendor_known}。
    - 金额超过证据规定的审批线：必须有人工审批（由 permissions/high risk 把关），
      本函数只标注"需要审批"，不替代审批。
    - 供应商明确不在名录且名录可从证据确认：判违规（problem），阻止无声下单。
    - 证据不足以确认阈值/名录时如实标注 unknown，不编造规则。
    """
    problems: list[dict] = []
    threshold = extract_amount_threshold(chunks)
    vendors = extract_approved_vendors(chunks)

    amount = args.get("amount")
    try:
        amount = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount = None
    needs_approval = False
    if amount is not None and threshold is not None and amount >= threshold:
        needs_approval = True
        problems.append({"kind": "amount_threshold",
                         "problem": f"金额 {amount:g} 元达到/超过审批线 {threshold:g} 元，须人工审批后下单"})

    vendor = (args.get("vendor") or "").strip().lower()
    vendor_ok = None
    if vendor:
        if vendors:
            # 明确不在名录（且证据显式说明某供应商不在名录）=> 违规
            blob = "\n".join(c.get("text", "") for c in chunks)
            explicitly_excluded = bool(re.search(rf"{re.escape(vendor)}[^。\n]*(不在|无法查到|违规)", blob))
            if explicitly_excluded:
                vendor_ok = False
                problems.append({"kind": "vendor_not_listed",
                                 "problem": f"供应商 {vendor} 不在标准名录且证据标明违规"})
            else:
                vendor_ok = vendor in vendors
        else:
            vendor_ok = None  # 无证据可确认，保持未知
    return {
        "ok": not problems,
        "problems": problems,
        "amount_threshold": threshold,
        "needs_approval": needs_approval,
        "vendor_ok": vendor_ok,
        "vendors": sorted(vendors),
    }


def overall_verified(*, verification_dicts: list[dict] | None = None,
                      conflicts: list | None = None, tool_failures: list | None = None,
                      denied: list | None = None, pending_high_risk_approvals: int = 0,
                      has_substantive_answer: bool = True) -> dict:
    """总体核验判定（比单个 verify_answer 更严格的出口条件）。

    verified=True 必须**同时**满足：
      - 存在实质性结论；
      - 每个可确定性核验的关键事实核验档为 verified（无 partial/insufficient/failed）；
      - 无证据冲突；
      - 无失败工具；
      - 无被拒权限；
      - 无未解决的高风险审批。
    任何一条不满足都不得对外宣称"核验通过"。返回四档桶与原因，供前端分层展示。
    """
    conflicts = conflicts or []
    tool_failures = tool_failures or []
    denied = denied or []
    buckets = {"verified": 0, "partial": 0, "insufficient": 0, "failed": 0, "none": 0}
    reasons: list[dict] = []
    for v in verification_dicts or []:
        verdict = (v or {}).get("verdict", "none")
        buckets[verdict if verdict in buckets else "none"] += 1
        for prob in (v or {}).get("problems", []) or []:
            reasons.append({"kind": verdict, "detail": prob})

    if buckets["failed"]:
        bucket = FAILED
    elif tool_failures or denied or conflicts or pending_high_risk_approvals:
        bucket = FAILED if denied else PARTIAL
    elif not has_substantive_answer:
        bucket = INSUFFICIENT
    elif buckets["partial"] or buckets["insufficient"]:
        bucket = PARTIAL if buckets["verified"] or buckets["partial"] else INSUFFICIENT
    elif buckets["verified"]:
        bucket = VERIFIED
    else:
        bucket = INSUFFICIENT

    if tool_failures:
        reasons.append({"kind": "tool_failure", "detail": tool_failures})
    if denied:
        reasons.append({"kind": "permission_denied", "detail": denied})
    if conflicts:
        reasons.append({"kind": "source_conflict", "detail": conflicts})
    if pending_high_risk_approvals:
        reasons.append({"kind": "pending_approval",
                        "detail": f"{pending_high_risk_approvals} 个高风险审批未决"})

    return {
        "verified": bucket == VERIFIED,
        "bucket": bucket,
        "verified_partial": bucket == PARTIAL,
        "insufficient": bucket == INSUFFICIENT,
        "counts": buckets,
        "reasons": reasons,
    }
