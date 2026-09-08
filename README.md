# LongFlow

> **长程 AI Agent Harness** —— 一个把"长任务执行"做成可审计、可恢复、可核验工程系统的开源 Agent 框架。

LongFlow 不是一个聊天机器人，而是一套**任务编排外壳（Harness）**：主控 Agent 把长目标拆成持久化任务图，分派研究/执行/核验子 Agent，工具调用全部经过权限与审批闸门，答案必须通过引用核验才能交付。默认使用**规则驱动的本地 LLM**，离线即可跑通真实闭环；也可一键切换到任意 OpenAI 兼容端点。

---

## 核心特性

- **主从 Agent 编排**：主控 Agent（controller）负责任务拆解与调度；researcher / executor / verifier 等子 Agent 独立推进分支，一个分支等待审批不会阻塞其他分支。
- **入口 / 出口双闸门**：入口闸门做意图分类与必填槽位检查（缺信息先澄清，不硬猜）；出口闸门对事实论断做引用核验、工具状态一致性、硬约束满足、脱敏与高风险审批记录检查，不通过则返工。
- **RAG 引用核验**：知识库 BM25 检索 + 字段过滤 + 重排；答案中每个 `[cite:chunk_id]` 必须存在且其原文支撑对应论断，无引用支撑的事实性论断不予通过。
- **工具权限与审批**：low/medium/high 三级风险；low 自动放行，medium 匹配预授权（preauth）否则审批，high 必须逐次审批且批准绑定具体动作与参数；显式 deny 永远拒绝。权限规则绝不写进 prompt。
- **持久化任务图与重启恢复**：任务、依赖、槽位、计划、审批、授权全部落 SQLite（WAL）；进程重启后由后台恢复循环继续调度，等待中的任务到点 / 审批后自动恢复，无模型空转。
- **副作用幂等**：所有副作用工具以 `sha256(tool_name|canonical_json(args)|root_id)` 为幂等键去重，恢复时发现已成功则复用结果，绝不重复执行（不重复下单、不重复发通知）。
- **插件 SDK**：不改核心即可加工具、加知识、挂事件钩子；插件清单声明所需权限，加载失败只禁用自身、核心继续运行。
- **GEO 可选插件**：基于本地 GeoJSON 的真实 haversine 距离计算与半径筛选，前端内联 SVG 地图；没有外部路径规划 provider 时如实返回"不支持"，**不编造通勤时间**。
- **任务工作台**：零构建原生前端（无 CDN、无外部依赖），提交目标、查看任务树与事件日志、处理审批、补充澄清、查看地图结果与"真实/模拟"徽标。
- **回归评测**：`tests/cases/*.yaml` 用例驱动，临时库跑完整闭环后做函数级断言（不用 LLM 打分），工作台"评测"页可一键运行。

---

## 边界与非目标

诚实说明 LongFlow 不做什么：

- **默认是规则驱动的本地 LLM（LocalDriver）**：确定性、离线、可复现，能跑通真实的任务/权限/审批/恢复闭环，但它不是通用智能；可在配置中切换 `openai_compatible` 驱动接入真实模型。
- **不承诺消除幻觉**：LongFlow 用引用核验、证据闸门、冲突标注等工程手段*抑制*无据论断（出口闸门会拦下无引用支撑的事实断言），但不保证任何模型输出绝对正确。
- **不做不可信插件沙箱 / 插件市场**：插件以本机 Python 代码身份运行，**仅支持你自己审计过的可信本地插件**；没有代码隔离沙箱、没有签名机制、没有插件商店。
- **不做完整 GIS**：GEO 插件提供本地地理编码、直线距离、半径筛选；路径规划/通勤时间依赖外部 provider，缺失时明确返回不支持，不伪造。

---

## 快速开始

环境要求：**Python 3.11+**（开发实测 3.14）。

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2.（可选）初始化数据库；serve 启动时也会自动幂等建库
python -m longflow.cli init-db

