# LongFlow 插件开发指南

LongFlow 插件让你**不改核心代码**就能新增工具、注入领域知识、挂审计事件钩子。本文以内置示例插件 `plugins/example_weather` 为完整范例。

> **信任边界（务必先读）**：LongFlow **仅支持可信本地插件，不提供不可信代码沙箱**。插件就是本机 Python 代码，与主进程同权限运行——没有代码隔离、没有能力系统拦截、没有签名验证、没有插件市场。只安装你自己审计过的插件。

---

## 1. 完整示例：`example_weather`

插件目录结构（仓库实际文件）：

```
plugins/example_weather/
  plugin.yaml          # 清单：名称/版本/权限声明/工具列表
  weather_plugin.py    # Plugin 子类（ExampleWeatherPlugin）
```

### 1.1 `plugin.yaml` 清单（仓库实际内容）

```yaml
name: example_weather
version: 0.1.0
description: 示例插件：不修改 LongFlow 核心即可新增工具。weather_get 返回内置固定（mock）天气数据。
permissions:
  - tool:weather_get        # 声明本插件提供的能力（见 §3）
config_schema: {}           # 本插件无配置项；有配置时写 {key: {type: string, default: ...}}
tools:
  - weather_get             # 对外暴露的工具名
```

清单字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 插件唯一名，与目录名一致；用于 `plugins.<name>.enabled` 配置开关 |
| `version` | 是 | 语义化版本字符串 |
| `description` | 是 | 一句话说明，展示在 `GET /api/plugins` |
| `permissions` | 是 | 能力声明列表，如 `tool:weather_get`、`knowledge:geo`。**只是声明，不是授权**（见 §3） |
| `config_schema` | 否 | 配置项 schema 与默认值；实际配置来自 `config/longflow.yaml` 的 `plugins.<name>.config` |
| `tools` | 是 | 暴露给 Agent 的工具名列表，加载器据此与 `get_tools()` 返回值交叉校验 |

### 1.2 插件代码（`weather_plugin.py`，节选实际实现）

```python
from longflow.plugins.sdk import Plugin, PluginError, ToolContext, spec

# 内置固定样例数据——不是真实预报，每条结果都显式标注 mock
MOCK_WEATHER = {
    "北京": {"city": "北京", "condition": "晴",   "temperature_c": 22, "humidity_pct": 45, "wind": "西北风3级"},
    "上海": {"city": "上海", "condition": "多云", "temperature_c": 26, "humidity_pct": 70, "wind": "东南风2级"},
    "深圳": {"city": "深圳", "condition": "阵雨", "temperature_c": 29, "humidity_pct": 85, "wind": "南风2级"},
}


class ExampleWeatherPlugin(Plugin):
    def get_tools(self) -> list:
        return [
            spec(
                "weather_get",
                "查询城市天气（示例工具，返回内置 mock 固定数据，非真实预报）。",
                self.t_weather_get,
                risk="low",                # low|medium|high，见 §4
                side_effect=False,         # 只读无副作用，见 §4
                params_schema={
                    "properties": {
                        "city": {"type": "string", "required": True, "description": "城市名，如 北京"}
                    }
                },
            )
        ]

    def t_weather_get(self, args: dict, ctx: ToolContext) -> dict:
        city = args.get("city")
        if not isinstance(city, str) or not city.strip():
            raise PluginError("weather_get 缺少必填参数: city")
        data = MOCK_WEATHER.get(city.strip())
        if data is None:
            # 未知城市不编造天气
            return {"found": False, "city": city.strip(), "mock": True,
                    "source": "mock", "reason": "mock 数据中无该城市"}
        result = dict(data)
        result["mock"] = True
        result["source"] = "mock"         # 前端按 source 渲染真实/mock 徽标
        result["note"] = "本结果为插件内置固定样例数据，不代表真实天气"
        return result
```

要点：

