"""闭环 e2e 测试：orchestrator + 工具 + 权限 + RAG + 插件真实跑通（SPEC §6/§10）。

后端尚未实现时整模块跳过（后端实现完成后自动运行）。
覆盖 §10 全部 10 个用例：run_eval 的参数化测试逐例执行完整闭环并断言 checks；
本文件另用 conftest 助手（tmp_db/run_goal/restart）对关键契约做独立交叉验证。

注意：临时目录用 conftest 的 ``workdir`` fixture（工作区内 .lfwork/），
不使用 pytest 内置 tmp_path——受限沙箱下 tmp_path 工厂扫描系统临时目录会被拒。
"""
from __future__ import annotations

import conftest as cf

cf.skip_if_no_backend()

import json  # noqa: E402

import pytest  # noqa: E402

import run_eval  # noqa: E402


# ---------------------------------------------------------------------------
# conftest 助手契约
# ---------------------------------------------------------------------------

def test_tmp_db_initializes_stack(workdir):
    """tmp_db：db init + RAG + 插件 + 核心工具全部就绪。"""
    session = cf.tmp_db(workdir / "db")
    try:
        names = session.tool_names()
        for core in ("kb_search", "make_purchase", "send_notification"):
            assert core in names, f"核心工具 {core} 未注册: {names}"
        # 知识库已加载（SPEC §4：启动加载 knowledge 到 knowledge_chunks）
        (cnt,) = session.conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()
        assert cnt > 0, "knowledge_chunks 为空（RAG 未加载场景知识）"
    finally:
        session.close()


def test_run_goal_returns_full_trace(workdir):
    """run_goal：建 root → 循环 run_root → 返回 root/events/approvals。"""
    session = cf.tmp_db(workdir / "db")
    try:
        out = cf.run_goal(session, "公司采购笔记本电脑的审批规则是什么？", "team_ops")
        assert out["root"] is not None
        assert out["root"]["root_id"] == out["root_id"]
        kinds = {e["kind"] for e in out["events"]}
        assert "task_created" in kinds
        assert out["root"]["status"] in cf.TERMINAL or \
            any(h in out["root"]["status"] for h in cf.CLARIFY_STATUS_HINTS)
    finally:
        session.close()


def test_restart_reopens_same_db(workdir):
    """restart：新连接重开同一 DB 文件，任务与事件跨"进程"可见（SPEC §6 可恢复）。"""
    s1dir = workdir / "s1"
    s1 = cf.tmp_db(s1dir)
    rid = None
    n_events_before = 0
    try:
        rid = s1.create_root("公司采购审批规则是什么？", "team_ops")
        cf._drive(s1, rid)
        n_events_before = len(s1.events(rid))
    finally:
        s1.close()
    # 模拟进程重启：新 BackendSession 打开同一 DB 文件（内存状态全丢）
    s2 = cf.restart(s1dir / "longflow.db", rid)
    try:
        root = s2.task(rid)
        assert root is not None, "重启后 root 任务丢失"
        assert root["id"] == rid
        assert len(s2.events(rid)) >= n_events_before, "重启后事件日志丢失"
    finally:
        s2.close()


# ---------------------------------------------------------------------------
# 关键契约交叉验证（独立于 run_eval 的 check 实现）
# ---------------------------------------------------------------------------

def test_geo_distances_independently_recomputed(workdir):
    """用例 10 交叉验证：geo_radius_search 结果用 conftest.haversine 独立复算。"""
    session = cf.tmp_db(workdir / "db")
    try:
        names = session.tool_names()
        if "geo_radius_search" not in names:
            pytest.skip(f"geo 插件未加载: {names}")
        ok, geo = session.call_tool("geo_geocode", {"location": "中关村"},
                                    task_id="t_probe", root_id="t_probe", scenario="geo_site")
        assert ok, geo
        coords = geo.get("coords") or geo.get("coordinates") or geo
        lon, lat = float(coords.get("lon", coords.get("lng"))), float(coords.get("lat"))
        ok, res = session.call_tool(
            "geo_radius_search",
            {"center": {"lon": lon, "lat": lat}, "radius_km": 2,
             "filters": {"category": "咖啡馆"}, "sort_by": "distance"},
            task_id="t_probe", root_id="t_probe", scenario="geo_site")
        assert ok, res
        cands = res.get("candidates") or res.get("results")
        assert cands, "半径内无候选"
        dists = []
        for c in cands:
            p = run_eval._norm_point(c)
            d = cf.haversine_km(lon, lat, p["lon"], p["lat"])
            dists.append(d)
            assert d <= 2.0 + 1e-6, f"{p['name']} 距离 {d:.3f}km 超出半径"
        assert dists == sorted(dists), "候选未按距离排序"
        assert "4326" in str(res.get("crs", "")), f"crs 标注异常: {res.get('crs')}"
        assert res.get("source"), "结果缺少 source 标注"
    finally:
        session.close()


