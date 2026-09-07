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

from . import db, events, permissions, rag
from .plugins.sdk import ToolContext, ToolSpec  # re-export for convenience

# 已知失败供应商（演示权威系统不可达场景；真实系统由插件/配置提供）
UNREACHABLE_VENDORS = {"unreachable-vendor"}
APPROVED_VENDORS = {"approved-vendor", "standard-it"}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._meta: dict[str, dict] = {}

    def register(self, spec: ToolSpec, *, data_dir: str = "", config: dict | None = None) -> None:
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
    chunks = rag.search(
        ctx.conn,
        args["query"],
        scenario=ctx.scenario or None,
        fields=args.get("fields"),
        top_k=int(args.get("top_k", 5)),
    )
    return {"chunks": chunks, "count": len(chunks)}


def _tool_record_note(args: dict, ctx: ToolContext) -> dict:
    return {"recorded": True, "text": args.get("text", "")[:2000]}


def _tool_http_get(args: dict, ctx: ToolContext) -> dict:
    url = args["url"]
    if ctx.http is None:
        # 无外部网络配置时如实返回，不编造内容
        return {"ok": False, "error": "http_client_unavailable", "url": url}
    try:
        resp = ctx.http.get(url, timeout=15)
        return {"ok": True, "status": resp.status_code, "text": resp.text[:4000]}
    except Exception as exc:  # noqa: BLE001 - 工具失败必须如实上抛/返回
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


def _tool_make_purchase(args: dict, ctx: ToolContext) -> dict:
    """下单（high 风险，有外部副作用）。

    供应商不在名录或不可达时如实失败——工具失败绝不描述为成功。
    """
    vendor = args.get("vendor") or "approved-vendor"  # 未指定时走公司标准供应商
    if vendor in UNREACHABLE_VENDORS:
        raise RuntimeError(f"供应商系统不可达: {vendor}")
    # 名录用于规则问答；执行层仅拦截显式标记为不可达的供应商，其余由人工审批把关。
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
            self.conn, tool_name, risk, args, task_id=task.id
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
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(spec.handler, args, ctx)
                try:
                    result = future.result(timeout=self.timeout_seconds)
                except concurrent.futures.TimeoutError:
                    raise RuntimeError(f"工具执行超时（{self.timeout_seconds}s）")
            ok = True
            err = None
            if isinstance(result, dict) and result.get("ok") is False:
                ok = False
                err = result.get("error", "tool_returned_failure")
        except Exception as exc:  # noqa: BLE001 - 失败如实记录
            result, ok, err = {"error": str(exc)[:500]}, False, str(exc)[:500]

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
                "idempotency_key": idem_key,
                "result": result if ok else None,
            },
        )
        if not ok:
            raise RuntimeError(f"工具 {tool_name} 执行失败: {err}")
        return result
