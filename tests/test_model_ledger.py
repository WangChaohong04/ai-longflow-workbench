"""Batch4：模型调用账本——真实调用记录、模板/规则动作不冒充、预算拦截、降级标记。"""
from tests.conftest import workdir  # noqa: F401

import longflow.db as db
import longflow.model_ledger as ml
from longflow.plugins.sdk import ToolSpec


def test_ledger_records_fields_and_unknown_tokens_none(workdir):
    conn = db.connect(str(workdir / "l.db"))
    db.init_db(conn)
    ml.init_ledger(conn)
    mid = ml.record_call(conn, driver="openai_compatible", model="gpt-x",
                         call_type=ml.NEXT_ACTION, ok=True, latency_ms=123,
                         prompt_tokens=10, completion_tokens=5,
                         root_id="r1", task_id="t1")
    assert mid
    rows = conn.execute("SELECT * FROM llm_calls WHERE root_id='r1'").fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["driver"] == "openai_compatible"
    assert r["model"] == "gpt-x"
    assert r["call_type"] == "next_action"
    assert r["ok"] == 1
    assert r["latency_ms"] == 123
    assert r["prompt_tokens"] == 10
    assert r["completion_tokens"] == 5

    # token 未知 -> None，不写 0
    ml.record_call(conn, driver="openai_compatible", model="gpt-x",
                   call_type=ml.DRAFT, ok=True, latency_ms=50, root_id="r1")
    r2 = conn.execute("SELECT * FROM llm_calls WHERE call_type='draft_answer'").fetchone()
    assert r2["prompt_tokens"] is None and r2["completion_tokens"] is None


def test_budget_blocks_after_limit(workdir):
    conn = db.connect(str(workdir / "b.db"))
    db.init_db(conn)
    ml.init_ledger(conn)
    bud = ml.ModelBudget(conn, max_calls=2)
    bud.check("r")  # 0 次，通过
    ml.record_call(conn, driver="openai_compatible", model="m", call_type=ml.PLAN,
                   ok=True, root_id="r")
    ml.record_call(conn, driver="openai_compatible", model="m", call_type=ml.NEXT_ACTION,
                   ok=True, root_id="r")
    import pytest
    with pytest.raises(RuntimeError):
        bud.check("r")  # 第 3 次应被预算拦截


def test_timed_call_records_failure(workdir):
    conn = db.connect(str(workdir / "f.db"))
    db.init_db(conn)
    ml.init_ledger(conn)
    try:
        with ml.TimedCall(conn, driver="openai_compatible", model="m",
                          call_type=ml.NEXT_ACTION) as tc:
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    r = conn.execute("SELECT * FROM llm_calls").fetchone()
    assert r["ok"] == 0 and "boom" in r["error"]


def test_template_and_local_rules_not_counted_as_model(workdir):
    """本地规则驱动 + 模板动作：llm_calls 表为空（不冒充真实模型调用），
    但 llm_call 事件标注 real_model_call=False。"""
    from tests.conftest import BackendSession, tmp_db
    session = tmp_db(workdir)
    session.ensure_scenario_knowledge("team_ops")
    eng = session.engine()  # 默认 local 驱动
    res = eng.create_goal("出差住宿标准是多少？", "team_ops", {})
    eng.tick(res["task_id"])
    ml.init_ledger(session.conn)
    # 本地确定性驱动不应写任何真实模型调用
    n = session.conn.execute("SELECT COUNT(*) AS n FROM llm_calls").fetchone()["n"]
    assert n == 0, f"本地规则不应产生模型调用记录，实际 {n}"
    # llm_call 事件应显式标注非真实模型调用
    evs = session.conn.execute(
        "SELECT detail_json FROM events WHERE kind='llm_call'").fetchall()
    import json
    flagged = [json.loads(r["detail_json"]) for r in evs]
    assert flagged and all(e.get("real_model_call") is False for e in flagged)
