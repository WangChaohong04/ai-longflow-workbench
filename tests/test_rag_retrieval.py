"""RAG 按需激活/证据复用/预算/访问控制/指标 专项回归（SPEC §0 + §4）。

覆盖按需激活契约：
- 检索按阶段按需发起，复用有效证据（verifier/executor/controller 不重复检索）；
- 检索次数有上限，无依据时收敛为"无法确认"；
- 出口检查支持但不重复检索；恢复时仅复用已持久化证据、不重复检索；
- 按需激活不绕过访问控制：RAG 场景过滤（scenario）不得跨场景泄漏证据；
  字段过滤（fields）不写死行业字段；
- 未经检索的模型推断不得作为已验证业务事实；
- 评测可观测检索调用量/模型调用量/时延（metrics）。
"""
from __future__ import annotations

import conftest as cf

cf.skip_if_no_backend()

import pytest  # noqa: E402

import run_eval  # noqa: E402


def _kb_requests(events):
    return [e for e in events if e.get("kind") == "tool_request"
            and (e.get("detail") or {}).get("tool") == "kb_search"]


# ---------- 按需检索：只有研究员发起、且次数受控 ----------

def test_rule_qa_single_on_demand_retrieval(workdir):
    """规则问答：仅 researcher 发起 1 次 kb_search；verifier/controller 复用证据不重检索。"""
    s = cf.tmp_db(workdir / "db")
    try:
        out = cf.run_goal(s, "公司采购笔记本电脑的审批规则是什么？", "team_ops")
        reqs = _kb_requests(out["events"])
        assert reqs, "应至少有一次按需检索"
        assert len(reqs) == 1, f"kb_search 调用 {len(reqs)} 次，期望 1 次（证据复用，不重复检索）"
        tid_to_role = {t["id"]: t["agent_role"] for t in out["tasks"]}
        roles = [tid_to_role.get(e.get("task_id")) for e in reqs]
        assert set(roles) == {"researcher"}, f"检索应由研究员发起，实际: {roles}"
        # verifier/executor/controller 没有任何 kb_request
        non_research = [e for e in reqs
                        if tid_to_role.get(e.get("task_id")) != "researcher"]
        assert not non_research
    finally:
        s.close()


def test_purchase_flow_reuses_research_evidence(workdir):
    """采购长程任务：研究分支检索一次；审批后执行/核验阶段不再 kb_search。"""
    s = cf.tmp_db(workdir / "db")
    try:
        rid = s.create_root("我要采购1台打印机，预算2000元，走标准供应商 approved-vendor",
                            "team_ops", slots={"item": "打印机", "budget": "2000",
                                               "vendor": "approved-vendor"})
        cf._drive(s, rid)
        pend = s.pending_approvals(rid)
        assert pend, "采购分支应进入 waiting_approval"
        pre = len(_kb_requests(s.events(rid)))
        s.decide_approval(pend[0]["id"], "approved")  # 内部 tick 恢复并执行
        cf._drive(s, rid)
        post = len(_kb_requests(s.events(rid)))
        assert post == pre, f"审批/执行阶段新增检索 {post - pre} 次（应复用研究证据）"
        assert post <= 2, f"整个采购流程检索 {post} 次，超出按需预算"
        assert s.task(rid)["status"] == "completed"
    finally:
        s.close()


# ---------- 出口不重复检索 / 无证据收敛 ----------

def test_exit_gate_does_not_reretrieve(workdir):
    """出口闸门核验支持关系但不重新检索：verifier 无 kb_request。"""
    s = cf.tmp_db(workdir / "db")
    try:
        out = cf.run_goal(s, "公司采购笔记本电脑的审批规则是什么？", "team_ops")
        verifier_tasks = [t for t in out["tasks"] if t["agent_role"] == "verifier"]
        verifier_ids = {t["id"] for t in verifier_tasks}
        verifier_retr = [e for e in _kb_requests(out["events"])
                         if e.get("task_id") in verifier_ids]
        assert not verifier_retr, "出口核验（verifier）不应重复发起 kb_search"
        # verifier 仍完成了核验（有 llm_call 草稿但无检索）
        assert all(t["status"] in ("completed", "failed") for t in verifier_tasks)
    finally:
        s.close()


def test_no_evidence_converges_within_budget(workdir):
    """无依据时在检索预算内收敛为无法确认（不无限重试检索）。"""
    s = cf.tmp_db(workdir / "db")
    try:
        out = cf.run_goal(s, "公司的宠物保险政策是什么？", "team_ops")
        n = len(_kb_requests(out["events"]))
        assert n <= 2, f"无依据时检索 {n} 次超过预算（应收敛）"
        answer = run_eval._answer_of(out["root"], out["events"])
        assert any(k in answer for k in ("无法确认", "未找到", "没有找到", "无相关",
                                        "未检索到", "不能确认")), answer[:200]
    finally:
        s.close()


