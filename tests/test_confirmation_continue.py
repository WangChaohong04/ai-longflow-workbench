"""无证据暂停（含结构化确认）与确认后继续、超时恢复路径。"""
from __future__ import annotations

from longflow import confirmation as cf
from longflow import domains as dom_mod
from longflow import orchestrator as orc
from longflow.models import WAITING_USER, PARTIALLY_COMPLETED
from tests.conftest import tmp_db


def _drive(eng, rid, n=30):
    last = None
    for _ in range(n):
        last = eng.tick(rid)["status"]
        if last in ("completed", "partially_completed", "failed",
                    "waiting_user", "waiting_external", "cancelled"):
            break
    return last


def test_no_evidence_pauses_with_confirmation(workdir):
    """所有研究分支无证据时：保持不变已完成 + 明确待人力继续 + 结构化确认（不伪装终态）。"""
    s = tmp_db(workdir)
    eng = s.engine()
    reg = dom_mod.load_default_registry()
    r = eng.create_domain_goal("10万以内家用车推荐", registry=reg)  # 无外部来源 -> 无证据
    rid = r["task_id"]
    eng.user_message(rid, "全部比较")
    _drive(eng, rid)
    root = s.task(rid)
    assert root["status"] in (WAITING_USER, PARTIALLY_COMPLETED)
    res = root["result"]
    conf = res.get("confirmation")
    assert conf, "无证据必须给结构化确认"
    assert conf["reason_code"] == cf.PAUSE_NO_EVIDENCE
    for key in ("confirmed_info", "problem", "decision_needed", "options", "question"):
        assert key in conf
    assert conf["options"] and all(o.get("impact") for o in conf["options"])
    # 证据不足 -> 绝不标 verified
    assert res["verified"] is False
    # 暂停快照保存已完成工作/证据/失败原因/待决/恢复步骤
    snap = res.get("pause_snapshot")
    assert snap and snap["open_questions"] and snap["recovery_steps"]


def test_no_evidence_then_cancel_marks_honest_end(workdir):
    s = tmp_db(workdir)
    eng = s.engine()
    r = eng.create_domain_goal("10万以内家用车推荐", registry=dom_mod.load_default_registry())
    rid = r["task_id"]
    eng.user_message(rid, "全部比较")
    _drive(eng, rid)
    # 用户选择"就此结束并如实说明无证据"
    out = eng.user_message(rid, "就此结束")
    # 取消并对结果标注"无证据/未核实"
    root = s.task(rid)
    res = root["result"] or {}
    assert res.get("status_bucket") in ("insufficient", "failed", "partial")
    assert res["verified"] is False
    problem = (res.get("confirmation", {}) or {}).get("problem", "") or ""
    blob = (res.get("message") or "") + (res.get("answer") or "") + problem
    assert ("无证据" in blob) or ("证据" in blob) or ("无法确认" in blob)

def test_retryable_timeout_recovers_then_continues(workdir, monkeypatch):
    """超时进入 retrying 后恢复、确认后继续（可恢复瞬时错误路径）。"""
    s = tmp_db(workdir)
    eng = s.engine()
    reg = dom_mod.load_default_registry()
    r = eng.create_domain_goal("出差住宿标准是多少", registry=reg)
    rid = r["task_id"]

    from longflow import subagents as sa
    calls = {"n": 0}
    class _FlakyRunner:
        def run(self, sub_id, req, *, task_id, root_id, scenario="", input_records=None):
            calls["n"] += 1
            if calls["n"] <= 1:
                return sa.SubagentResult(subagent=sub_id, ok=False, needs_user=False,
                                         error="timeout: 503 temporarily unavailable")
            return sa.SubagentResult(subagent=sub_id, ok=True, evidence=[],
                                     findings={"count": 0}, limitations=["无召回"])
    monkeypatch.setattr(orc, "SubagentRunner", lambda runtime, **kwargs: _FlakyRunner())
    _drive(eng, rid)
    nodes = [c for c in s.tasks_of_root(rid) if c["id"] != rid]
    # 出现过 retrying（重试计数 >=1 或事件）
    retried = [c for c in nodes if ((c["plan"] or {}).get("retry_count", 0) >= 1)
               or ((c["result"] or {}).get("retry_count", 0) >= 1)]
    assert retried, "超时节点应经过 retrying"
    # 最终节点完成或部分完成（来源恢复后继续）
    final = s.task(rid)
    assert final["status"] in ("completed", "partially_completed", "waiting_user")
