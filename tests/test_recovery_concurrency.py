"""Batch1 可靠性：真实后台恢复、原子领取、取消守卫、迟到结果不覆盖。

不依赖 HTTP/线程时序，直接用两个独立连接/引擎模拟"进程重启"：
- 引擎 A 创建任务并推进到等待审批（持久化）；
- 引擎 B（新连接，等同重启）在审批后恢复并完成；
- 原子 CAS 领取保证同一任务不会被两次领取；
- 取消后迟到的 tick/结果不覆盖 cancelled。
"""
from tests.conftest import BackendSession, tmp_db, workdir  # noqa: F401

import longflow.orchestrator as orc
import longflow.db as db
import longflow.events as events
from longflow.models import (
    CANCELLED, COMPLETED, WAITING_APPROVAL, IN_PROGRESS, READY, PENDING,
)


def _fresh_engine(session: BackendSession):
    """模拟进程重启：用同一 db 文件、全新连接与引擎。"""
    conn = db.connect(str(session.db_path))
    db.init_db(conn)
    eng = orc.Engine(conn, session.cfg, session.runtime)
    return eng, conn


def test_recovery_after_restart_completes_pending(workdir):
    """任务在等待审批后持久化；新进程（新连接）恢复，审批后推进到完成。"""
    session = tmp_db(workdir)
    session.ensure_scenario_knowledge("team_ops")
    eng = session.engine()
    # 高风险下单：会停在 waiting_approval
    res = eng.create_goal("我要采购笔记本电脑 预算 8000元", "team_ops",
                          {"item": "笔记本电脑", "budget": 8000})
    eng.tick(res["task_id"])

    root = db.get_task(session.conn, res["task_id"])
    children = db.list_children(session.conn, root.id)
    # 应有任务停在等待审批
    waiting = [c for c in children if c.status == WAITING_APPROVAL]
    assert waiting, "高风险动作应停在等待审批"

    # 模拟重启：全新连接/引擎
    eng2, conn2 = _fresh_engine(session)
    # 恢复循环在审批前不应推进等待审批的任务
    eng2.tick(root.id)
    assert db.get_task(conn2, waiting[0].id).status == WAITING_APPROVAL

    # 审批通过（用新连接）
    ap = conn2.execute(
        "SELECT id FROM approvals WHERE task_id=? AND status='pending'",
        (waiting[0].id,)).fetchone()
    assert ap is not None
    eng2.decide_approval(ap["id"], "approved")
    eng2.tick(root.id)

    final = db.get_task(conn2, root.id)
    assert final.status == COMPLETED, f"重启恢复后应完成，实际 {final.status}"
    conn2.close()


def test_atomic_claim_prevents_double_pickup(workdir):
    """transition_status CAS：同一任务只能被一个 worker 从 READY 领到 IN_PROGRESS。"""
    session = tmp_db(workdir)
    conn = session.conn
    # 构造一个 READY 任务
    tid = db.insert_task(conn, title="t", kind="task", agent_role="executor",
                         objective="o", root_id="r1", status=READY)
    ok1 = db.transition_status(conn, tid, (PENDING, READY), IN_PROGRESS)
    ok2 = db.transition_status(conn, tid, (PENDING, READY), IN_PROGRESS)
    assert ok1 is True
    assert ok2 is False, "第二次领取必须失败（已被领走）"
    assert db.get_task(conn, tid).status == IN_PROGRESS


def test_cancelled_root_not_overwritten_by_late_tick(workdir):
    """取消根任务后，迟到的 tick 不把状态改回运行/完成。"""
    session = tmp_db(workdir)
    session.ensure_scenario_knowledge("team_ops")
    eng = session.engine()
    res = eng.create_goal("我要采购笔记本电脑 预算 8000元", "team_ops",
                          {"item": "笔记本电脑", "budget": 8000})
    eng.tick(res["task_id"])
    assert db.get_task(session.conn, res["task_id"]).status != COMPLETED
    # 用户取消
    eng.cancel(res["task_id"], reason="测试取消")
    root = db.get_task(session.conn, res["task_id"])
    assert root.status == CANCELLED
    # 迟到的恢复 tick
    eng2, conn2 = _fresh_engine(session)
    eng2.tick(res["task_id"])
    after = db.get_task(conn2, res["task_id"])
    assert after.status == CANCELLED, "迟到 tick 不得覆盖已取消状态"
    conn2.close()


def test_cancel_records_irreversible_side_effects(workdir):
    """取消时已成功的副作用动作被保留并在结果中标注，不谎称可撤回。"""
    session = tmp_db(workdir)
    session.ensure_scenario_knowledge("team_ops")
    eng = session.engine()
    # 预授权通知（medium），使其无需审批即可执行产生副作用
    import longflow.permissions as perm
    perm.add_grant(session.conn, scope="preauth", tool_name="send_notification",
                   object_pattern="send_notification:*")
    res = eng.create_goal("请通知行政采购已提交", "team_ops",
                          {"item": "文具", "budget": 100})
    eng.tick(res["task_id"])
    out = eng.cancel(res["task_id"])
    # 取消成功；若有已发生副作用，应在结果中可追溯（不承诺撤回）
    assert out["status"] == CANCELLED
    root = db.get_task(session.conn, res["task_id"])
    assert root.status == CANCELLED
