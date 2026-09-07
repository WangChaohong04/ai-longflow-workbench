"""用例数据静态校验（不依赖后端实现，任何时候都应通过）。

覆盖 SPEC §10 用例文件格式：id/name/scenario/goal/expect，
以及 checks/flow/grants 的结构合法性与检查名可解析性。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import conftest as cf
import run_eval

CASES_DIR = Path(__file__).resolve().parent / "cases"
CASE_FILES = sorted(CASES_DIR.glob("*.yaml"))

EXPECTED_IDS = {
    "e2e_citation", "missing_info_clarify", "no_evidence", "source_conflict",
    "tool_failure_not_success", "permission_denied", "approval_parallel_resume",
    "restart_recovery_idempotency", "plugin_adds_tool", "geo_spatial",
}


def test_case_files_present():
    ids = {p.stem for p in CASE_FILES}
    missing = EXPECTED_IDS - ids
    assert not missing, f"缺少 SPEC §10 要求的用例文件: {sorted(missing)}"
    assert len(CASE_FILES) >= 10


@pytest.mark.parametrize("path", CASE_FILES, ids=[p.stem for p in CASE_FILES])
def test_case_yaml_structure(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} 顶层必须是映射"
    for key in ("id", "name", "scenario", "goal", "expect"):
        assert key in data, f"{path.name} 缺少字段 {key}"
    assert data["id"] == path.stem, f"id 与文件名不一致: {data['id']} vs {path.stem}"
    assert data["scenario"] in ("team_ops", "geo_site"), f"未知场景: {data['scenario']}"
    assert isinstance(data["goal"], str) and data["goal"].strip()
    expect = data["expect"]
    assert isinstance(expect, dict) and expect, "expect 必须是非空映射"


@pytest.mark.parametrize("path", CASE_FILES, ids=[p.stem for p in CASE_FILES])
def test_case_checks_resolvable(path):
    """expect 中每个检查名必须能在 run_eval.CHECKS 中解析（SPEC §10 断言用代码）。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    expect = data["expect"]
    names = []
    if isinstance(expect.get("checks"), list):
        names.extend(c["name"] for c in expect["checks"] if isinstance(c, dict))
    for k, v in expect.items():
        if k == "checks" or v is None:
            continue
        names.append(k)
    resolved = set()
    for n in names:
        aliased = run_eval._iter_check_specs({k: v for k, v in expect.items() if k == n})
        for name, _arg in aliased:
            resolved.add(name)
            assert name in run_eval.CHECKS, f"{path.name}: 检查 {n!r} 无对应实现"
    assert resolved, f"{path.name}: 未解析出任何检查"


@pytest.mark.parametrize("path", CASE_FILES, ids=[p.stem for p in CASE_FILES])
def test_case_flow_steps_valid(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    valid_kinds = {"tick", "drive", "run", "wait_approval", "await_approval",
                   "approve", "decide", "restart", "disable_plugin", "snapshot"}
    for step in data.get("flow") or []:
        assert isinstance(step, dict), f"{path.name}: flow 步骤必须是映射"
        kind = step.get("do") or step.get("step") or step.get("type")
        assert kind in valid_kinds, f"{path.name}: 未知 flow 步骤 {kind!r}"
        if kind in ("approve", "decide"):
            assert step.get("decision", "approved") in ("approved", "rejected")
    for g in data.get("grants") or []:
        assert g.get("scope") in ("auto", "preauth", "once", "deny"), \
            f"{path.name}: 非法 grant scope {g.get('scope')!r}"


def test_all_case_ids_unique():
    ids = [yaml.safe_load(p.read_text(encoding="utf-8"))["id"] for p in CASE_FILES]
    assert len(ids) == len(set(ids)), "用例 id 重复"


def test_run_eval_api_contract():
    """run_all 契约（SPEC §10 / /api/eval/run）：结构完整、字段类型正确。"""
    assert callable(run_eval.run_all)
    import inspect
    sig = inspect.signature(run_eval.run_all)
    assert "db_path" in sig.parameters, "run_all 必须提供 db_path 参数"
    assert callable(run_eval.main)