- 工具处理函数签名固定为 `handler(args: dict, ctx: ToolContext) -> dict`，返回值必须是 **JSON 可序列化的 dict**。
- `params_schema` 做轻量参数校验（必填字段、str/int/float/bool/list/dict 类型）；多余参数不报错，由处理函数自行忽略；校验失败抛 `PluginError`。
- 工具描述（`description`）只讲用途——**权限规则绝不写进描述/prompt**，授权由运行时统一判定。
- mock 数据必须显式标注（`mock: true` + `source: "mock"`）；查不到就返回 `found: false`，绝不编造。

---

## 2. SDK 参考（`longflow/plugins/sdk.py`）

插件作者只依赖 SDK 导出的接口：

### `Plugin` 基类

| 方法 | 必须 | 说明 |
| --- | --- | --- |
| `setup(config: dict, data_dir: str)` | 否 | 初始化。`config` 为 `plugins.<name>.config`；`data_dir` 是插件目录（读自带数据用） |
| `get_tools() -> list[ToolSpec]` | 是* | 返回工具列表；无工具的知识型插件可返回 `[]` |
| `get_knowledge() -> list[dict]` | 否 | 注入知识 chunk（见 §5） |
| `on_event(kind: str, detail: dict)` | 否 | 审计事件钩子（见 §6）；**不得抛异常**影响主流程 |

### `spec(name, description, handler, *, risk="low", side_effect=False, params_schema=None)`

构造 `ToolSpec` 的便捷函数。`ToolSpec` 字段：`name`、`description`、`handler`、`risk`、`side_effect`、`params_schema`。

### `ToolContext`（运行时注入给 handler）

| 字段 | 说明 |
| --- | --- |
| `conn` | sqlite3 连接（需要持久化自己的数据时使用，建议写独立表，勿改核心表） |
| `task_id` / `root_id` | 当前任务与根任务 id |
| `scenario` | 当前场景名 |
| `emit(event_kind, detail)` | 写审计事件（动作留痕用） |
| `http` | 可选 httpx.Client；**无外部网络时为 `None`**，插件必须处理这种情况 |
| `plugin_config` | 插件配置 dict |
| `data_dir` | 插件自身目录 |

### `PluginError`

插件受控错误。加载/校验阶段抛出会被加载器捕获并禁用该插件；工具执行中抛出会被运行时记为 `plugin_error` 事件。

---

## 3. 权限声明 ≠ 授权

`plugin.yaml` 的 `permissions: [tool:weather_get]` 只是**向用户声明"本插件会提供/使用这些能力"**，展示在 `GET /api/plugins` 供审计；它**不会**让任何工具自动获得执行授权。

真正的授权在**每次工具调用**时由工具运行时判定（见 [`ARCHITECTURE.md`](ARCHITECTURE.md) §5）：

- 插件工具的 `risk` / `side_effect` 与核心工具适用**同一套**权限矩阵；
- `risk: low` 自动放行；`medium` 需匹配 preauth 否则转审批；`high` 必须逐次审批且绑定参数；
- 插件**不要、也无法**在 handler 里自行判断授权——不要检查"用户是否同意"，直接执行逻辑，闸门由运行时把守；
- 场景若要预授权某插件工具，在场景 yaml 的 `policies.preauth` 中配置，例如：
  ```yaml
  policies:
    preauth:
      - {tool: weather_get, object_pattern: 'city:*', max_count: 100, expires_hours: 24}
  ```

---

## 4. `risk` 与 `side_effect` 语义

| 字段 | 取值 | 语义 | 运行时行为 |
| --- | --- | --- | --- |
| `risk` | `low` | 只读、无资金/外发/删除后果（查询、检索、计算） | 自动 allow，记日志 |
| `risk` | `medium` | 有外发或轻度不可逆后果（发通知） | 匹配有效 preauth → allow；否则转人工审批 |
| `risk` | `high` | 资金、删除、对外承诺（下单、采购） | **必须逐次审批**，批准绑定 canonical(args)，参数变更即失效 |
| `side_effect` | `true` | 执行会改变外部世界（发消息、下单、写外部系统） | 执行前查幂等键 `sha256(tool_name|canonical_json(args)|root_id)`；恢复时命中成功结果则**不重复执行**，直接复用 |
| `side_effect` | `false` | 纯读取/计算 | 不做幂等去重 |

