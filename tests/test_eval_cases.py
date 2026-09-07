"""SPEC §10 回归评测用例的 pytest 收集入口。

run_eval.py 作为 Runner（也被 POST /api/eval/run 调用）不以 test_ 开头；
本文件把 run_all 的 10 个 yaml 用例接入 pytest 收集，使
``python -m pytest tests/ -q`` 直接跑完整回归（断言用代码，不用 LLM 打分）。
"""
from __future__ import annotations

import pytest

import conftest as cf

cf.skip_if_no_backend()

import run_eval  # noqa: E402

CASES = cf.all_cases()


@pytest.mark.eval
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_regression_case(case, workdir):
    result = run_eval.execute_case(case, workdir=workdir / case["id"])
    failed = [c for c in result["checks"] if not c["passed"]]
    if failed:
        lines = [f"用例 {case['id']}（{case['name']}）失败检查:"]
        for c in failed:
            lines.append(f"  - [{c['name']}] {c['detail']}")
        if result.get("notes"):
            lines.append("适配/说明: " + " | ".join(result["notes"][:4]))
        pytest.fail("\n".join(lines), pytrace=False)


def test_run_all_contract(workdir):
    """run_all 返回 SPEC §10 规定的结构（total/passed/cases[id,name,passed,checks]）。"""
    summary = run_eval.run_all()
    assert set(summary) >= {"total", "passed", "cases"}
    assert summary["total"] == len(CASES) == 10
    assert 0 <= summary["passed"] <= summary["total"]
    for c in summary["cases"]:
        assert set(c) >= {"id", "name", "passed", "checks"}
        for ch in c["checks"]:
            assert set(ch) >= {"name", "passed", "detail"}