def test_route_mode_never_fabricates_time(workdir):
    """用例 10 交叉验证：geo_distance route 模式 supported:false 且无时间字段。"""
    session = cf.tmp_db(workdir / "db")
    try:
        if "geo_distance" not in session.tool_names():
            pytest.skip("geo 插件未加载")
        ok, res = session.call_tool(
            "geo_distance",
            {"a": {"lon": 116.31, "lat": 39.98}, "b": {"lon": 116.40, "lat": 39.90},
             "mode": "route"},
            task_id="t_probe", root_id="t_probe", scenario="geo_site")
        assert ok, res
        assert res.get("supported") is False, f"route 应 supported:false: {res}"
        for k in ("duration_min", "travel_time", "commute_time", "duration"):
            assert not res.get(k), f"route 不支持却编造 {k}: {res}"
    finally:
        session.close()


def test_deny_grant_blocks_purchase(workdir):
    """用例 6 交叉验证：deny grant → tool_denied 事件 + 无成功采购。"""
    session = cf.tmp_db(workdir / "db")
    try:
        out = cf.run_goal(
            session, "帮我采购一批办公椅，预算5000元", "team_ops",
            slots={"item": "办公椅", "budget": "5000"},
            grants=[{"scope": "deny", "tool_name": "make_purchase"}])
        denied = [e for e in out["events"] if e["kind"] == "tool_denied"]
        assert denied, f"预期 tool_denied 事件，实际事件种类: {sorted({e['kind'] for e in out['events']})}"
        assert all((e.get("detail") or {}).get("tool_name") in ("make_purchase", None) for e in denied)
        # 绝无成功采购
        ok_results = [e for e in out["events"] if e["kind"] == "tool_result"
                      and (e.get("detail") or {}).get("tool_name") == "make_purchase"
                      and (e.get("detail") or {}).get("ok", True) is not False]
        assert not ok_results, "deny 下不得有成功 make_purchase"
    finally:
        session.close()


def test_idempotency_key_survives_restart(workdir):
    """用例 8 交叉验证：批准→重启→再 run_root，make_purchase 副作用仅一次。"""
    s1dir = workdir / "case8"
    session = cf.tmp_db(s1dir)
    rid = None
    db_file = s1dir / "longflow.db"
    try:
        rid = session.create_root("采购1台打印机（预算2000元，供应商 office-supplies）",
                                  "team_ops", slots={"item": "打印机", "budget": "2000",
                                                     "vendor": "office-supplies"})
        cf._drive(session, rid)
        pending = session.pending_approvals(rid)
        if not pending:
            pytest.skip("采购分支未进入 waiting_approval（场景模板/驱动差异）")
        appr = pending[0]
        bound = json.loads(appr["args_json"] or "{}")
        dec = session.decide_approval(appr["id"], "approved", args=bound)
        assert dec["ok"], dec
    finally:
        session.close()

    # 重启 1：新连接，内存状态全丢
    s2 = cf.restart(db_file, rid)
    s2.close()
    # 重启 2：再次重入（幂等键命中，不得第二次执行副作用）
    s3 = cf.restart(db_file, rid)
    try:
        ev = s3.events(rid)
        ok_purchase = [e for e in ev if e["kind"] == "tool_result"
                       and (e.get("detail") or {}).get("tool_name") == "make_purchase"
                       and (e.get("detail") or {}).get("ok", True) is not False
                       and not (e.get("detail") or {}).get("error")]
        assert len(ok_purchase) == 1, \
            f"make_purchase 副作用执行 {len(ok_purchase)} 次（幂等键应保证 1 次）"
        decided = [e for e in ev if e["kind"] == "approval_decided"]
        assert any((e.get("detail") or {}).get("decision") == "approved" for e in decided)
    finally:
        s3.close()
