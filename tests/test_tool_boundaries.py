"""Batch3：工具执行边界——参数校验、工具重名拒绝、副作用超时待确认。"""
import time

import pytest

from longflow.plugins.sdk import ToolSpec, PluginError
from longflow.tools import ToolRegistry, ToolResultInDoubt


def _spec(**over):
    meta = dict(
        name="t", description="d", handler=lambda args, ctx: {"ok": True},
        risk="low", params_schema={
            "properties": {
                "q": {"type": "string", "required": True},
                "n": {"type": "integer", "minimum": 1, "maximum": 10},
                "mode": {"type": "string", "enum": ["a", "b"]},
                "ratio": {"type": "number", "minimum": 0, "maximum": 1},
                "tags": {"type": "array", "items": {"type": "string"}},
                "addr": {"type": "object", "properties": {
                    "city": {"type": "string", "required": True}}},
            },
        },
    )
    meta.update(over)
    return ToolSpec(**meta)


def test_required_missing_rejected():
    with pytest.raises(PluginError):
        _spec().validate_args({"n": 3})


def test_type_mismatch_rejected():
    with pytest.raises(PluginError):
        _spec().validate_args({"q": 123})  # q 应为 string


def test_integer_rejects_bool():
    with pytest.raises(PluginError):
        _spec().validate_args({"q": "x", "n": True})


def test_enum_rejected():
    with pytest.raises(PluginError):
        _spec().validate_args({"q": "x", "mode": "zzz"})


def test_range_rejected():
    with pytest.raises(PluginError):
        _spec().validate_args({"q": "x", "n": 99})
    with pytest.raises(PluginError):
        _spec().validate_args({"q": "x", "ratio": 1.5})


def test_nested_required_rejected():
    with pytest.raises(PluginError):
        _spec().validate_args({"q": "x", "addr": {}})  # addr.city 必填


def test_array_item_type_rejected():
    with pytest.raises(PluginError):
        _spec().validate_args({"q": "x", "tags": ["ok", 5]})


def test_valid_args_pass():
    _spec().validate_args({"q": "hi", "n": 5, "mode": "a", "ratio": 0.2,
                           "tags": ["x"], "addr": {"city": "武汉"}})


def test_duplicate_tool_name_rejected_silent_override():
    reg = ToolRegistry()
    s1 = _spec(name="dup")
    s2 = ToolSpec("dup", "不同工具/可自降 risk", lambda a, c: {}, risk="low")
    reg.register(s1)
    with pytest.raises(ValueError):
        reg.register(s2)  # 默认拒绝静默覆盖
    # 显式 override 才允许
    reg.register(s2, on_conflict="override")
    assert reg.get("dup").description == "不同工具/可自降 risk"


def test_identical_core_reregister_is_idempotent():
    reg = ToolRegistry()
    s1 = _spec(name="core")
    reg.register(s1)
    reg.register(_spec(name="core"))  # 完全相同 => 幂等，不报错


def test_side_effect_timeout_marks_in_doubt(workdir):
    """副作用工具超时：任务进入待确认（waiting_event + in_doubt），不抛成功也不盲重试。"""
    from tests.conftest import BackendSession, tmp_db
    session = tmp_db(workdir)

    def slow_side_effect(args, ctx):
        time.sleep(2.0)  # 超过 timeout
        return {"ordered": True}

    spec = ToolSpec("slow_buy", "慢副作用工具", slow_side_effect,
                    risk="low", side_effect=True,
                    params_schema={"properties": {"item": {"type": "string", "required": True}}})
    session.registry.register(spec, on_conflict="override")

    import longflow.orchestrator as orc
    import longflow.db as db
    from longflow.models import WAITING_EVENT
    # 直接调 runtime.call 验证抛 in-doubt
    conn = session.conn
    tid = db.insert_task(conn, title="x", kind="task", agent_role="executor",
                         objective="o", root_id="r1")
    task = db.get_task(conn, tid)
    session.runtime.timeout_seconds = 0.3
    with pytest.raises(ToolResultInDoubt):
        session.runtime.call("slow_buy", {"item": "x"}, task, reason="test")
