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
        """执行前参数校验：必填、类型、枚举、数值范围、嵌套结构。

        支持的 meta（properties.<key>）：
          required: bool、type: str|int|float|bool|list|dict、
          enum: list、minimum/maximum: number、items/properties: 嵌套 schema。
        多余参数不报错（由处理函数忽略）；校验失败抛 PluginError，动作不执行。
        """
        schema = self.params_schema or {}
        props = schema.get("properties", {})
        args = args or {}

        def _check(key, meta, value, path):
            tname = meta.get("type")
            if tname:
                py = {"string": str, "str": str, "integer": int, "int": int,
                      "number": (int, float), "float": (int, float),
                      "boolean": bool, "bool": bool,
                      "array": list, "list": list, "object": dict, "dict": dict}.get(tname)
                # bool 是 int 子类：number/integer 显式拒绝 bool
                bool_for_number = isinstance(value, bool) and tname in (
                    "number", "integer", "int", "float")
                if bool_for_number or (py is not None and not isinstance(value, py)):
                    raise PluginError(
                        f"工具 {self.name} 参数 {path} 类型错误：期望 {tname}，实际 {type(value).__name__}")
            if "enum" in meta and value not in meta["enum"]:
                raise PluginError(f"工具 {self.name} 参数 {path} 取值非法：{value!r} 不在 {meta['enum']}")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if meta.get("minimum") is not None and value < meta["minimum"]:
                    raise PluginError(f"工具 {self.name} 参数 {path} 小于最小值 {meta['minimum']}")
                if meta.get("maximum") is not None and value > meta["maximum"]:
                    raise PluginError(f"工具 {self.name} 参数 {path} 大于最大值 {meta['maximum']}")
            # 嵌套结构
            if isinstance(value, dict) and isinstance(meta.get("properties"), dict):
                for ck, cm in meta["properties"].items():
                    if cm.get("required") and ck not in value:
                        raise PluginError(f"工具 {self.name} 参数 {path}.{ck} 为必填嵌套字段")
                    if ck in value:
                        _check(ck, cm, value[ck], f"{path}.{ck}")
            if isinstance(value, list) and isinstance(meta.get("items"), dict):
                for i, item in enumerate(value):
                    _check(key, meta["items"], item, f"{path}[{i}]")

        for key, meta in props.items():
            if meta.get("required") and key not in args:
                raise PluginError(f"工具 {self.name} 缺少必填参数: {key}")
        for key in schema.get("required", []):
            if key not in args:
                raise PluginError(f"工具 {self.name} 缺少必填参数: {key}")
        for key, meta in props.items():
            if key in args:
                _check(key, meta, args[key], key)


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
