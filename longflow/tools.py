"""工具注册表与统一工具运行时。

所有工具调用（核心与插件）唯一入口是 ToolRuntime.call：
权限检查 → 审批闸门 → 幂等去重 → 超时执行 → 审计日志。
工具实现与权限规则不写进 Agent Prompt。
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import sqlite3
from typing import Callable

from . import db, events, permissions, rag, netguard
from .plugins.sdk import ToolContext, ToolSpec  # re-export for convenience

# 已知失败供应商（演示权威系统不可达场景；真实系统由插件/配置提供）
UNREACHABLE_VENDORS = {"unreachable-vendor"}


class ToolResultInDoubt(RuntimeError):
    """副作用工具可能已执行但结果未确认（如本地不可中断工具超时）。

    对应任务进入"待人工确认"，绝不自动重试，避免重复下单/重复外发。
    """

    def __init__(self, tool_name: str, reason: str):
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"工具 {tool_name} 结果待确认：{reason}")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._meta: dict[str, dict] = {}

    def register(self, spec: ToolSpec, *, data_dir: str = "", config: dict | None = None,
                 on_conflict: str = "error") -> None:
        """注册工具。重名默认报错：不静默覆盖核心工具，也不让插件顶替并自降 risk。

        on_conflict="override" 时显式允许覆盖（仅核心注册/测试使用）。
        """
        if spec.name in self._tools and on_conflict != "override":
            existing = self._tools[spec.name]
            same = (getattr(existing, "description", "") == spec.description
                    and getattr(existing, "risk", "") == spec.risk)
            if not same:
                raise ValueError(
                    f"工具名冲突：{spec.name!r} 已注册（{getattr(existing, 'description', '')[:30]}），"
                    f"拒绝静默覆盖；请改用不同工具名或显式 on_conflict='override'")
        self._tools[spec.name] = spec
        self._meta[spec.name] = {"data_dir": data_dir, "config": config or {}}

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def meta(self, name: str) -> dict:
        return self._meta.get(name, {})

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> dict[str, ToolSpec]:
        return dict(self._tools)


# ---------- 核心内置工具 ----------

def _tool_kb_search(args: dict, ctx: ToolContext) -> dict:
    # 按需分段检索：首轮不足时在预算内改写/扩召回一次；逐阶段记录命中与耗时。
    staged = rag.search_staged(
        ctx.conn,
        args["query"],
        scenario=ctx.scenario or None,
        fields=args.get("fields"),
        top_k=int(args.get("top_k", 5)),
        max_rounds=int(args.get("max_rounds", 2)),
    )
    try:
        ctx.emit("retrieval", {
            "tool": "kb_search", "query": args["query"],
            "stages": staged["stages"], "rounds": len(staged["stages"]),
            "total_hits": staged["count"],
        })
    except Exception:  # noqa: BLE001 - 观测事件失败不影响检索
        pass
    return {"chunks": staged["chunks"], "count": staged["count"],
            "retrieval_stages": staged["stages"]}


def _tool_record_note(args: dict, ctx: ToolContext) -> dict:
    return {"recorded": True, "text": args.get("text", "")[:2000]}


def _tool_http_get(args: dict, ctx: ToolContext) -> dict:
    url = args["url"]
    # 域名白名单：优先取工具参数 allowed_domains，其次插件/领域配置
    allowlist = args.get("allowed_domains") or ctx.plugin_config.get("allowed_domains")
    try:
        netguard.check_url(url, allowlist=allowlist)
    except netguard.UrlNotAllowed as exc:
        # SSRF/非白名单/危险协议：拒绝，不发起请求（失败如实记录，绝不抓取）
        return {"ok": False, "error": f"url_blocked: {exc}", "url": url, "blocked": True}
    if ctx.http is None:
        return {"ok": False, "error": "http_client_unavailable", "url": url}
    try:
        # follow_redirects=False：重定向目标必须逐跳重新过 SSRF/白名单校验
        resp = ctx.http.get(url, timeout=15, follow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location", "")
            nxt = loc if str(loc).startswith("http") else f"{resp.url.scheme}://{resp.url.host}{loc}"
            try:
                netguard.check_url(nxt, allowlist=allowlist)
            except netguard.UrlNotAllowed as exc:
                return {"ok": False, "error": f"redirect_blocked: {exc}", "url": url,
                        "redirect": nxt, "blocked": True}
            return {"ok": False, "error": "redirect_requires_recheck", "redirect": nxt,
                    "url": url, "blocked": True}
        return {"ok": True, "status": resp.status_code, "text": resp.text[:4000], "url": str(resp.url)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300], "url": url}


def _tool_send_notification(args: dict, ctx: ToolContext) -> dict:
    """有副作用的外发动作（medium 风险）。"""
    return {
        "sent": True,
        "channel": args.get("channel"),
        "to": args.get("to"),
        "text": args.get("text", "")[:500],
        "delivered_at": db.now(),
    }


def _gather_root_chunks(ctx: ToolContext) -> list[dict]:
    """汇总同 root 任务研究阶段检索到的知识 chunks（供执行前约束核验）。"""
    rows = ctx.conn.execute(
        """SELECT result_json FROM tasks WHERE root_id=?""", (ctx.root_id,)
    ).fetchall()
    chunks = []
    for r in rows:
        try:
            res = json.loads(r["result_json"] or "{}")
        except Exception:  # noqa: BLE001
            continue
        for c in res.get("chunks", []) or []:
            if isinstance(c, dict) and c.get("text"):
                chunks.append(c)
    return chunks


def _tool_make_purchase(args: dict, ctx: ToolContext) -> dict:
    """下单（high 风险，有外部副作用）。

    执行前基于研究阶段证据核验硬约束（金额审批线/供应商名录）：
    - 证据明确标明供应商不在名录且违规 -> 拒绝下单（不借"人工审批"放行违规）。
    - 金额达到审批线 -> 提示需审批（high 风险本身已强制逐次审批）。
    - 供应商系统不可达 -> 如实失败。工具失败绝不描述为成功。
    """
    from . import verification as _v
    vendor = args.get("vendor") or "approved-vendor"  # 未指定时走公司标准供应商
    if vendor in UNREACHABLE_VENDORS:
        raise RuntimeError(f"供应商系统不可达: {vendor}")
    # 基于已检索证据核验供应商/金额硬约束
    chunks = _gather_root_chunks(ctx)
    if chunks:
        chk = _v.check_purchase_constraints(args, chunks)
        bad_vendor = [p for p in chk["problems"] if p.get("kind") == "vendor_not_listed"]
        if bad_vendor:
            raise RuntimeError("采购约束核验失败：" + "；".join(p["problem"] for p in bad_vendor))
    amount = float(args.get("amount", 0))
    return {
        "ordered": True,
        "order_id": f"PO-{hashlib.sha1((vendor + str(amount) + db.now()).encode()).hexdigest()[:10].upper()}",
        "vendor": vendor,
        "item": args.get("item"),
        "amount": amount,
        "currency": args.get("currency", "CNY"),
    }


def register_core_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "kb_search",
            "检索已授权知识库（带来源与条款引用）",
            _tool_kb_search,
            risk="low",
            params_schema={
                "properties": {"query": {"type": "string", "required": True}},
            },
        )
    )
    registry.register(
        ToolSpec(
            "record_note",
            "记录工作笔记到任务档案",
            _tool_record_note,
            risk="low",
            params_schema={"properties": {"text": {"type": "string", "required": True}}},
        )
    )
    registry.register(
        ToolSpec(
            "http_get",
            "只读 HTTP 获取（演示）",
            _tool_http_get,
            risk="low",
            params_schema={"properties": {"url": {"type": "string", "required": True}}},
        )
    )
    registry.register(
        ToolSpec(
            "send_notification",
            "向渠道/人员发送通知（外发副作用）",
            _tool_send_notification,
            risk="medium",
            side_effect=True,
            params_schema={
                "properties": {
                    "channel": {"type": "string", "required": True},
                    "to": {"type": "string"},
                    "text": {"type": "string", "required": True},
                }
            },
        )
    )
    registry.register(
        ToolSpec(
            "make_purchase",
            "向供应商下单（资金副作用）",
            _tool_make_purchase,
            risk="high",
            side_effect=True,
            params_schema={
                "properties": {
                    "vendor": {"type": "string", "description": "供应商；缺省走标准供应商 approved-vendor"},
                    "item": {"type": "string", "required": True},
                    "amount": {"type": "number", "required": True},
                    "currency": {"type": "string"},
                }
            },
        )
    )


# ---------- 运行时 ----------

class ToolRuntime:
    def __init__(
        self,
        conn: sqlite3.Connection,
        registry: ToolRegistry,
        *,
        timeout_seconds: int = 30,
        http=None,
    ) -> None:
        self.conn = conn
        self.registry = registry
        self.timeout_seconds = timeout_seconds
        self.http = http

    def _context(self, task, plugin_config: dict | None = None, data_dir: str = "") -> ToolContext:
        return ToolContext(
            conn=self.conn,
            task_id=task.id,
            root_id=task.root_id,
            scenario=getattr(task, "scenario", None) or task.slots.get("__scenario__", ""),
            emit=lambda kind, detail: events.emit(
                self.conn, kind, task_id=task.id, actor=f"tool:{task.agent_role}", detail=detail
            ),
            http=self.http,
            plugin_config=plugin_config or {},
            data_dir=data_dir,
        )

    def idempotency_key(self, root_id: str, tool_name: str, args: dict) -> str:
        payload = permissions.canonical_args(tool_name, args) + "|" + root_id
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def call(
        self,
        tool_name: str,
        args: dict,
        task,
        *,
        reason: str = "",
        plugin_data_dir: str = "",
        plugin_config: dict | None = None,
    ) -> dict:
        spec = self.registry.get(tool_name)
        risk = spec.risk if spec is not None else "high"  # 未知工具按最高风险对待

        # 权限检查独立于工具注册：先记请求、再做授权判定（deny 永远先生效），
        # 不依赖工具是否注册成功。
        events.emit(
            self.conn,
            events.TOOL_REQUEST,
            task_id=task.id,
            actor=f"agent:{task.agent_role}",
            detail={"tool": tool_name, "tool_name": tool_name, "args": args, "risk": risk, "reason": reason},
        )
        decision = permissions.decide(
            self.conn, tool_name, risk, args, task_id=task.id,
            root_id=getattr(task, "root_id", task.id),
        )
        if decision.verdict == permissions.DENY:
            events.emit(
                self.conn,
                events.TOOL_DENIED,
                task_id=task.id,
                actor="governance",
                detail={"tool": tool_name, "tool_name": tool_name, "args": args, "reason": decision.reason},
            )
            raise permissions.PermissionDenied(decision.reason)

        if spec is None:
            raise FileNotFoundError(f"工具不存在: {tool_name}")
        spec.validate_args(args)

        idem_key = None
        if spec.side_effect:
            idem_key = self.idempotency_key(task.root_id, tool_name, args)
            prior = events.find_side_effect_result(self.conn, idem_key)
            if prior is not None:
                events.emit(
                    self.conn,
                    events.TOOL_RESULT,
                    task_id=task.id,
                    actor="runtime",
                    detail={
                        "tool": tool_name,
                        "tool_name": tool_name,
                        "ok": True,
                        "idempotent_replay": True,
                        "idempotency_key": idem_key,
                        "result": prior,
                    },
                )
                return prior

        if decision.verdict == permissions.NEEDS_APPROVAL:
            approval_id = db.new_id("ap")
            self.conn.execute(
                """INSERT INTO approvals (id, task_id, tool_name, args_json, args_hash,
                   reason, status, created_at) VALUES (?,?,?,?,?,?, 'pending', ?)""",
                (
                    approval_id,
                    task.id,
                    tool_name,
                    json.dumps(args, ensure_ascii=False),
                    permissions.args_hash(tool_name, args),
                    decision.reason + (f"；{reason}" if reason else ""),
                    db.now(),
                ),
            )
            self.conn.commit()
            events.emit(
                self.conn,
                events.APPROVAL_REQUESTED,
                task_id=task.id,
                actor="governance",
                detail={
                    "approval_id": approval_id,
                    "tool": tool_name, "tool_name": tool_name,
                    "args": args,
                    "risk": spec.risk,
                    "reason": decision.reason,
                },
            )
            raise permissions.ApprovalRequired(approval_id, tool_name, args, decision.reason)

        # 允许执行（插件工具的 data_dir/config 由注册表元数据提供）
        meta = self.registry.meta(tool_name)
        plugin_data_dir = plugin_data_dir or meta.get("data_dir", "")
        plugin_config = plugin_config if plugin_config is not None else meta.get("config", {})
        ctx = self._context(task, plugin_config, plugin_data_dir)
        result: dict
        timed_out = False
        # 独立 executor：超时后不 shutdown(wait=True) 阻塞主流程；daemon 线程自行结束，
        # 不拖垮整个 tick（本地不可中断工具如实标注为"可能已执行，待确认"）。
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(spec.handler, args, ctx)
            try:
                result = future.result(timeout=self.timeout_seconds)
            except concurrent.futures.TimeoutError:
                timed_out = True
                result = {"error": f"工具执行超时（{self.timeout_seconds}s）"}
            ok = not timed_out
            err = None
            if not timed_out and isinstance(result, dict) and result.get("ok") is False:
                ok = False
                err = result.get("error", "tool_returned_failure")
            if timed_out:
                err = result["error"]
                if spec.side_effect:
                    events.emit(
                        self.conn,
                        events.TOOL_RESULT,
                        task_id=task.id,
                        actor=f"tool:{tool_name}",
                        detail={
                            "tool": tool_name, "tool_name": tool_name,
                            "ok": False, "error": err, "in_doubt": True,
                            "idempotency_key": idem_key, "result": None,
                        },
                    )
                    raise ToolResultInDoubt(tool_name, err)
        except ToolResultInDoubt:
            raise  # 副作用待确认：向上传播，不被下面的通用失败处理吞掉
        except Exception as exc:  # noqa: BLE001 - 失败如实记录
            result, ok, err = {"error": str(exc)[:500]}, False, str(exc)[:500]
        finally:
            # wait=False：不等待可能卡死的本地工具线程；daemon 线程随进程退出回收。
            pool.shutdown(wait=False)

        if decision.grant_id:
            permissions.increment_grant(self.conn, decision.grant_id)

        events.emit(
            self.conn,
            events.TOOL_RESULT,
            task_id=task.id,
            actor=f"tool:{tool_name}",
            detail={
                "tool": tool_name,
                "tool_name": tool_name,
                "ok": ok,
                "error": err,
                "args": args,
                "blocked": bool(isinstance(result, dict) and result.get("blocked")),
                "idempotency_key": idem_key,
                "result": result if ok else None,
            },
        )
        if not ok:
            raise RuntimeError(f"工具 {tool_name} 执行失败: {err}")
        return result
