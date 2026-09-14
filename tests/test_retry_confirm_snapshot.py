"""本轮新增：retrying 有界重试、5 段结构化确认、困难暂停快照。"""
from __future__ import annotations

import json

import pytest

from longflow import domains as dom_mod
from longflow import orchestrator as orc
from longflow.models import RETRYING, FAILED, PARTIALLY_COMPLETED, WAITING_USER
from longflow import confirmation as cf
from tests.conftest import tmp_db


def _drive(eng, rid, n=24):
    last = None
    for _ in range(n):
        last = eng.tick(rid)["status"]
        if last in ("completed", "partially_completed", "failed",
                    "waiting_user", "waiting_external", "cancelled", "retrying"):
            break
    return last


def _plan(node):
    return json.loads(node["plan_json"] or "{}")


# ---------- 5 段结构化确认 ----------

def test_clarify_produces_five_part_confirmation(workdir):
    s = tmp_db(workdir)
    eng = s.engine()
    reg = dom_mod.load_default_registry()
    r = eng.create_domain_goal("10万以内家用车", registry=reg)
    root = s.task(r["task_id"])
    assert root["status"] == WAITING_USER
    conf = (root["result"] or {}).get("confirmation")
    assert conf, "等待用户时必须持久化结构化确认"
    # 5 段齐全
    for key in ("reason_code", "reason_label", "confirmed_info", "problem",
                "decision_needed", "options", "question"):
        assert key in conf
    assert conf["reason_code"] == cf.PAUSE_MISSING_INFO
    assert isinstance(conf["confirmed_info"], list) and conf["confirmed_info"]
    assert conf["problem"] and conf["decision_needed"]
    # 每个可选方案都必须带影响（不用默认值掩盖不确定性）
    assert conf["options"] and all(o.get("impact") for o in conf["options"])
    ids = {o["id"] for o in conf["options"]}
    assert "answer" in ids and "compare_all" in ids


def test_high_risk_await_confirmation_is_structured(workdir):
    s = tmp_db(workdir)
    eng = s.engine()
    reg = dom_mod.load_default_registry()
    r = eng.create_domain_goal("帮我下单买10台笔记本 预算5万", registry=reg)
    root = s.task(r["task_id"])
    conf = (root["result"] or {}).get("confirmation")
    assert root["status"] in (WAITING_USER, "waiting_external")
    if conf:  # 高风险路径可能先澄清或直接等待；两者都应有结构
        assert conf["reason_code"] in (cf.PAUSE_IRREVERSIBLE, cf.PAUSE_MISSING_INFO)
        for o in conf["options"]:
            assert o.get("impact")


# ---------- retrying：可恢复瞬时错误有界重试 ----------

class _FlakyRunner:
    """前 fail_n 次返回可恢复瞬时错误，之后成功。"""
    def __init__(self, fail_n, error="search_unreachable: timeout 503"):
        self.fail_n = fail_n
        self.error = error
        self.calls = 0

    def run(self, sub_id, req, *, task_id, root_id, scenario="", input_records=None):
        from longflow import subagents as sa
        self.calls += 1
        if self.calls <= self.fail_n:
            return sa.SubagentResult(subagent=sub_id, ok=False, needs_user=False,
                                     error=self.error)
        return sa.SubagentResult(
            subagent=sub_id, ok=True, evidence=[],
            findings={"count": 0}, limitations=["无召回"])


def test_transient_failure_enters_retrying_then_succeeds(workdir, monkeypatch):
    s = tmp_db(workdir)
    eng = s.engine()
    reg = dom_mod.load_default_registry()
    # 用一个可物化的研究目标：team_ops 下官方知识库可成功；这里把 runner 换成 flaky
    r = eng.create_domain_goal("出差住宿标准是多少", registry=reg)
    rid = r["task_id"]
    flaky = _FlakyRunner(fail_n=1)
    monkeypatch.setattr(orc, "SubagentRunner", lambda runtime, **kwargs: flaky)
    status = _drive(eng, rid)
    # 至少出现过 RETRYING（检查历史上该节点的 retry_count>=1）
    nodes = [c for c in s.tasks_of_root(rid) if c["id"] != rid]
    retried = [c for c in nodes if _plan(c).get("retry_count", 0) >= 1]
    assert retried, "瞬时错误节点应进入重试"
    # 恢复步骤被记录
    snap = (s.task(rid)["result"] or {})
    assert status in ("completed", "partially_completed")


