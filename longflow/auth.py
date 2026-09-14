"""最小可用身份认证与工作区隔离（可内测，不引入外部依赖）。

身份来源（按优先级）：
1. `Authorization: Bearer <token>`：token 在配置/环境中映射到用户与角色；
2. 本地开发：未配置任何 token 时，回退为单一本地用户（local/admin），
   并在 /api/health 标注 auth_mode=local_single_user，便于内测期浏览器直接使用。

关键原则：
- user / workspace / role 一律由**服务端凭据**决定，绝不信任请求体里的
  owner / workspace / by / role 字段；
- 任务、审批、授权、知识文档、反馈都按当前 user + workspace 过滤；
- 管理员专属端点（如 /api/grants）要求 role=admin。

token 映射可来自 config.auth.tokens：
  auth:
    tokens:
      "<token>": {user: alice, role: admin, workspaces: [ws-a, ws-b], default_workspace: ws-a}
也可用环境 LONGFLOW_AUTH_TOKEN（单管理员 token）快速启用。
"""
from __future__ import annotations

import dataclasses
import os
from typing import Any

from fastapi import Header


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 401):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


@dataclasses.dataclass
class Principal:
    user: str
    role: str                 # admin | member
    workspace: str
    workspaces: list[str]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def can_access_workspace(self, ws: str) -> bool:
        if self.is_admin:
            return True
        return ws in self.workspaces or self.workspace == ws


class Authenticator:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        auth_cfg = cfg.get("auth", {}) or {}
        self.tokens: dict[str, dict] = dict(auth_cfg.get("tokens", {}) or {})
        env_token = os.environ.get("LONGFLOW_AUTH_TOKEN")
        if env_token:
            self.tokens.setdefault(env_token, {
                "user": os.environ.get("LONGFLOW_AUTH_USER", "admin"),
                "role": "admin",
                "workspaces": ["*"],
                "default_workspace": "default",
            })
        self.local_dev = not self.tokens

    def authenticate(self, authorization: str | None,
                     x_workspace: str | None = None) -> Principal:
        if self.local_dev:
            # 本地开发/单机内测：单一本地管理员（明确暴露在 health）。
            ws = x_workspace or "default"
            return Principal(user="local", role="admin", workspace=ws,
                             workspaces=["*"])
        token = _bearer(authorization)
        if not token:
            raise AuthError("unauthorized", "缺少认证凭据（Authorization: Bearer <token>）")
        entry = self.tokens.get(token)
        if not entry:
            raise AuthError("invalid_token", "认证凭据无效", status=403)
        allowed = entry.get("workspaces", ["*"]) or ["*"]
        default_ws = entry.get("default_workspace") or (allowed[0] if allowed != ["*"] else "default")
        ws = x_workspace or default_ws
        if "*" not in allowed and ws not in allowed:
            raise AuthError("workspace_forbidden", f"无权访问工作区: {ws}", status=403)
        return Principal(
            user=entry.get("user", "unknown"),
            role=entry.get("role", "member"),
            workspace=ws,
            workspaces=allowed,
        )

    def require_admin(self, principal: Principal) -> None:
        if not principal.is_admin:
            raise AuthError("admin_required", "该操作需要管理员权限", status=403)


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    parts = header.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return header.strip() or None


def principal_dependency(authenticator: Authenticator):
    """生成 FastAPI 依赖：解析当前用户与工作区。"""
    def _dep(authorization: str | None = Header(default=None),
             x_workspace: str | None = Header(default=None, alias="X-Workspace")) -> Principal:
        return authenticator.authenticate(authorization, x_workspace)
    return _dep
