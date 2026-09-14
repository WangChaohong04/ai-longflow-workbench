"""行模型与枚举常量。"""
from __future__ import annotations

import dataclasses
import json
from typing import Any

# 任务状态
PENDING = "pending"
READY = "ready"
IN_PROGRESS = "in_progress"
WAITING_APPROVAL = "waiting_approval"
WAITING_EVENT = "waiting_event"      # 通用等待（向后兼容旧数据）
WAITING_USER = "waiting_user"        # 等待用户补充信息/澄清/确认
WAITING_EXTERNAL = "waiting_external"  # 等待外部系统（人工渠道/第三方回调）
RETRYING = "retrying"                # 可恢复失败后的重试中
PARTIALLY_COMPLETED = "partially_completed"  # 部分分支成功、部分阻塞
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_STATUSES = {COMPLETED, FAILED, CANCELLED}
# 所有挂起等待状态：调度器不空转推进，等待对应事件唤醒
WAITING_STATUSES = {WAITING_APPROVAL, WAITING_EVENT, WAITING_USER, WAITING_EXTERNAL}
# 非终态、可被恢复循环/用户输入唤醒
RESUMABLE_STATUSES = WAITING_STATUSES | {RETRYING, PARTIALLY_COMPLETED, READY, PENDING}

# 角色
ROLE_CONTROLLER = "controller"
ROLE_RESEARCHER = "researcher"
ROLE_EXECUTOR = "executor"
ROLE_VERIFIER = "verifier"

ROLE_LABELS = {
    "controller": "主控",
    "researcher": "检索分析",
    "executor": "执行",
    "verifier": "核验",
}

STATUS_LABELS = {
    PENDING: "待处理",
    READY: "就绪",
    IN_PROGRESS: "进行中",
    WAITING_APPROVAL: "待审批",
    WAITING_EVENT: "等待中",
    WAITING_USER: "待用户处理",
    WAITING_EXTERNAL: "等待外部",
    RETRYING: "重试中",
    PARTIALLY_COMPLETED: "部分完成",
    COMPLETED: "已完成",
    FAILED: "失败",
    CANCELLED: "已取消",
}


@dataclasses.dataclass
class Task:
    id: str
    parent_id: str | None
    root_id: str
    title: str
    kind: str
    agent_role: str
    status: str
    objective: str
    slots: dict
    plan: dict
    result: dict
    depends_on: list[str]
    idempotency_key: str | None
    scheduled_at: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> "Task":
        return cls(
            id=row["id"],
            parent_id=row["parent_id"],
            root_id=row["root_id"],
            title=row["title"],
            kind=row["kind"],
            agent_role=row["agent_role"],
            status=row["status"],
            objective=row["objective"],
            slots=json.loads(row["slots_json"] or "{}"),
            plan=json.loads(row["plan_json"] or "{}"),
            result=json.loads(row["result_json"] or "{}"),
            depends_on=json.loads(row["depends_on_json"] or "[]"),
            idempotency_key=row["idempotency_key"],
            scheduled_at=row["scheduled_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "root_id": self.root_id,
            "title": self.title,
            "kind": self.kind,
            "agent_role": self.agent_role,
            "agent_role_label": ROLE_LABELS.get(self.agent_role, self.agent_role),
            "status": self.status,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "objective": self.objective,
            "slots": self.slots,
            "plan": self.plan,
            "result": self.result,
            "depends_on": self.depends_on,
            "scheduled_at": self.scheduled_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