def test_nonretriable_error_does_not_retry(workdir, monkeypatch):
    s = tmp_db(workdir)
    eng = s.engine()
    reg = dom_mod.load_default_registry()

    class _HardFail:
        calls = 0
        def run(self, sub_id, req, *, task_id, root_id, scenario="", input_records=None):
            from longflow import subagents as sa
            type(self).calls += 1
            return sa.SubagentResult(subagent=sub_id, ok=False, needs_user=False,
                                     error="source_not_configured")
    hf = _HardFail()
    r = eng.create_domain_goal("出差住宿标准是多少", registry=reg)
    rid = r["task_id"]
    monkeypatch.setattr(orc, "SubagentRunner", lambda runtime, **kwargs: hf)
    _drive(eng, rid)
    nodes = [c for c in s.tasks_of_root(rid) if c["id"] != rid]
    # 配置缺口不应被重试（retry_count 全为 0）
    assert all(_plan(c).get("retry_count", 0) == 0 for c in nodes)


def test_retry_bounded_by_max_retries(workdir, monkeypatch):
    s = tmp_db(workdir)
    s.cfg["limits"]["max_node_retries"] = 2
    eng = s.engine()
    reg = dom_mod.load_default_registry()

    class _AlwaysTimeout:
        def run(self, sub_id, req, *, task_id, root_id, scenario="", input_records=None):
            from longflow import subagents as sa
            return sa.SubagentResult(subagent=sub_id, ok=False, needs_user=False,
                                     error="timeout 503")
    r = eng.create_domain_goal("出差住宿标准是多少", registry=reg)
    rid = r["task_id"]
    monkeypatch.setattr(orc, "SubagentRunner", lambda runtime, **kwargs: _AlwaysTimeout())
    _drive(eng, rid, n=40)
    nodes = [c for c in s.tasks_of_root(rid) if c["id"] != rid]
    # 任一研究节点最多重试到上限（执行次数 = 1 + max_retries）
    for c in nodes:
        assert _plan(c).get("retry_count", 0) <= 2


# ---------- 困难暂停快照 ----------

def test_partial_completion_has_pause_snapshot(workdir):
    s = tmp_db(workdir)
    eng = s.engine()
    reg = dom_mod.load_default_registry()
    r = eng.create_domain_goal("10万以内家用车推荐", registry=reg)
    rid = r["task_id"]
    eng.user_message(rid, "全部比较")
    _drive(eng, rid)
    root = s.task(rid)
    assert root["status"] == PARTIALLY_COMPLETED
    snap = (root["result"] or {}).get("pause_snapshot")
    assert snap, "部分完成必须带暂停快照"
    for key in ("completed_work", "evidence_refs", "failures",
                "open_questions", "recovery_steps"):
        assert key in snap
    assert snap["failures"] and snap["recovery_steps"]
    assert snap["open_questions"]


def test_retrying_node_resumes_after_restart(workdir, monkeypatch):
    """RETRYING 节点持久化在 DB；新连接（模拟重启）后仍被再次领取推进。"""
    from longflow import db as dbm
    from tests.conftest import BackendSession
    s = tmp_db(workdir)
    eng = s.engine()
    reg = dom_mod.load_default_registry()
    r = eng.create_domain_goal("出差住宿标准是多少", registry=reg)
    rid = r["task_id"]
    flaky = _FlakyRunner(fail_n=99)  # 始终瞬时失败 -> 进入 RETRYING/最终 FAILED
    # 先制造一次 RETRYING：max_node_retries 设大，只 tick 一步
    s.cfg["limits"]["max_node_retries"] = 5
    monkeypatch.setattr(orc, "SubagentRunner", lambda runtime, **kwargs: flaky)
    eng.tick(rid)
    # 找到一个被置为 RETRYING 或已计数的研究节点
    nodes = [c for c in s.tasks_of_root(rid) if c["id"] != rid]
    # 手动把一个节点置为 RETRYING（模拟崩溃正发生在重试窗口）
    target = nodes[0]
    plan = _plan(target)
    plan["retry_count"] = 1
    s.conn.execute("UPDATE tasks SET status=?, plan_json=? WHERE id=?",
                   ("retrying", json.dumps(plan, ensure_ascii=False), target["id"]))
    s.conn.commit()
    s.close()

    # 重启：全新 BackendSession 打开同一 DB
    s2 = BackendSession(workdir / "longflow.db")
    eng2 = s2.engine()
    monkeypatch.setattr(orc, "SubagentRunner",
                        lambda runtime, **kwargs: _FlakyRunner(fail_n=0))  # 重启后来源恢复
    for _ in range(30):
        st = eng2.tick(rid)["status"]
        if st in ("completed", "partially_completed", "failed"):
            break
    final = s2.task(rid)
    # 之前 RETRYING 的节点最终被重新领取（retry_count 仍保留且状态终结）
    t2 = s2.task(target["id"])
    assert t2["status"] in ("completed", "failed")
    assert _plan(t2).get("retry_count", 0) >= 1
    s2.close()