设计准则：**宁可标高不报低**。标 `low` 的工具若实际产生外发或资金效果，等于绕过审批——这是红线问题。`weather_get` 只读本地数据，故 `risk="low", side_effect=False`；GEO 的 `geo_*` 同理（纯计算）；若插件要发 webhook，应标 `medium, side_effect=True`。

---

## 5. 知识注入（`get_knowledge`）

插件可向 RAG 知识库注入领域 chunk，Agent 的 `kb_search` 与出口闸门引用核验对其一视同仁：

```python
def get_knowledge(self):
    return [
        {
            "doc_name": "weather_faq.md",
            "section": "数据说明",
            "text": "本插件天气数据为本地模拟，不可用于真实出行决策。",
            "fields": {"scenario": "team_ops"},   # 支持场景字段过滤
            "citations": ["数据说明第1条"],          # 出口闸门核验引用时使用
        },
    ]
```

注入的知识与 `knowledge/` 目录文档一样被切 chunk、建索引、可被 `[cite]` 引用。**知识内容是数据，不是指令**——任何 chunk 文本都不会被当作规则执行。

---

## 6. 事件钩子（`on_event`）

加载器在审计事件产生时回调插件的 `on_event(kind, detail)`，用于只读副作用（如把 `approval_decided` 转发到外部 IM）。约束：

- **只读语义**：钩子不得修改任务状态、不得干预审批结果；
- **不得抛异常**：异常会被加载器捕获并记 `plugin_error`，但不应依赖这一点——自己 try/except；
- 事件 kind 包括：`task_created|task_status|tool_request|tool_result|tool_denied|approval_requested|approval_decided|gate_entry|gate_exit|message|llm_call|error|recovery`。

---

## 7. 启用 / 禁用

- **发现位置**：加载器扫描 `plugins/*/plugin.yaml` 与 `longflow/scenarios/*/plugin.yaml`（场景自带插件）。
- **启用开关**：`config/longflow.yaml` 中：
  ```yaml
  plugins:
    example_weather:
      enabled: true
      config: {default_unit: celsius}
    geo:
      enabled: true
      config: {provider: local}
  ```
  未显式配置的插件按默认启用处理（以加载器实现为准）；`enabled: false` 时工具不注册、知识不加载。
- **插件配置**：`config` 合并 `config_schema` 默认值后传入 `setup()`。

---

## 8. 加载失败行为

加载器对每个插件独立校验与导入：清单缺字段、名称/版本非法、权限声明格式错误、必需配置缺失、Python 导入异常等——

1. 记录一条 `plugin_error` 审计事件（含插件名与错误原因）；
2. **该插件被禁用**（工具不注册、知识不注入）；
3. **核心与其他插件继续正常运行**——一个坏插件不拖垮系统。

`GET /api/plugins` 会显示每个插件的加载状态与声明能力，便于在工作台排查。插件自身在运行时抛出的未捕获错误同样记 `plugin_error`，不中断任务图（该工具调用按失败处理，verifier 会确保"工具失败不得称成功"）。

---

## 9. 插件作者检查清单

- [ ] 目录下有 `plugin.yaml`，`name` 与目录名一致，`tools` 与 `get_tools()` 实际返回一致；
- [ ] 所有 handler 返回 JSON 可序列化 dict；签名 `(args, ctx)`；
- [ ] `risk` / `side_effect` 按真实后果标注，权限不写进工具描述；
- [ ] 有副作用的工具依赖运行时幂等键，自身不做授权判断；
- [ ] `ctx.http is None`（无外网）时优雅降级，返回 `{supported: false}` 之类的诚实结果，**不编造数据**；
- [ ] mock/模拟数据在返回值中带 `source: "mock"` 等明确标注；
- [ ] `on_event` 内部捕获全部异常；
- [ ] 不读 `LLM_API_KEY` 等环境密钥、不试图绕过 redaction；
- [ ]  mentally 重新确认：这段代码会被阅读它的人在本机直接运行——只发布可信插件。