# 3. 启动服务
python -m longflow.cli serve --port 8765
```

浏览器打开 <http://127.0.0.1:8765> 即可使用任务工作台。

运行回归评测（任选其一）：

```bash
python -m pytest tests/ -q
```

或在工作台"评测"页点击运行，逐条查看通过/失败与断言证据。

其他命令：`python -m longflow.cli reset` 清空运行库（谨慎）。

---

## 示例场景

### 场景一：团队行政知识问答与多步采购审批（`team_ops`）

- **问规则**：例如"公司采购笔记本电脑的审批规则是什么？"——researcher 检索行政知识库，verifier 核验引用，出口闸门确认每条论断都有条款支撑后，返回带 `[cite]` 的答案。
- **多步采购**：例如"帮我采购 3 台笔记本，预算 2 万"——入口闸门检查物品/预算槽位（缺失则先澄清），生成 `researcher → verifier → executor` 子任务链；executor 调用高风险工具 `make_purchase` 时**必须逐次审批**，任务转入 `waiting_approval`，工作台出现审批卡片；批准时系统校验参数与请求时完全一致才放行。
- 分支独立：若另一条分支只需发通知（medium，命中 preauth 或单独审批），它不会被采购分支的等待阻塞。

### 场景二：GEO 选址分析（`geo_site`）

- 例如"在望京 5 公里内找低噪音、适合办公的候选地点"——researcher 调 `geo_geocode` 把地名转坐标（未知地名返回 `null`，不编造），`geo_radius_search` 用**真实 haversine 公式**做半径过滤与排序；结果带 `source` 字段与 `crs: EPSG:4326`。
- 前端 SVG 地图绘制候选点与半径圆，并根据 `source` 显示"真实/mock"徽标。
- `geo_distance` 的 route（通勤）模式在无外部 provider 时返回 `{supported: false}`，系统如实告知"无法提供通勤时间"，绝不杜撰。

---

## 配置

| 配置 | 位置 | 说明 |
| --- | --- | --- |
| 主配置 | `config/longflow.yaml` | LLM 驱动（`local` / `openai_compatible`）、模型路由、插件启用开关与插件配置、预算（`max_steps` / `max_llm_calls` / `max_verify_rounds`）、恢复 tick 间隔等。 |
| 场景配置 | `config/scenarios/team_ops.yaml`、`config/scenarios/geo_site.yaml` | 知识源、意图关键词、槽位定义、权限策略（preauth）、子任务模板（local driver 据此拆任务，真实模型可覆盖）。 |
| 环境变量 | `.env`（从 `.env.example` 复制） | `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` 用于 OpenAI 兼容驱动；`LONGFLOW_DB` 指定数据库路径。**密钥只从环境变量读取，绝不落盘、不写入日志。** |

`.env` 示例：

```ini
LLM_API_KEY=sk-...                       # openai_compatible 驱动必填；local 驱动留空
LLM_BASE_URL=https://api.openai.com/v1   # 任意 OpenAI 兼容端点（可指向自建网关）
LLM_MODEL=gpt-4o-mini
LONGFLOW_DB=data/longflow.db
```

### Map Plugin System（工作台地理分析）

工作台**不绑定任何具体地图 Provider**。地理分析只负责搜索、筛选、Haversine 直线距离与结论；
地图展示、Marker、半径 Circle、道路路线计算/绘制、外部导航跳转全部交给当前启用的 **Map Plugin**。
启用哪个插件，地图/路线/绘制/「在地图中打开」就全部走哪个，不会出现混合状态。

- 默认内置 **高德地图插件（AMap）**，位于 `longflow/web/maps/amap.js`；
  **Google Maps 插件**（`longflow/web/maps/google.js`）已预留统一接口，当前 Coming Soon。
- 选择优先级：**用户手动选择（插件页） > 服务端 `map_plugins.active` > 默认可用插件**。
- 切换插件时会 `destroy()` 旧实例（地图/路线/监听/DOM），避免两个地图并存。
- 无可用插件或地图加载失败时自动回退内联 SVG 离线示意图，Marker 与 Haversine 直线距离仍可见，工作台不崩溃。

**统一插件接口**（轻量、无继承体系；路线绘制由插件自己完成，MapPanel 不接触任何 Provider API）：

```
mount(container) / showSites(geoResult, onSelect) / destroy()
planRoute({ origin, destination, mode }) -> Promise<RouteResult>   # 插件内部算路并绘制
exitRoute()
openExternalSite(site) -> URL      openExternalRoute({origin,destination,mode}) -> URL
getDisplayName() -> "高德地图"      getExternalOpenLabel() -> "在高德地图打开"
capabilities = { map, markers, circle, route, routeModes, externalSite, externalRoute }
```

统一路线模式：`driving | walking | transit | cycling`（高德内部映射 `transit→AMap.Transfer`、
`cycling→AMap.Riding`；未来 Google 可映射 `cycling→bicycling`）。`RouteResult` 统一为
`{ provider, mode, ok, distanceMeters, durationSeconds, summary, steps, raw }`，MapPanel 只消费这些字段。

**第三方接入**：调用 `registerMapPlugin(definition)` 即可，无需改 MapPanel / Agent / Geo Result：

```js
import { registerMapPlugin } from "./maps/index.js";
registerMapPlugin({
  id: "my-map", name: "My Map", version: "1.0.0", available: true,
  capabilities: { map: true, markers: true, circle: true, route: true,
    routeModes: ["driving", "walking"], externalSite: true, externalRoute: true },
  create(config) {
    return {
      id: "my-map", available: true,
      async mount(container) { /* 初始化你的地图 */ },
      showSites(geo, onSelect) { /* 画中心点/候选/Circle */ },
      async planRoute({ origin, destination, mode }) {
        // 调你的路线 API 并在你的地图上绘制，返回统一 RouteResult
        return { provider: "my-map", mode, ok: true,
          distanceMeters: 1200, durationSeconds: 900, summary: "1.2 km · 15 min", steps: [] };
      },
      exitRoute() {}, destroy() {},
      openExternalSite(s) { return "https://example.com/..." },
      openExternalRoute(o) { return "https://example.com/route" },
      getDisplayName() { return "My Map"; },
      getExternalOpenLabel() { return "在 My Map 打开"; },
    };
  },
});
```

### AMap Security Configuration（高德安全配置）

Demo 使用高德 JS API 2.0 的 **securityJsCode 客户端方式**：`AMAP_SECURITY_CODE` 经 `/api/config` 下发，
在加载 `https://webapi.amap.com/maps...` 的 `<script>` **之前**设置
`window._AMapSecurityConfig = { securityJsCode }`（顺序由 `loadAMap()` 保证，并有测试守护）。

