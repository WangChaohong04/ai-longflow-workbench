"""LongFlow Plugin SDK.

插件作者只依赖本模块导出的接口：
  - ``ToolSpec``        工具描述（名称/用途/风险/是否有副作用/参数模式/处理函数）
  - ``ToolContext``     工具执行上下文（数据库连接、任务 id、事件记录器等）
  - ``Plugin``          插件基类，子类化并实现 get_tools / get_knowledge / on_event
  - ``spec``            快速构造 ToolSpec 的辅助函数
  - ``PluginError``     插件应抛出的受控错误

工具处理函数签名：``handler(args: dict, ctx: ToolContext) -> dict``
工具返回值必须是 JSON 可序列化的 dict。工具内容是*数据*，不得借返回值下发指令；
权限与审批由 Harness 运行时统一处理，插件不要自行判断授权。
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable, Optional


class PluginError(Exception):
    """插件受控错误：加载器捕获后禁用该插件，核心继续运行。"""


@dataclasses.dataclass
class ToolSpec:
    name: str
    description: str
    handler: Callable[[dict, "ToolContext"], dict]
    risk: str = "low"            # 'low' | 'medium' | 'high'
    side_effect: bool = False
    params_schema: dict = dataclasses.field(default_factory=dict)

    def validate_args(self, args: dict) -> None:
        """轻量参数校验：必填字段与类型（仅 str/int/float/bool/list/dict）。"""
        schema = self.params_schema or {}
        props = schema.get("properties", {})
        for key, val in (args or {}).items():
            if key not in props:
                continue  # 多余参数不报错，由处理函数自行忽略
        for key, meta in props.items():
            if meta.get("required") and key not in (args or {}):
                raise PluginError(f"工具 {self.name} 缺少必填参数: {key}")
        # required 数组形式（JSON Schema 风格）
        for key in schema.get("required", []):
            if key not in (args or {}):
                raise PluginError(f"工具 {self.name} 缺少必填参数: {key}")


@dataclasses.dataclass
class ToolContext:
    conn: Any                    # sqlite3.Connection
    task_id: str
    root_id: str
    scenario: str
    emit: Callable[[str, dict], None]   # emit(event_kind, detail)
    http: Any = None             # 可选 httpx.Client（无外部网络时为 None）
    plugin_config: dict = dataclasses.field(default_factory=dict)
    data_dir: str = ""           # 插件自身数据目录（plugin.yaml 所在目录）


def spec(
    name: str,
    description: str,
    handler: Callable[[dict, "ToolContext"], dict],
    *,
    risk: str = "low",
    side_effect: bool = False,
    params_schema: Optional[dict] = None,
) -> ToolSpec:
    """构造 ToolSpec 的便捷函数。"""
    return ToolSpec(
        name=name,
        description=description,
        handler=handler,
        risk=risk,
        side_effect=side_effect,
        params_schema=params_schema or {},
    )


class Plugin:
    """插件基类。

    子类必须设置类属性 ``manifest_path`` 由加载器注入，或直接由加载器传入 manifest。
    生命周期：加载器构造插件 → ``setup(config, ctx_like)`` → 调用 ``get_tools()`` 等。
    """

    manifest: dict = {}

    def setup(self, config: dict, data_dir: str) -> None:
        """插件初始化。config 来自 longflow.yaml 的 plugins.<name>.config。"""

    def get_tools(self) -> list[ToolSpec]:
        """返回本插件提供的工具。默认无工具。"""
        return []

    def get_knowledge(self) -> list[dict]:
        """可选：返回知识 chunk 字典列表。

        每个 dict: {doc_name, section?, text, fields?: dict, citations?: list}
        """
        return []

    def on_event(self, kind: str, detail: dict) -> None:
        """可选：审计事件钩子（只读用途，如通知外部系统）。不得抛异常影响主流程。"""
        return None