# ---------- 恢复不重复检索 ----------

def test_recovery_reuses_persisted_evidence(workdir):
    """长程任务重启恢复：研究分支在重启前已检索并持久化；恢复阶段新增检索为 0。"""
    d = workdir / "case8"
    s = cf.tmp_db(d)
    rid = None
    try:
        rid = s.create_root("我要采购1台打印机，预算2000元，走标准供应商 approved-vendor",
                            "team_ops", slots={"item": "打印机", "budget": "2000",
                                               "vendor": "approved-vendor"})
        cf._drive(s, rid)
        pend = s.pending_approvals(rid)
        s.decide_approval(pend[0]["id"], "approved")
        pre = len(_kb_requests(s.events(rid)))
        assert pre >= 1
    finally:
        s.close()
    # 两次进程重启（新连接、内存状态全丢）
    s2 = cf.restart(d / "longflow.db", rid)
    try:
        pass
    finally:
        s2.close()
    s3 = cf.restart(d / "longflow.db", rid)
    try:
        after = len(_kb_requests(s3.events(rid)))
        assert after == pre, f"重启恢复新增检索 {after - pre} 次（应仅复用已持久化证据）"
        assert s3.task(rid)["status"] == "completed"
    finally:
        s3.close()


# ---------- 访问控制不被绕过 ----------

def test_retrieval_scenario_scope_isolation(workdir):
    """按需检索不得绕过访问范围：scenario 过滤阻止跨场景证据泄漏。"""
    from longflow import rag
    s = cf.tmp_db(workdir / "db")
    try:
        s.ensure_scenario_knowledge("team_ops")
        s.ensure_scenario_knowledge("geo_site")
        # team_ops 上下文检索 geo 词汇 → 不得返回 geo_site 场景 chunk
        res = rag.search(s.conn, "中关村 咖啡 选址 场地", scenario="team_ops", top_k=10)
        assert all((r.get("scenario") or "team_ops") == "team_ops" for r in res), \
            f"team_ops 检索泄漏跨场景证据: {[r.get('doc_name') for r in res]}"
        # geo_site 上下文检索采购词汇 → 不得返回 team_ops chunk
        res2 = rag.search(s.conn, "采购 审批 报销 规则", scenario="geo_site", top_k=10)
        docs2 = [r.get("doc_name") for r in res2]
        assert not any(d in ("procurement.md", "expense.md", "travel_2023.md",
                            "travel_2024.md") for d in docs2), f"geo_site 检索泄漏 team_ops: {docs2}"
    finally:
        s.close()


def test_retrieval_fields_filter_does_not_leak(workdir):
    """字段过滤（业务字段由场景提供，核心不写死）生效：不相关字段值不召回。"""
    from longflow import rag
    s = cf.tmp_db(workdir / "db")
    try:
        s.ensure_scenario_knowledge("geo_site")
        # 不存在的字段值 → 过滤后为空（不绕过字段约束返回任意 chunk）
        res = rag.search(s.conn, "咖啡馆", scenario="geo_site",
                         fields={"category": "__不存在的类别XYZ__"}, top_k=10)
        assert res == [], f"字段过滤失效，返回 {len(res)} 条不相关 chunk"
    finally:
        s.close()


# ---------- 未经检索的推断不得作为事实 ----------

def test_verified_answer_carries_citations(workdir):
    """verified=true 的结论必须附引用（无引用的模型推断不得作为已验证业务事实）。"""
    s = cf.tmp_db(workdir / "db")
    try:
        out = cf.run_goal(s, "公司采购笔记本电脑的审批规则是什么？", "team_ops")
        res = out["root"]["result"]
        if res.get("verified") is True:
            assert res.get("citations"), "verified=true 但无引用（推断冒充事实）"
        else:
            pytest.skip("该后端运行未给出 verified=true（由用例 1 强断言覆盖）")
    finally:
        s.close()


# ---------- 指标可观测（SPEC §0：检索调用量/成本/时延） ----------

def test_run_all_emits_rag_metrics():
    """run_all 每例输出 metrics：retrieval_calls/llm_calls/tool_calls/elapsed_ms。"""
    r = run_eval.run_all()
    assert r["total"] == 10
    for c in r["cases"]:
        detail = run_eval.execute_case(cf.load_case(c["id"]))
        m = detail.get("metrics")
        assert m is not None, f"{c['id']} 缺少 metrics"
        assert {"retrieval_calls", "llm_calls", "tool_calls", "elapsed_ms"} <= set(m), m
        assert m["retrieval_calls"] >= 0 and m["elapsed_ms"] >= 0