```ini
AMAP_JS_KEY=你的高德Web端Key        # 浏览器加载 JS SDK 必需（公开值，务必在高德后台限制 Referer）
AMAP_SECURITY_CODE=你的安全密钥      # securityJsCode；Demo 客户端方式，下发浏览器但不入日志/仓库
AMAP_WEB_KEY=你的Web服务Key         # 仅服务器使用，绝不下发浏览器
AMAP_CITY=北京                      # 公交(transit)默认城市
```

- `AMAP_JS_KEY` 是前端加载 SDK 所需的公开 Web Key，不要误当服务器密钥；`AMAP_WEB_KEY` 是
  Web Service Key，**保持服务器端**，public config 中刻意不含它。
- 不主动 `console.log` 密钥、不写死代码、不进仓库；`.env` 已在 `.gitignore`。
- **生产部署建议**改用 `serviceHost` 代理模式（`window._AMapSecurityConfig = { serviceHost: "/_AMapService" }`），
  本版不实现该代理后端，但 `loadAMap()` 已支持传入 `serviceHost`，未来可平滑切换。

---

## 目录结构

```
longflow/
  __init__.py
  config.py        # 配置加载：config/longflow.yaml + .env 环境变量
  db.py            # sqlite3 连接、建表、迁移（幂等）
  models.py        # dataclass / TypedDict 行模型 + 枚举常量
  events.py        # append_event() 审计事件写入；动作日志读取
  rag.py           # 知识库加载、chunk、BM25 关键词 + 字段过滤 + 简单重排
  redaction.py     # 敏感信息检测/脱敏（key/token/手机号/身份证/邮箱）
  permissions.py   # 授权矩阵、preauth 匹配、决策函数
  tools.py         # 工具注册表（核心内置工具）、工具运行时（权限检查+执行+日志）
  llm.py           # LLM 网关：LocalDriver（默认，规则驱动）/ OpenAICompatibleDriver
  orchestrator.py  # 主控 Agent：入口闸门、任务图、调度循环、出口闸门
  workers.py       # 恢复循环：后台线程 tick 可运行/到期任务（用于重启恢复）
  api.py           # FastAPI app：REST + 静态文件
  cli.py           # python -m longflow.cli serve / init-db / reset
  plugins/
    __init__.py
    sdk.py         # Plugin 基类、ToolContext、PluginManifest 数据类
    loader.py      # 发现/校验/加载 plugins 目录与场景插件
  scenarios/       # 场景包（每个一个子目录，由 loader 发现）
plugins/           # 用户本地插件（目录）：geo/、example_weather/
config/
  longflow.yaml
  scenarios/team_ops.yaml
  scenarios/geo_site.yaml
knowledge/         # 知识源（场景引用）
data/              # 运行时：longflow.db（.gitignore）
tests/             # pytest
web/               # 前端
docs/              # 架构、插件、验证报告文档
```

---

## 文档

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) —— 五层架构、主从 Agent、双闸门、任务图/恢复、权限模型、RAG 引用核验、LLM 网关（含 ASCII 架构图与请求闭环序列）。
- [`docs/PLUGINS.md`](docs/PLUGINS.md) —— 插件开发指南，以 `plugins/example_weather` 为完整示例。
- [`docs/VERIFICATION.md`](docs/VERIFICATION.md) —— 首版验证报告模板与检查清单（结果待集成后由实际运行填入）。

---

## 已实现 / 模拟环境 / 尚未验证

### 已实现（SPEC 第一版能力）

