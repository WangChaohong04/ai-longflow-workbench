"""Batch8：审批绑定 root（不跨任务复用）、取消后迟到工具结果不覆盖、worker 连接独立。"""
from tests.conftest import workdir, tmp_db  # noqa: F401

import longflow.db as db
import longflow.orchestrator as orc
import longflow.permissions as perm
from longflow.models import WAITING_APPROVAL, COMPLETED, CANCELLED, FAILED


def test_approval_not_reusable_across_roots(workdir):
    """同参数 high 风险动作在 root A 审批后，root B 不得复用该批准。"""
    session = tmp_db(workdir)
    conn = session.conn
    # 在 root A 下建任务并产生一条已审批记录
    ta = db.insert_task(conn, title="A", kind="task", agent_role="executor",
                        objective="o", root_id="rootA")
    args = {"vendor": "approved-vendor", "amount": 8000, "item": "x"}
    ah = perm.args_hash("make_purchase", args)
    conn.execute(
        """INSERT INTO approvals (id, task_id, tool_name, args_json, args_hash, reason, status, created_at)
           VALUES ('apA', ?, 'make_purchase', '{}', ?, 'r', 'approved', ?)""",
        (ta, ah, db.now()))
    conn.commit()
    # root B 下同参数决策：不应命中 root A 的批准 -> NEEDS_APPROVAL
    tb = db.insert_task(conn, title="B", kind="task", agent_role="executor",
                        objective="o", root_id="rootB")
    dec = perm.decide(conn, "make_purchase", "high", args, task_id=tb, root_id="rootB")
    assert dec.verdict == perm.NEEDS_APPROVAL, "high 审批不得跨 root 复用"
    # root A 内同参数：命中批准
    decA = perm.decide(conn, "make_purchase", "high", args, task_id=ta, root_id="rootA")
    assert decA.verdict == perm.ALLOW


def test_after_tool_does_not_overwrite_cancelled(workdir):
    """工具返回时根已取消：_after_tool 不把任务写成 completed。"""
    session = tmp_db(workdir)
    session.ensure_scenario_knowledge("team_ops")
    eng = session.engine()
    # 高风险下单 -> 等待审批
    res = eng.create_goal("我要采购笔记本电脑 预算 8000元", "team_ops",
                          {"item": "笔记本电脑", "budget": 8000})
    root_id = res["task_id"]
    eng.tick(root_id)
    children = db.list_children(session.conn, root_id)
    waiting = [c for c in children if c.status == WAITING_APPROVAL]
    assert waiting
    # 直接调用 _after_tool 模拟工具迟到返回，但此时根已取消
    eng.cancel(root_id, reason="用户取消")
    task = waiting[0]
    # 根已取消后，迟到的工具结果写回应被守卫拦截（不抛异常、不改状态）
    eng._after_tool(task, "send_notification", {"sent": True})
    after = db.get_task(session.conn, task.id)
    assert after.status != COMPLETED, "迟到结果不得把已取消任务改成已完成"


def test_worker_engine_uses_own_connection(workdir):
    """AppState.worker_engine 返回的引擎使用独立连接（不共享请求连接）。"""
    from longflow import api as api_mod, config as cfg_mod
    cfg = cfg_mod.load_config()
    cfg["db_path"] = str(workdir / "w.db")
    state = api_mod.AppState(cfg)
    req_eng = state.engine()
    wrk_eng = state.worker_engine()
    assert wrk_eng.conn is not state.conn, "worker 引擎必须有独立连接"
    assert wrk_eng.runtime is not state.runtime, "worker 必须有独立 ToolRuntime"
    assert wrk_eng.runtime.conn is wrk_eng.conn, "工具/事件写入应走 worker 连接"
    wrk_eng.conn.close()
