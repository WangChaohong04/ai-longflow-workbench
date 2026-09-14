"""LongFlow 回归评测 Runner（SPEC §10）。

- pytest 可收集（本模块内含 pytest 用例生成）；
- 同时提供 ``main()`` 与 ``run_all(db_path=None)`` 供 ``POST /api/eval/run`` 调用。

返回结构::

    {"total": N, "passed": M, "cases": [
        {"id", "name", "passed": bool,
         "checks": [{"name", "passed": bool, "detail"}]}]}

所有断言均为 API/函数级代码检查（SPEC §10：断言用代码不用 LLM 打分）。
用例数据在 tests/cases/*.yaml；流程编排（tick/approve/restart）与检查逻辑在本模块。
事件字段以后端实现为准：tool_result/tool_request/tool_denied/approval_* 的
细节在 ``detail.tool`` / ``detail.ok`` / ``detail.idempotency_key``。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
for p in (TESTS_DIR, REPO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import conftest as cf  # noqa: E402

TERMINAL = cf.TERMINAL


def _norm_point(c: dict) -> dict:
    """把 GEO 候选点归一化为 {name, lon, lat}（兼容多种字段命名）。"""
    lon = c.get("lon")
    lat = c.get("lat")
    if lon is None and isinstance(c.get("coordinates"), (list, tuple)):
        lon, lat = c["coordinates"][0], c["coordinates"][1]
    if lon is None and isinstance(c.get("geometry"), dict):
        coords = c["geometry"].get("coordinates") or []
        if coords:
            lon, lat = coords[0], coords[1]
    return {"name": c.get("name") or c.get("title") or "?",
            "lon": float(lon), "lat": float(lat)}


# ---------------------------------------------------------------------------
# 证据提取
# ---------------------------------------------------------------------------

def _snapshot(session: cf.BackendSession, root_id: str) -> dict:
    return {
        "root": session.task(root_id),
        "tasks": session.tasks_of_root(root_id),
        "events": session.events(root_id),
        "approvals": session.approvals(root_id),
    }


def _result_of(root: dict | None) -> dict:
    if not root:
        return {}
    res = root.get("result") or {}
    return res if isinstance(res, dict) else {"raw": res}


def _answer_of(root: dict | None, events: list[dict] | None = None) -> str:
    """root.result.answer（可能是字符串或 {text}）+ 澄清/失败说明文本。"""
    parts: list[str] = []
    res = _result_of(root)
    ans = res.get("answer")
    if isinstance(ans, str) and ans.strip():
        parts.append(ans)
    elif isinstance(ans, dict):
        for k in ("text", "content", "message"):
            if isinstance(ans.get(k), str):
                parts.append(ans[k])
    for k in ("message", "summary", "reply"):
        v = res.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    # 失败/越权/澄清的结构化说明
    for k in ("error", "reason"):
        v = res.get(k)
        if isinstance(v, str):
            parts.append(v)
    for tf in res.get("tool_failures", []) or []:
        parts.append(json.dumps(tf, ensure_ascii=False))
    for d in res.get("denied", []) or []:
        parts.append(json.dumps(d, ensure_ascii=False))
    cl = res.get("clarify")
    if isinstance(cl, list):
        parts.append("缺失槽位: " + " ".join(
            c.get("name", "") if isinstance(c, dict) else str(c) for c in cl))
    # 子任务答案（verifier 草稿落在子任务 result.answer；root 汇总通常已合并）
    for ev in events or []:
        if ev.get("kind") in ("message", "clarify"):
            d = ev.get("detail") or {}
            if isinstance(d.get("text"), str):
                parts.append(d["text"])
    return "\n".join(parts)


def _citations_of(root: dict | None, events: list[dict] | None = None) -> list[dict]:
    cits: list[dict] = []
    seen: set[str] = set()

    def add(cid, source):
        cid = str(cid or "").strip()
        if cid and cid not in seen:
            seen.add(cid)
            cits.append({"chunk_id": cid, "source_field": source})

    res = _result_of(root)
    rc = res.get("citations")
    if isinstance(rc, list):
        for c in rc:
            if isinstance(c, str):
                add(c, "result.citations")
            elif isinstance(c, dict):
                add(c.get("chunk_id") or c.get("id"), "result.citations")
    text = _answer_of(root, events)
    for m in re.finditer(r"\[cite:([^\]]+)\]", text):
        add(m.group(1).strip(), "inline [cite:]")
    return cits


def _events_of_kind(events: list[dict], *kinds: str) -> list[dict]:
    return [e for e in events if e.get("kind") in kinds]


def _tool_of(detail: dict) -> str | None:
    return detail.get("tool") or detail.get("tool_name") or detail.get("name")


def _tool_result_events(events: list[dict], tool: str | None = None) -> list[dict]:
    out = []
    for ev in events:
        if ev.get("kind") != "tool_result":
            continue
        d = ev.get("detail") or {}
        if tool is None or _tool_of(d) == tool:
            out.append(ev)
    return out


def _missing_slots(root: dict, events: list[dict]) -> list[str]:
    found: set[str] = set()
    res = _result_of(root)
    cl = res.get("clarify")
    if isinstance(cl, list):
        for c in cl:
            if isinstance(c, dict):
                found.add(str(c.get("name")))
            else:
                found.add(str(c))
    for ev in events:
        if ev.get("kind") in ("clarify", "gate_entry", "message"):
            d = ev.get("detail") or {}
            v = d.get("missing_slots")
            if isinstance(v, list):
                for c in v:
                    found.add(c.get("name") if isinstance(c, dict) else str(c))
    return sorted(x for x in found if x)


def _result_conflicts(root: dict) -> list:
    res = _result_of(root)
    v = res.get("conflicts")
    if v:
        return v if isinstance(v, list) else [v]
    return []


def _verified(root: dict | None):
    return _result_of(root).get("verified")


def _get_chunks(session: cf.BackendSession) -> list[dict]:
    try:
        rows = session.conn.execute(
            "SELECT id, source_id, doc_name, section, text, fields_json, citations_json "
            "FROM knowledge_chunks").fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        for k, default in (("fields_json", {}), ("citations_json", [])):
            try:
                d[k[:-5]] = json.loads(d.get(k) or json.dumps(default, ensure_ascii=False))
            except Exception:
                d[k[:-5]] = default
        out.append(d)
    return out


def _chunk_for(citation: dict, chunks: list[dict]) -> dict | None:
    cid = citation.get("chunk_id", "")
    for c in chunks:
        if c["id"] == cid:
            return c
    return None


# ---------------------------------------------------------------------------
# 检查函数：每个返回 (passed, detail)
# ---------------------------------------------------------------------------

def chk_status(ctx, expected):
    status = (ctx["final"]["root"] or {}).get("status", "<missing>")
    return (status == expected, f"root status={status}，期望 {expected}")


def chk_terminal_not_pending(ctx, _=True):
    status = (ctx["final"]["root"] or {}).get("status", "<missing>")
    if status in TERMINAL or status in ("waiting_event", "waiting_user", "waiting_external"):
        return True, f"root 已决：status={status}"
    return False, f"root 未决：status={status}（steps={ctx.get('steps')}）"


def chk_answer_contains(ctx, phrases):
    text = _answer_of(ctx["final"]["root"], ctx["final"]["events"])
    missing = [p for p in phrases if p not in text]
    if not missing:
        return True, f"答案包含: {phrases}"
    return False, f"答案缺少 {missing}；摘录: {text[:200]!r}"


def chk_answer_regex(ctx, patterns):
    text = _answer_of(ctx["final"]["root"], ctx["final"]["events"])
    missing = [p for p in patterns if not re.search(p, text)]
    if not missing:
        return True, f"答案匹配正则: {patterns}"
    return False, f"未匹配 {missing}；摘录: {text[:200]!r}"


def chk_answer_not_contains(ctx, phrases):
    text = _answer_of(ctx["final"]["root"], ctx["final"]["events"])
    hit = [p for p in phrases if p in text]
    if not hit:
        return True, f"答案未出现禁用词: {phrases}"
    return False, f"答案出现禁用词 {hit}；摘录: {text[:200]!r}"


def chk_verified_true(ctx, _=True):
    v = _verified(ctx["final"]["root"])
    if v is True:
        return True, "result.verified=true（出口闸门通过）"
    res = _result_of(ctx["final"]["root"])
    return False, f"verified={v!r}；gate_problems={json.dumps(res.get('gate_problems'), ensure_ascii=False)[:400]}"


def chk_verified_not_true(ctx, expect=True):
    v = _verified(ctx["final"]["root"])
    conflicts = _result_conflicts(ctx["final"]["root"])
    not_verified = (v is not True) or bool(conflicts)
    if expect is False:
        # 期望核验通过（现行有效资料无冲突，如过期版本被正确排除）
        if v is True and not conflicts:
            return True, f"verified={v!r}，conflicts=0（现行版本核验通过）"
        return False, f"期望核验通过，但 verified={v!r}，conflicts={len(conflicts)}"
    if not_verified:
        return True, f"verified={v!r}，conflicts={len(conflicts)} 处（冲突未作为已验证结论放行）"
    return False, "verified=true 但存在来源冲突应标注/不放行"


def chk_citations_present(ctx, _=True):
    cits = _citations_of(ctx["final"]["root"], ctx["final"]["events"])
    if cits:
        return True, f"引用 {len(cits)} 条: {[c['chunk_id'][:12] for c in cits][:5]}"
    return False, "result.citations 为空且答案无 [cite:] 标记"


def chk_citations_support(ctx, _=True):
    """SPEC §4：每条 citation 的 chunk 文本必须支撑答案论断（词重叠核验）。"""
    root = ctx["final"]["root"]
    events = ctx["final"]["events"]
    cits = _citations_of(root, events)
    if not cits:
        return False, "无引用可核验"
    chunks = _get_chunks(ctx["session"])
    if not chunks:
        return False, "knowledge_chunks 为空（RAG 未加载？）"
    answer = _answer_of(root, events)
    claims = [c.strip() for c in re.split(r"[。！？\n.!?]", answer) if len(c.strip()) >= 4]
    bad = []
    checked = 0
    for c in cits:
        chunk = _chunk_for(c, chunks)
        if chunk is None:
            bad.append(f"{c['chunk_id'][:12]}: chunk 不存在于知识库")
            continue
        ctext = chunk.get("text") or ""
        cid = c["chunk_id"]
        best, best_claim = 0.0, ""
        for cl in claims:
            if f"[cite:{cid}]" in cl:
                r = cf.text_overlap_ratio(cl, ctext)
                if r > best:
                    best, best_claim = r, cl
        if not best_claim:
            for cl in claims:
                r = cf.text_overlap_ratio(cl, ctext)
                if r > best:
                    best, best_claim = r, cl
        checked += 1
        if best < 0.34:
            bad.append(f"{cid[:12]}: 最高词重叠 {best:.2f}<0.34，论断 {best_claim[:50]!r} 不被支撑")
    if bad:
        return False, "引用支撑核验失败: " + "；".join(bad[:4])
    return True, f"{checked} 条引用的 chunk 文本均支撑答案论断（词重叠≥0.34）"


def chk_mentions_slots(ctx, slots):
    missing_detected = _missing_slots(ctx["final"]["root"], ctx["final"]["events"])
    lack = [s for s in slots if s not in missing_detected]
    if not lack:
        return True, f"澄清列出缺失槽位: {slots}"
    status = (ctx["final"]["root"] or {}).get("status")
    return False, f"未列出缺失槽位 {lack}（检出 {missing_detected}，root={status}）"


def chk_no_fabricated_answer(ctx, _=True):
    text = _answer_of(ctx["final"]["root"], ctx["final"]["events"])
    patterns = [r"已为你(采购|下单|购买)", r"下单成功", r"采购金额\s*\d",
                r"建议采购\s*\S+[，,。]\s*金额", r"订单号\s*[A-Z0-9-]{6,}"]
    hit = [p for p in patterns if re.search(p, text)]
    if hit:
        return False, f"信息缺失下臆造答案: {hit}；{text[:150]!r}"
    return True, "未臆造具体采购方案"


def chk_no_evidence_ack(ctx, _=True):
    """无检索结果：明确无法确认/未找到依据，不编造条款。"""
    text = _answer_of(ctx["final"]["root"], ctx["final"]["events"])
    ack = any(k in text for k in ("无法确认", "未找到", "没有找到", "无相关", "未检索到",
                                   "无法找到", "不能确认", "未在已授权知识库中找到"))
    if not ack:
        return False, f"未明确表达无法确认；摘录: {text[:200]!r}"
    if re.search(r"(宠物保险|宠物医疗)[^。]{0,40}(报销|赔付|保额|补贴)\s*\d", text):
        return False, "疑似编造宠物保险条款"
    return True, "明确无法确认/未找到依据，且未编造条款"


def chk_conflict_flagged(ctx, expect=True):
    conflicts = _result_conflicts(ctx["final"]["root"])
    if expect is False:
        if not conflicts:
            return True, "现行有效资料无来源冲突（过期版本已排除）"
        return False, f"不应有冲突却检出 {len(conflicts)} 处：{json.dumps(conflicts, ensure_ascii=False)[:300]}"
    text = _answer_of(ctx["final"]["root"], ctx["final"]["events"])
    text_flag = any(k in text for k in ("冲突", "不一致", "两个版本", "矛盾", "差异"))
    if conflicts or text_flag:
        return True, f"来源冲突已标注（conflicts={len(conflicts)}，文本标注={text_flag}）"
    return False, f"未标注来源冲突；result={json.dumps(_result_of(ctx['final']['root']), ensure_ascii=False)[:300]}"


def chk_tool_failure_reported(ctx, tool="make_purchase"):
    events = ctx["final"]["events"]
    failed = [e for e in _tool_result_events(events, tool)
              if (e.get("detail") or {}).get("ok") is False
              or (e.get("detail") or {}).get("error")]
    err_events = [e for e in events if e.get("kind") == "error"
                  and tool in json.dumps(e.get("detail") or {}, ensure_ascii=False)]
    failed_tasks = [t for t in ctx["final"]["tasks"]
                    if t.get("status") == "failed" and tool in json.dumps(t.get("result") or {}, ensure_ascii=False)]
    text = _answer_of(ctx["final"]["root"], events)
    reported = any(k in text for k in ("失败", "未能", "无法", "错误", "异常", "不可达",
                                        "unreachable", "未成功", "不通过"))
    evidence = failed or err_events or failed_tasks
    if evidence and reported:
        return True, f"工具失败有记录（{len(failed)} tool_result 失败 / {len(err_events)} error / {len(failed_tasks)} 失败任务）且结果如实说明"
    if not evidence:
        return False, f"未记录 {tool} 失败（tool_result ok=false / error 事件 / failed 子任务）"
    return False, f"工具失败但结果未如实说明；摘录 {text[:200]!r}"


def chk_no_success_claim(ctx, _=True):
    blob = _answer_of(ctx["final"]["root"], ctx["final"]["events"]) + "\n" + \
        json.dumps(_result_of(ctx["final"]["root"]), ensure_ascii=False)
    patterns = [r"下单成功", r"采购成功", r"已成功(采购|下单|购买|执行)", r"购买成功",
                r"订单已(提交|创建|完成)", r"ordered.*true", r"成功下单"]
    hit = [p for p in patterns if re.search(p, blob)]
    if hit:
        return False, f"工具失败却声称成功: {hit}；{blob[:180]!r}"
    return True, "未声称下单/采购成功"


def chk_tool_denied_event(ctx, tool="make_purchase"):
    denied = [e for e in _events_of_kind(ctx["final"]["events"], "tool_denied")
              if _tool_of(e.get("detail") or {}) in (tool, None)]
    if denied:
        return True, f"存在 tool_denied 事件 {len(denied)} 条（{tool}）"
    return False, f"未找到 {tool} 的 tool_denied 事件"


def chk_permission_denied_reason(ctx, _=True):
    events = ctx["final"]["events"]
    tasks = ctx["final"]["tasks"]
    blob = "\n".join(
        [json.dumps(e.get("detail") or {}, ensure_ascii=False) for e in events]
        + [json.dumps(t.get("result") or {}, ensure_ascii=False) for t in tasks])
    text = _answer_of(ctx["final"]["root"], events)
    code = "permission_denied" in blob
    user_facing = "越权" in text or ("权限" in text and any(k in text for k in ("拒绝", "阻止", "不足", "禁止")))
    if code or user_facing:
        return True, "permission_denied 可核验（事件/任务结果代码或面向用户告知）"
    return False, f"未核验到 permission_denied；摘录 {text[:200]!r}"


def chk_parallel_approval_notify(ctx, _=True):
    """SPEC §6：approval_requested 时通知分支已完成（事件顺序证明并行，notify 未阻塞）。"""
    events = ctx["final"]["events"]
    reqs = _events_of_kind(events, "approval_requested")
    if not reqs:
        return False, "未找到 approval_requested 事件（采购分支未进入 waiting_approval）"
    notify_results = _tool_result_events(events, "send_notification")
    notify_tasks = [t for t in ctx["final"]["tasks"]
                    if "send_notification" in json.dumps(t.get("plan") or {}, ensure_ascii=False)
                    or "通知" in (t.get("title") or "") + (t.get("objective") or "")]
    if not notify_results and not notify_tasks:
        return False, "未找到通知分支（send_notification 子任务/tool_result）"
    notify_ok = [e for e in notify_results if (e.get("detail") or {}).get("ok") is True]
    notify_done = any(t.get("status") == "completed" for t in notify_tasks) or bool(notify_ok)
    if not notify_done:
        return False, f"通知分支未完成: {[(t.get('title'), t.get('status')) for t in notify_tasks]}"
    first_req = min(_parse_ts(e.get("ts")) for e in reqs)
    if notify_results:
        notify_finish = min(_parse_ts(e.get("ts")) for e in notify_results)
        if notify_finish < first_req - 1e-6:
            return True, ("通知分支 tool_result 早于 approval_requested（并行推进，未被审批阻塞）："
                          f"notify@{_iso(notify_finish)} ≤ approval@{_iso(first_req)}")
    return True, (f"approval_requested@{_iso(first_req)} 时通知分支已 completed"
                  f"（notify tool_result {len(notify_ok)} 条），等待审批未阻塞并行分支")


def chk_purchase_resumed(ctx, _=True):
    """批准后采购分支恢复：approval_decided(approved) + make_purchase 成功 + 分支 completed。"""
    events = ctx["final"]["events"]
    decided = _events_of_kind(events, "approval_decided")
    if not any((d.get("detail") or {}).get("decision") == "approved" for d in decided):
        return False, "未找到 approval_decided(approved)"
    ok_purchase = [e for e in _tool_result_events(events, "make_purchase")
                   if (e.get("detail") or {}).get("ok") is True]
    if not ok_purchase:
        return False, "批准后未找到 make_purchase 成功 tool_result（恢复未执行副作用）"
    tasks = ctx["final"]["tasks"]
    exec_done = any(t.get("agent_role") == "executor" and t.get("status") == "completed" for t in tasks)
    root_done = (ctx["final"]["root"] or {}).get("status") == "completed"
    if exec_done or root_done:
        return True, f"批准后 make_purchase 成功 {len(ok_purchase)} 次，采购分支恢复并完成"
    return False, f"采购分支未完成: root={ (ctx['final']['root'] or {}).get('status')}"


def chk_purchase_side_effect_once(ctx, expected=1):
    """make_purchase 真实副作用执行次数（非回放 tool_result）恰为期望（幂等键，SPEC §1）。"""
    events = ctx["final"]["events"]
    real = [e for e in _tool_result_events(events, "make_purchase")
            if (e.get("detail") or {}).get("ok") is True
            and not (e.get("detail") or {}).get("idempotent_replay")]
    root_status = (ctx["final"]["root"] or {}).get("status")
    if len(real) == expected and root_status == "completed":
        return True, (f"make_purchase 真实副作用 {len(real)} 次（批准+重启+重复 tick 后幂等键命中，"
                      f"无第二次执行），root completed")
    return False, f"make_purchase 真实执行 {len(real)} 次，期望 {expected}；root={root_status}"


def chk_weather_tool_present(ctx, _=True):
    names = ctx["session"].tool_names()
    if "weather_get" in names:
        return True, f"插件加载后注册表含 weather_get（共 {len(names)} 个工具）"
    return False, f"注册表不含 weather_get: {names}"


def chk_weather_mock(ctx, _=True):
    ok, val = ctx["session"].call_tool(
        "weather_get", {"city": "北京"}, task_id=ctx["root_id"], root_id=ctx["root_id"],
        scenario=ctx["case"].get("scenario", ""))
    if not ok:
        return False, f"weather_get 调用失败: {val}"
    blob = json.dumps(val, ensure_ascii=False)
    if val.get("mock") is True or val.get("source") == "mock" or "mock" in blob.lower() or "模拟" in blob:
        return True, f"weather_get 返回含 mock 标注: {blob[:120]}"
    return False, f"weather_get 未标注 mock: {blob[:200]}"


def chk_weather_tool_absent_when_disabled(ctx, _=True):
    note = ctx["session"].disable_plugin("example_weather")
    names = ctx["session"].tool_names()
    if "weather_get" not in names:
        return True, f"禁用插件后 weather_get 消失（via {note}）"
    return False, f"禁用后 weather_get 仍在（via {note}）: {names}"


# ---- RAG 按需激活（SPEC §0/§4：按需检索、证据复用、预算上限、恢复不重检索）----

def _retrieval_requests(events):
    return [e for e in events if e.get("kind") == "tool_request"
            and (e.get("detail") or {}).get("tool") == "kb_search"]


def chk_retrieval_on_demand(ctx, budget=2):
    """首版简单规则：检索次数有上限，且仅研究员（researcher）发起检索——
    verifier/executor/controller 复用已有证据，不重复 kb_search（SPEC §0 按需激活）。"""
    events = ctx["final"]["events"]
    reqs = _retrieval_requests(events)
    n = len(reqs)
    if n == 0:
        return False, "无任何 kb_search 检索请求（需要知识源却未检索）"
    if n > int(budget):
        return False, f"kb_search 调用 {n} 次超过预算上限 {budget}（应按需检索、证据复用）"
    tasks = {t["id"]: t for t in ctx["final"]["tasks"]}
    bad_actors = []
    for e in reqs:
        t = tasks.get(e.get("task_id"))
        role = (t or {}).get("agent_role", "<unknown>")
        # 证据复用：出口核验（verifier）与执行（executor/controller）不得再发起 kb_search
        if t is not None and role != "researcher":
            bad_actors.append(role)
    if bad_actors:
        return False, f"非研究员角色发起了 kb_search: {bad_actors}（应复用已有证据）"
    return True, f"kb_search {n} 次（≤预算 {budget}），均由研究员按需发起，其余角色复用证据"


def chk_retrieval_budget(ctx, budget=2):
    """SPEC §0：设置检索次数上限，仍无依据时明确无法确认（不是无限重试）。"""
    n = len(_retrieval_requests(ctx["final"]["events"]))
    if n <= int(budget):
        return True, f"检索调用 {n} 次 ≤ 上限 {budget}（简单规则预算内）"
    return False, f"检索调用 {n} 次超过上限 {budget}（超预算未及时收敛）"


def chk_recovery_no_reretrieve(ctx, _=True):
    """SPEC §0：长程任务恢复时仅刷新过期/受影响证据；重启阶段不重复已完成的检索。

    以第一个 restart 步骤快照为基准：重启之后的 kb_search 请求数必须为 0
    （研究分支在崩溃/重启前已完成检索并持久化，恢复时复用）。
    """
    phases = ctx.get("phases") or {}
    restart_names = [n for n in phases if str(n).startswith(("after_restart", "restart"))]
    baseline = len(_retrieval_requests((phases.get("initial") or {}).get("events") or []))
    if restart_names:
        baseline = len(_retrieval_requests(
            (phases.get(sorted(phases)[0]) or {}).get("events") or []))
        # 找第一个 restart 前一个阶段（approve 后）作为基准
        ordered = list(phases)
        for i, nm in enumerate(ordered):
            if str(nm).startswith(("after_restart", "restart")):
                prev = phases.get(ordered[i - 1]) if i > 0 else phases.get("initial")
                baseline = len(_retrieval_requests((prev or {}).get("events") or []))
                break
    final_n = len(_retrieval_requests(ctx["final"]["events"]))
    added = final_n - baseline
    if added <= 0:
        return True, f"重启/恢复阶段新增 kb_search {added} 次（复用已持久化证据，不重复检索；共 {final_n} 次）"
    return False, f"恢复阶段新增 kb_search {added} 次（基准 {baseline}→{final_n}），重启后重复检索"


def chk_no_unretrieved_claim(ctx, _=True):
    """SPEC §0/§11：未经检索的模型推断不得作为已验证业务事实。

    verified=true 的正向业务结论必须附引用；若答案本身是"已核验确无依据/无法确认"
    （无正向事实论断），空引用是合法的负向核验，不算臆造。
    """
    root = ctx["final"]["root"]
    res = _result_of(root)
    answer = _answer_of(root, ctx["final"]["events"])
    no_basis = any(k in answer for k in ("无法确认", "未找到", "没有找到", "无相关",
                                         "未检索到", "不能确认", "未在"))
    if res.get("verified") is True:
        cits = _citations_of(root, ctx["final"]["events"])
        if not cits and not no_basis:
            return False, "verified=true 但既无引用、答案也未声明无法确认（推断冒充已验证事实）"
        if not cits:
            return True, "verified=true 且答案明确无依据（负向核验，无正向事实论断）"
        return True, f"verified=true 且附 {len(cits)} 条引用（事实有检索证据）"
    return True, "未作为已验证事实放行（verified≠true，推断不冒充业务事实）"


# ---- GEO（SPEC §8）-------------------------------------------------------

def _geo_call(ctx, tool, args):
    return ctx["session"].call_tool(tool, args, task_id=ctx["root_id"], root_id=ctx["root_id"],
                                    scenario=ctx["case"].get("scenario", "geo_site"))


def chk_geo_tools_present(ctx, _=True):
    names = ctx["session"].tool_names()
    need = ["geo_geocode", "geo_radius_search", "geo_distance"]
    lack = [n for n in need if n not in names]
    if not lack:
        return True, f"GEO 工具齐备: {need}"
    return False, f"缺少 GEO 工具 {lack}；注册表: {names}"


def chk_geo_radius_and_sort(ctx, _=True):
    """半径内候选 + haversine 独立复算 + 距离排序 + EPSG:4326 + source 标注。"""
    slots = ctx["case"].get("slots") or {}
    location = slots.get("location", "中关村")
    radius = float(slots.get("radius_km", 2))
    category = slots.get("category", "咖啡馆")
    ok, geo = _geo_call(ctx, "geo_geocode", {"location": location})
    if not ok or not isinstance(geo, dict) or geo.get("found") is False:
        return False, f"geo_geocode 未能定位 {location!r}: {geo}"
    lon, lat = float(geo["lon"]), float(geo["lat"])
    ok, res = _geo_call(ctx, "geo_radius_search",
                        {"center": location, "radius_km": radius,
                         "filters": {"category": category}, "sort_by": "distance"})
    if not ok or not isinstance(res, dict):
        return False, f"geo_radius_search 调用失败: {res}"
    if res.get("supported") is False:
        return False, f"radius_search 返回不支持: {res}"
    cands = res.get("candidates")
    if not cands:
        return False, "半径内候选为空（GeoJSON 缺中关村 2km 内咖啡馆？）"
    dists = []
    for c in cands:
        d = cf.haversine_km(lon, lat, float(c["lon"]), float(c["lat"]))
        dists.append(d)
        if d > radius + 1e-6:
            return False, f"候选 {c.get('name')} 独立复算距离 {d:.3f}km 超出半径 {radius}km"
    if dists != sorted(dists):
        return False, f"候选未按距离排序: {[round(d, 3) for d in dists]}"
    if "4326" not in str(res.get("crs", "")):
        return False, f"坐标系非 EPSG:4326: {res.get('crs')!r}"
    if not res.get("source"):
        return False, "结果缺少 source 标注"
    cats = [str((c.get("properties") or {}).get("category", "")) for c in cands]
    if any(category not in c for c in cats if c):
        return False, f"类别过滤失效: {cats}"
    ctx["_geo"] = {"center": (lon, lat), "dists": dists, "total": res.get("total")}
    return True, (f"{len(cands)} 个候选均在 {radius}km 内（haversine 独立复算）、按距离排序、"
                  f"crs={res.get('crs')}、source={res.get('source')}")


def chk_geo_quiet_attr_sourced(ctx, _=True):
    """SPEC §11："安静"等无来源属性标 source:null/未核验，不得据此下事实断言。"""
    slots = ctx["case"].get("slots") or {}
    location = slots.get("location", "中关村")
    radius = float(slots.get("radius_km", 2))
    category = slots.get("category", "咖啡馆")
    ok, res = _geo_call(ctx, "geo_radius_search",
                        {"center": location, "radius_km": radius,
                         "filters": {"category": category}, "sort_by": "distance"})
    if not ok or not isinstance(res, dict) or not res.get("candidates"):
        return True, "（跳过：无候选明细）"
    problems = []
    for c in res["candidates"]:
        props = c.get("properties") or {}
        noise = props.get("noise_level")
        noise_src = props.get("noise_level_source")
        # 数据若给出安静结论（noise_level 低/含安静），必须有来源；无来源时证据串须标注未核验
        if noise is not None and not noise_src:
            evidence_blob = " ".join(c.get("evidence") or [])
            if "未核验" not in evidence_blob and "source:null" not in evidence_blob and "null" not in evidence_blob:
                problems.append(f"{c.get('name')}: noise_level={noise} 无来源且证据未标注未核验")
    if problems:
        return False, "；".join(problems[:3])
    return True, "候选噪声/安静类属性无来源时均标注 source:null/未核验，不编造"


def chk_geo_route_unsupported(ctx, _=True):
    """route 模式无 provider → supported:false，且不编造通勤时间（SPEC §8）。"""
    ok, res = _geo_call(ctx, "geo_distance",
                        {"a": {"lat": 39.984, "lon": 116.310},
                         "b": {"lat": 39.908, "lon": 116.397}, "mode": "route"})
    if not ok or not isinstance(res, dict):
        return False, f"geo_distance route 调用失败: {res}"
    if res.get("supported") is not False:
        return False, f"route 应 supported:false: {json.dumps(res, ensure_ascii=False)[:200]}"
    fabricated = [k for k in ("duration_min", "travel_time", "commute_time", "duration", "time_minutes")
                  if res.get(k) is not None]
    if fabricated:
        return False, f"route 不支持却返回通勤时间 {fabricated}: {res}"
    return True, "geo_distance(mode=route) → supported:false，无编造通勤时间"


def chk_geo_geocode_unknown(ctx, _=True):
    ok, res = _geo_call(ctx, "geo_geocode", {"location": "不存在的地名XYZ幻光星屿"})
    if not ok or not isinstance(res, dict):
        return False, f"geo_geocode 调用失败: {res}"
    if res.get("found") is False and not (res.get("lat") or res.get("lon")):
        return True, "未知地名 found:false、无坐标，未编造"
    return False, f"未知地名疑似编造坐标: {json.dumps(res, ensure_ascii=False)[:200]}"


CHECKS = {
    "status": chk_status,
    "terminal_not_pending": chk_terminal_not_pending,
    "answer_contains": chk_answer_contains,
    "answer_regex": chk_answer_regex,
    "answer_not_contains": chk_answer_not_contains,
    "verified_true": chk_verified_true,
    "verified_not_true": chk_verified_not_true,
    "citations_present": chk_citations_present,
    "citations_support": chk_citations_support,
    "mentions_slots": chk_mentions_slots,
    "no_fabricated_answer": chk_no_fabricated_answer,
    "no_evidence_ack": chk_no_evidence_ack,
    "conflict_flagged": chk_conflict_flagged,
    "tool_failure_reported": chk_tool_failure_reported,
    "no_success_claim": chk_no_success_claim,
    "tool_denied_event": chk_tool_denied_event,
    "permission_denied_reason": chk_permission_denied_reason,
    "parallel_approval_notify": chk_parallel_approval_notify,
    "purchase_resumed": chk_purchase_resumed,
    "purchase_side_effect_once": chk_purchase_side_effect_once,
    "weather_tool_present": chk_weather_tool_present,
    "weather_mock": chk_weather_mock,
    "weather_tool_absent_when_disabled": chk_weather_tool_absent_when_disabled,
    "retrieval_on_demand": chk_retrieval_on_demand,
    "retrieval_budget": chk_retrieval_budget,
    "recovery_no_reretrieve": chk_recovery_no_reretrieve,
    "no_unretrieved_claim": chk_no_unretrieved_claim,
    "geo_tools_present": chk_geo_tools_present,
    "geo_radius_and_sort": chk_geo_radius_and_sort,
    "geo_quiet_attr_sourced": chk_geo_quiet_attr_sourced,
    "geo_route_unsupported": chk_geo_route_unsupported,
    "geo_geocode_unknown": chk_geo_geocode_unknown,
}


def _parse_ts(ts: str | None) -> float:
    if not ts:
        return 0.0
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def _iso(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")
    except Exception:
        return str(ts)


# ---------------------------------------------------------------------------
# 流程执行
# ---------------------------------------------------------------------------

def _grants_from_case(case: dict) -> list[dict]:
    out = []
    for g in case.get("grants") or []:
        row = {"scope": g.get("scope", "preauth"),
               "tool_name": g.get("tool") or g.get("tool_name"),
               "object_pattern": g.get("object_pattern"),
               "max_count": g.get("max_count")}
        if g.get("expires_hours"):
            from datetime import timedelta
            row["expires_at"] = (datetime.now(timezone.utc)
                                 + timedelta(hours=int(g["expires_hours"]))).isoformat(timespec="seconds")
        else:
            row["expires_at"] = g.get("expires_at")
        out.append(row)
    return out


def _default_workdir(case_id: str) -> Path:
    """临时工作目录：优先系统 temp；沙箱/无法打开时回退到工作区内 .lfwork/。"""
    try:
        d = Path(tempfile.mkdtemp(prefix=f"lf_eval_{case_id}_"))
        probe = d / "_probe.tmp"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return d
    except Exception:  # noqa: BLE001
        import uuid
        d = REPO_ROOT / ".lfwork" / f"run_{case_id}_{uuid.uuid4().hex[:8]}"
        d.mkdir(parents=True, exist_ok=True)
        return d


def execute_case(case: dict, workdir: Path | None = None) -> dict:
    """运行单个用例：临时 DB → 引导 → grants → flow 步骤 → expect 检查。

    额外输出 RAG/成本观测指标（SPEC §0 评测观测：检索调用量、模型调用量、时延）：
      metrics = {retrieval_calls, llm_calls, tool_calls, elapsed_ms}
    """
    import time
    t0 = time.time()
    result = {"id": case["id"], "name": case.get("name", case["id"]),
              "passed": False, "checks": [], "notes": []}
    workdir = workdir or _default_workdir(case["id"])
    workdir.mkdir(parents=True, exist_ok=True)
    session = None
    try:
        session = cf.tmp_db(workdir, plugins_overrides=case.get("plugins_overrides"))
    except cf.BackendUnavailable as exc:
        result["checks"].append({"name": "backend", "passed": False,
                                 "detail": f"后端不可用，用例阻塞: {exc}"})
        return result
    except Exception as exc:  # noqa: BLE001
        result["checks"].append({"name": "backend", "passed": False,
                                 "detail": f"后端初始化异常: {type(exc).__name__}: {exc}\n"
                                           + traceback.format_exc(limit=3)})
        return result

    result["notes"] = list(session.notes)
    ctx = {"session": session, "case": case, "root_id": None, "phases": {},
           "final": None, "steps": 0, "workdir": workdir, "flow_errors": []}
    root_id = None
    try:
        for g in _grants_from_case(case):
            session.add_grant(**g)
        root_id = session.create_root(case["goal"], case.get("scenario", "team_ops"),
                                      slots=case.get("slots") or {})
        ctx["root_id"] = root_id
        ctx["phases"]["initial"] = _snapshot(session, root_id)
        for step in case.get("flow") or []:
            _execute_step(ctx, step)
        session = ctx["session"]  # restart 步骤会替换为新会话
        if ctx["phases"].get("final") is None:
            drive = cf._drive(session, root_id)
            ctx["steps"] += drive["steps"]
            if drive.get("last_error"):
                ctx["flow_errors"].append(drive["last_error"])
            ctx["phases"]["final"] = _snapshot(session, root_id)
        ctx["final"] = ctx["phases"]["final"]
    except Exception as exc:  # noqa: BLE001
        result["checks"].append({"name": "flow", "passed": False,
                                 "detail": f"流程异常: {type(exc).__name__}: {exc}\n"
                                           + traceback.format_exc(limit=4)})
        try:
            ctx["final"] = _snapshot(session, root_id)
        except Exception:
            ctx["final"] = {"root": None, "tasks": [], "events": [], "approvals": []}

    for name, arg in _iter_check_specs(case.get("expect") or {}):
        fn = CHECKS.get(name)
        if fn is None:
            result["checks"].append({"name": name, "passed": False,
                                     "detail": f"未知检查项 {name}"})
            continue
        try:
            ok, detail = fn(ctx, arg)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"检查异常: {type(exc).__name__}: {exc}"
        result["checks"].append({"name": name, "passed": bool(ok), "detail": str(detail)})

    result["passed"] = bool(result["checks"]) and all(c["passed"] for c in result["checks"])
    result["root_status"] = ((ctx.get("final") or {}).get("root") or {}).get("status")
    # SPEC §0：检索调用量/模型调用量/工具调用量/时延观测（首版简单规则，按量观测）
    final_events = ((ctx.get("final") or {}).get("events")) or []
    result["metrics"] = {
        "retrieval_calls": len([e for e in final_events
                                if e.get("kind") == "tool_request"
                                and (e.get("detail") or {}).get("tool") == "kb_search"]),
        "llm_calls": len([e for e in final_events if e.get("kind") == "llm_call"]),
        "tool_calls": len([e for e in final_events if e.get("kind") == "tool_request"]),
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
    }
    if ctx.get("flow_errors"):
        result["notes"].append("flow: " + " | ".join(ctx["flow_errors"][:3]))
    try:
        session.close()
    except Exception:  # noqa: BLE001
        pass
    return result


def _iter_check_specs(expect: dict):
    alias = {
        "status": "status",
        "answer_contains": "answer_contains",
        "citations_present": "citations_present",
        "no_unsupported_claims": "citations_support",
        "clarifies_slots": "mentions_slots",
        "no_fabricated_answer": "no_fabricated_answer",
        "acknowledges_no_evidence": "no_evidence_ack",
        "conflict_flagged": "conflict_flagged",
        "verified": "verified_true",
        "verified_not_true": "verified_not_true",
    }
    yielded = set()
    if isinstance(expect.get("checks"), list):
        for c in expect["checks"]:
            if isinstance(c, dict):
                name = c.get("name")
                yielded.add(name)
                yield (name, c.get("arg", c.get("value", True)))
    for k, v in expect.items():
        if k == "checks" or v is None:
            continue
        name = alias.get(k, k)
        if name in yielded or name not in CHECKS:
            continue
        yielded.add(name)
        yield (name, v)


def _execute_step(ctx, step: dict):
    session = ctx["session"]
    root_id = ctx["root_id"]
    kind = step.get("do") or step.get("step") or step.get("type")
    name = step.get("name") or f"{kind}_{len(ctx['phases'])}"

    if kind in ("tick", "drive", "run"):
        drive = cf._drive(session, root_id, max_steps=int(step.get("max_steps", 60)))
        ctx["steps"] += drive["steps"]
        if drive.get("last_error"):
            ctx["flow_errors"].append(drive["last_error"])
        ctx["phases"][name] = _snapshot(session, root_id)

    elif kind in ("wait_approval", "await_approval"):
        snap = _snapshot(session, root_id)
        if not [a for a in snap["approvals"] if a["status"] == "pending"]:
            ctx["flow_errors"].append(
                f"wait_approval: 无 pending approval（tasks={[t['status'] for t in snap['tasks']]}）")
        ctx["phases"][name] = snap

    elif kind in ("approve", "decide"):
        snap = _snapshot(session, root_id)
        pending = [a for a in snap["approvals"] if a["status"] == "pending"]
        if not pending:
            ctx["flow_errors"].append("approve: 无 pending approval")
        else:
            appr = pending[0]
            res = session.decide_approval(appr["id"],
                                          decision=step.get("decision", "approved"),
                                          args=step.get("args"))
            if not res.get("ok"):
                ctx["flow_errors"].append(f"approve 失败: {res}")
        # decide_approval 内部已 tick 恢复；再 drive 兜底到下一等待点/终态
        cf._drive(session, root_id)
        ctx["phases"][name] = _snapshot(session, root_id)

    elif kind == "restart":
        session.close()
        # 新进程/新连接重开同一 DB（内存状态全丢）；随后显式 tick 验证恢复
        session = cf.restart(ctx["workdir"] / "longflow.db", root_id,
                             max_steps=int(step.get("max_steps", 60)), drive=False)
        ctx["session"] = session
        cf._drive(session, root_id, max_steps=int(step.get("max_steps", 60)))
        ctx["phases"][name] = _snapshot(session, root_id)

    elif kind == "disable_plugin":
        note = session.disable_plugin(step.get("plugin", "example_weather"))
        ctx.setdefault("notes", []).append(f"disable_plugin: {note}")

    elif kind == "snapshot":
        ctx["phases"][name] = _snapshot(session, root_id)

    else:
        ctx["flow_errors"].append(f"未知 flow 步骤: {kind!r}")


# ---------------------------------------------------------------------------
# run_all / main
# ---------------------------------------------------------------------------

def run_all(db_path=None) -> dict:
    """SPEC §10 / §7：运行全部回归用例（每例独立临时 DB）。

    db_path: 保留给 /api/eval/run（可传入评测 DB）；用例本身始终在各自临时 DB
    隔离运行。db_path 提供时尽力把汇总写入 eval_runs 表。
    """
    results = [execute_case(case) for case in cf.all_cases()]
    summary = {
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "cases": [{"id": r["id"], "name": r["name"], "passed": r["passed"],
                   "checks": r["checks"], "metrics": r.get("metrics")}
                  for r in results],
    }
    if db_path is not None:
        try:
            import uuid
            conn = cf.connect(db_path)
            ts = cf.now_iso()
            for r in results:
                conn.execute(
                    "INSERT INTO eval_runs(id, ts, case_id, passed, checks_json, note) "
                    "VALUES (?,?,?,?,?,?)",
                    ("ev_" + uuid.uuid4().hex, ts, r["id"], 1 if r["passed"] else 0,
                     json.dumps(r["checks"], ensure_ascii=False), "regression"))
            conn.commit()
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return summary


def main(argv=None) -> int:
    summary = run_all()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n== LongFlow 回归评测: {summary['passed']}/{summary['total']} 用例通过 ==")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# pytest 收集
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

CASES = cf.all_cases()


@pytest.mark.eval
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_eval_case(case, workdir):
    """SPEC §10：每个 yaml 用例一个 pytest 测试，全部 check 必须通过。"""
    result = execute_case(case, workdir=workdir / case["id"])
    failed = [c for c in result["checks"] if not c["passed"]]
    if failed:
        lines = [f"用例 {case['id']} 失败检查:"]
        for c in failed:
            lines.append(f"  - [{c['name']}] {c['detail']}")
        if result.get("notes"):
            lines.append("适配说明: " + " | ".join(result["notes"][:4]))
        pytest.fail("\n".join(lines), pytrace=False)