- SQLite（WAL）幂等建表与迁移：tasks / approvals / grants / knowledge_sources / knowledge_chunks / events / eval_runs。
- 任务状态机与依赖生效：dep 全部 completed 才 ready；任一 dep failed/cancelled 级联 failed；建图校验依赖存在、同 root、无循环（DFS），违例抛 `TaskGraphError`。
- 主控 Agent 编排：入口闸门（意图分类、槽位 regex、clarify/handoff 信号）、计划生成、ready 任务调度、预算检查（max_steps / max_llm_calls）。
- 出口闸门：引用核验、工具状态一致性、硬约束满足、来源冲突标注、脱敏检查、high 风险审批记录检查；不通过最多返工 `max_verify_rounds` 轮。
- 工具运行时唯一调用路径：`permissions.decide` → deny/needs_approval/allow；审批任务转 `waiting_approval` 且**不执行**；副作用工具幂等键去重、带超时执行、全程事件留痕。
- 内置核心工具：`kb_search`、`http_get`、`record_note`、`send_notification`（medium）、`make_purchase`（high）。
- 权限模型：low 自动放行；medium 匹配未过期、次数未满的 preauth（tool + object glob）；high 必须 `once` 审批且绑定 canonical(args)；显式 deny 永远拒绝；审批决定时校验参数一致性。
- RAG：知识加载与切 chunk、BM25（中英文 token）、字段过滤、粗排 + citation/字段加权重排。
- 敏感信息脱敏：key/token/手机号/身份证/邮箱检测，日志与响应兜底。
- LLM 网关：`driver.respond(messages, tools, json_schema)` 统一接口；LocalDriver 规则驱动产出 plan / next_action / draft_answer，且所有判断同时产出可核验信号。
- 恢复循环：后台线程 tick（默认 2s）处理到期 `waiting_event` 与审批后恢复；等待时不调用 LLM；暂停/取消级联并汇总已发生副作用清单。
- REST API：health / config / tasks CRUD / cancel / message / approvals decide / knowledge 调试 / eval 运行 / plugins 列表；错误码 `permission_denied|approval_required|clarification_needed|dependency_failed|budget_exceeded|not_found|plugin_error`。
- 插件系统：清单扫描与校验、SDK 基类（setup/get_tools/get_knowledge/on_event）、`plugins.<name>.enabled` 开关、加载失败隔离（`plugin_error` 事件 + 禁用，核心继续）。
- GEO 插件：本地地理编码、haversine 距离、半径筛选排序、EPSG:4326 与 source 标注、route 无 provider 时 `{supported:false}`。
- 示例插件 `example_weather`：`weather_get(city)`，本地模拟数据并明确标注 mock。
- 零构建前端工作台：任务提交/任务树/事件日志/审批卡片/澄清对话/SVG 地图与真实-mock 徽标/评测页。
- 回归评测：`tests/cases/*.yaml` + `tests/run_eval.py`（临时库、完整闭环、函数级断言），覆盖引用、澄清、无结果、冲突、工具失败、越权阻止、审批并行与恢复、重启幂等、插件加载、GEO 十类用例。

### 模拟环境（可离线跑通真实闭环）

- **本地规则 LLM（LocalDriver）**：默认驱动，按场景 `intent_keywords` / 槽位正则 / `subtask_templates` 确定性产出结构化动作——跑通的是真实的编排、权限、审批、恢复、核验机制，不是脚本录制。
- **天气数据**：`example_weather` 的 `weather_get` 返回本地模拟数据，返回值明确标注 mock。
- **本地 GeoJSON 地理数据**：无外部地理 provider 时，GEO 插件使用 `plugins/geo/data/sample.geojson`；距离/筛选为真实计算，要素属性中无来源者标 `source: null`。

### 尚未验证（待集成后实测）

- **真实 OpenAI 兼容端点**：`OpenAICompatibleDriver` 的端到端行为（模型路由、工具调用 JSON、超时/重试、密钥脱敏）需配置真实 `LLM_BASE_URL` / `LLM_API_KEY` 后实测。
- **外部 Geo Provider**：route 通勤模式与外部地理编码 provider 的对接（当前无 provider 时按契约返回不支持）。

---

## 开源许可证

LongFlow 采用 **[MIT 许可证](LICENSE)**。

选择 MIT 的理由：

- **宽松**：允许自由使用、修改、分发与私有化部署，几乎没有使用门槛；
- **鼓励扩展与商用**：用户可基于 LongFlow 构建自己的场景、插件甚至商业产品，只需保留版权与许可声明；
- **要求低**：无 copyleft 传染性，不强制开源衍生作品，适合作为框架/底座被集成进各类项目；
- **生态友好**：与 FastAPI、SQLite、httpx 等依赖的宽松许可证传统一致。

MIT 不提供任何担保；参见 [`docs/VERIFICATION.md`](docs/VERIFICATION.md) 了解首版实际验证范围。

---

*LongFlow Contributors · 2025*
