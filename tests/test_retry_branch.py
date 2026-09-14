"""问题6a：用户触发的单分支重试——只重跑失败只读分支，保留其余证据，重算汇聚。"""
from __future__ import annotations

import json

from longflow import domains as dom_mod
from longflow import db
from tests.conftest import tmp_db

TERMINAL = {"completed", "partially_completed", "failed", "cancelled"}


def _drive(eng, rid, n=30):
    last = None
    for _ in range(n):
        last = eng.tick(rid)["status"]
        if last in TERMINAL:
            break
    return last


def _plan(c):
    return json.loads(c["plan_json"]) if c.get("plan_json") else {}


def test_retry_only_failed_branch_preserves_others(workdir):
    s = tmp_db(workdir)
    eng = s.engine()
    reg = dom_mod.load_default_registry()
    r = eng.create_domain_goal("10万以内家用车", registry=reg)
    rid = r["task_id"]
    eng.user_message(rid, "全部比较")
    assert _drive(eng, rid) == "partially_completed"

    children = [dict(c) for c in s.tasks_of_root(rid)]
    research = [c for c in children if _plan(c).get("node_kind") == "research"]
    failed = [c for c in research if c["status"] == "failed"]
    assert failed, "外部来源未配置：研究节点应失败"
    branches = sorted({_plan(c)["branch"] for c in failed})
    target = branches[0]

    out = eng.retry_branch(rid, target)
    assert len(out["retried"]) >= 1
    assert out["reasons"], "应返回失败原因"
    # 原因能区分来源未配置/不可达
    assert any("not_configured" in (v or "") or "unreachable" in (v or "")
               for v in out["reasons"].values())

    after = [dict(c) for c in s.tasks_of_root(rid)]
    tgt_failed_ids = {c["id"] for c in failed if _plan(c).get("branch") == target}
    assert tgt_failed_ids
    status_by_id = {c["id"]: c["status"] for c in after}
    # 仅原本失败的目标分支研究被重置为可重试
    assert all(status_by_id[i] == "ready" for i in tgt_failed_ids)
    # 其余研究节点未被碰：成功者仍 completed，其他分支失败者未被重试（仍 failed）
    preserved = [c for c in after
                 if _plan(c).get("node_kind") == "research" and c["id"] not in tgt_failed_ids]
    assert preserved and all(c["status"] in ("completed", "failed") for c in preserved), preserved
    assert any(c["status"] == "completed" for c in preserved), "应有已保留的成功证据"
    # 汇聚节点被重置为 pending，等待依赖完成后重算
    agg = [c for c in after if _plan(c).get("node_kind") in ("normalize", "verify", "compare")]
    assert agg and all(c["status"] == "pending" for c in agg)
    # 根的聚合不会有两个"已验证"重复结果（重算后仍是部分）
    final = _drive(eng, rid)
    assert final in ("partially_completed", "failed")


def test_retry_targets_koordinator_only(workdir):
    s = tmp_db(workdir)
    eng = s.engine()
    r = eng.create_goal("出差住宿标准", "team_ops")
    out = eng.retry_branch(r["task_id"], None)
    assert out.get("error") == "only_coordinator"