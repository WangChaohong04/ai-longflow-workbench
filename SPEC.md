# LongFlow 内部契约（SPEC）— 各模块必须严格遵守

本文件是各并行实现方（人类与 subagent）之间的唯一契约。不得擅自更改签名、表结构、
HTTP 路径、清单字段。发现契约缺陷时停止并通知队长，不得自行分叉。

## 0. 技术栈与目录

- 后端：Python 3.11+ / FastAPI / SQLite（stdlib `sqlite3`，WAL）/ PyYAML / httpx。
- 前端：零构建。`longflow/web/index.html` + `app.js` + `style.css`，原生 ES Module，
  由 FastAPI `StaticFiles` 挂载在 `/`。地图用内联 SVG 手绘，**禁止任何 CDN/外部依赖**。
- Python 包根：`longflow/`（含 `__init__.py`）。

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
```

## 1. 数据库表（db.py 幂等建表）

```sql
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,              -- t_<ulid 风格 hex>
  parent_id TEXT,                   -- 子任务指向父；根任务 parent_id IS NULL
  root_id TEXT NOT NULL,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,               -- 'goal' | 'subtask'
  agent_role TEXT NOT NULL,         -- 'controller'|'researcher'|'executor'|'verifier'|plugin 角色
  status TEXT NOT NULL DEFAULT 'pending',
      -- pending|ready|in_progress|waiting_approval|waiting_event|completed|failed|cancelled
  objective TEXT NOT NULL,
  slots_json TEXT NOT NULL DEFAULT '{}',   -- 槽位（结构化记忆），字段由场景决定
  plan_json TEXT NOT NULL DEFAULT '{}',    -- 主控计划：子任务图/动作意图（可审计）
  result_json TEXT NOT NULL DEFAULT '{}',  -- 交付结果
  depends_on_json TEXT NOT NULL DEFAULT '[]', -- 依赖任务 id 列表
  idempotency_key TEXT,             -- 副作用动作幂等键
  scheduled_at TEXT,                -- ISO8601；waiting_event 定时唤醒
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, tool_name TEXT NOT NULL,
  args_json TEXT NOT NULL,         -- 批准绑定的具体动作与参数
  reason TEXT, status TEXT NOT NULL DEFAULT 'pending', -- pending|approved|rejected
  decided_by TEXT, decided_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (           -- 持久化授权记录
  id TEXT PRIMARY KEY, scope TEXT NOT NULL,   -- 'auto'|'preauth'|'once'|'deny'
  tool_name TEXT, action TEXT,                -- tool/action 可为空表示通配（按 glob）
  object_pattern TEXT,                        -- 动作对象 glob，如 'purchase:*'
  max_count INTEGER, used_count INTEGER DEFAULT 0,
  expires_at TEXT, granted_by TEXT, task_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_sources (
  id TEXT PRIMARY KEY, name TEXT, scenario TEXT, version TEXT,
  source_path TEXT, access_scope TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id TEXT PRIMARY KEY, source_id TEXT, doc_name TEXT, section TEXT,
  text TEXT, fields_json TEXT DEFAULT '{}',   -- 业务字段（场景/插件提供，不写死核心）
  citations_json TEXT DEFAULT '[]'            -- 支持的条款/位置，如 ["规则第三条"]
);
CREATE TABLE IF NOT EXISTS events (          -- 审计/动作日志（只增）
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, task_id TEXT, kind TEXT NOT NULL,
      -- task_created|task_status|tool_request|tool_result|tool_denied|approval_requested|
      -- approval_decided|gate_entry|gate_exit|message|llm_call|error|recovery
  actor TEXT, detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS eval_runs (
  id TEXT PRIMARY KEY, ts TEXT, case_id TEXT, passed INTEGER,
  checks_json TEXT, note TEXT
);
```

关键不变式：
- 任务状态流转只允许：pending→ready→in_progress→(waiting_approval|waiting_event)→in_progress→
  completed/failed/cancelled。cancelled/failed 为终态。
- 依赖在后端实际生效：任务 ready 条件 = 所有 dep 为 completed；任一 dep 为 failed/cancelled
  → 本任务置 failed 并记录 `detail.reason='dependency_failed'`。
- 创建任务图时必须校验：依赖 id 存在、属于同一 root、无循环（DFS）。违例抛 `TaskGraphError`。
- 副作用工具（`side_effect=true`）执行前：必须有 approved approval 或匹配 preauth；
  执行以 `idempotency_key = sha256(tool_name|canonical_json(args)|root_id)` 去重，
  结果存 `events(tool_result)`；恢复时发现 key 已成功 → 不重复执行，直接复用结果。

## 2. 工具运行时（tools.py + plugins/sdk.py）

- 工具清单对象：`ToolSpec(name, description, risk, side_effect, params_schema, handler)`
  - `risk`: 'low' | 'medium' | 'high'。high = 资金/删除/外发等。
- `ToolContext`：`{conn, task_id, root_id, scenario, emit(event_kind, detail), http}`。
- 注册：核心在 `tools.py:register_core_tools(registry)`；插件在 `manifest` 中提供
  `get_tools(ctx) -> list[ToolSpec-like]`（SDK 基类方法）。
- 调用路径唯一：`runtime.call(tool_name, args, ctx)`：
  1. `permissions.decide(...)` → allow / deny / needs_approval；
  2. deny → 记 `tool_denied` 事件，抛 `PermissionDenied`；
  3. needs_approval → 建 approval（pending）+ 任务转 waiting_approval + 事件，
     抛 `ApprovalRequired(approval_id)`（**不执行**）；
  4. allow → 副作用工具查幂等键 → 执行（带超时）→ 记 `tool_result` → 返回。
- 内置核心工具：
  - `kb_search(query, fields?, top_k?)`（low, 无副作用）：RAG 检索，返回 chunks+citations。
  - `http_get(url)`（low）：演示用只读获取（测试中可被插件/场景替换）。
  - `record_note(text)`（low）：工作记录。
  - `send_notification(channel, to, text)`（medium, side_effect）：需 preauth/审批。
  - `make_purchase(vendor, item, amount, currency)`（high, side_effect）：必须逐次审批。
- 工具的权限规则**绝不**写进 prompt；prompt 只描述工具用途。

## 3. 权限（permissions.py）

输入：conn、tool_name、args、task_id、scenario。查 grants 表 + 场景配置 `policies`：
- risk=low → auto allow（记日志）。
- risk=medium → 匹配未过期、次数未满的 preauth（tool_name + object_pattern glob）→ allow；
  否则 needs_approval。
- risk=high → 必须 `once` 审批且 approval 绑定完全相同的 tool_name+canonical(args)；
  否则 needs_approval。
- 显式 deny grant 永远拒绝。
批准绑定具体动作与参数：approval.approve 时校验 args 与请求时一致（canonical JSON 比对）。

## 4. RAG（rag.py）

- 启动/加载场景时把 `knowledge/*.md|txt|json` 按标题/段落切 chunk，写 knowledge_chunks。
- 检索：BM25（stdlib 实现，中英文按正则 `[\w一-鿿]+` 切 token）+ 场景 `fields` 过滤
  （相等匹配 fields_json）→ 粗排 top 2k → 重排：citation/字段命中加权 → top_k。
- 返回：`[{chunk_id, doc_name, section, text, score, citations, fields}]`。
- **出口闸门引用核验**：答案中每个 `[cite:chunk_id]` 必须存在且其 text 包含答案关键论断
  锚点（简单包含/词重叠核验，verifier 实现）；无引用支撑的事实性论断 → 闸门不通过。

## 5. LLM 网关（llm.py）

配置 `llm.driver`：`local`（默认）或 `openai_compatible`。
- `OpenAICompatibleDriver`：httpx 调 `{base_url}/chat/completions`，模型 `model`，
  Key 来自 env `LLM_API_KEY`（绝不落盘/入日志，redaction 兜底）。
- `LocalDriver`：**确定性规则驱动**，产出与真实模型同构的结构化动作，使闭环离线可跑：
  - `plan(goal, scenario_config, slots) -> Plan`：解析目标文本中的关键词（场景配置
    `intent_keywords` / 槽位正则）生成子任务模板（场景 `subtask_templates`）。
  - `next_action(task, evidence) -> Action`：按任务 agent_role 的状态机产出
    `{type: 'tool_call'|'answer'|'clarify'|'request_approval'|'delegate_done', ...}`。
  - `draft_answer(task, chunks) -> {text with [cite:id], ...}`。
  - 所有"判断"必须同时产出可核验信号（见闸门），禁止只返回自报置信度。
- 接口：`driver.respond(messages, *, tools=None, json_schema=None) -> dict`，
  local driver 用 messages 中的 `system.context`（结构化 JSON）驱动规则。

## 6. 编排（orchestrator.py）

主控 Agent 循环（每个 root 一个 `run_root(conn, root_id)`，可重入、可恢复）：
1. **入口闸门** `gate_entry(goal)`：
   - 意图分类（场景 intent_keywords + 必填槽位 regex）；
   - 信号：必填槽位缺失 → `clarify`（列缺失字段）；命中风险词且无授权渠道 →
     说明并 `handoff`（无人工渠道时如实说明，不伪造）；情绪词仅作辅助日志；
   - 通过 → 生成/更新计划（plan_json），创建子任务图（校验依赖/循环）。
2. **调度**：反复扫描 ready 任务 → in_progress；不同子任务独立推进（一个
   waiting_approval 不阻塞其他分支）；每步检查预算（max_steps / max_llm_calls）。
3. 子任务执行：researcher→kb_search/geo 检索；executor→工具调用（过权限运行时）；
   verifier→收集证据并做出口闸门预检。
4. **出口闸门** `gate_exit(root_result, evidence)`：规则+信号核验：
   - 事实论断均有 [cite] 且引用核验通过；工具状态与结论一致（失败不得称成功）；
   - 用户约束（slots 中的硬约束）满足；来源冲突 → 标注冲突不算通过；
   - 敏感信息已脱敏；high 风险动作均有审批记录。
   - 通过 → status=completed, result.verified=true；不通过 → 补充检索/修正/标 unknown/
   - 或生成 handoff，最多重试 `max_verify_rounds` 轮。
5. 等待：waiting_approval/waiting_event 持久化；workers.py 定时 tick（默认 2s）处理
   scheduled_at 到期与审批后恢复；**无模型空转**（等待时不调用 LLM）。
6. 暂停/取消：API 设置 status=cancelled；子任务级联标记；结果中说明已发生副作用
   （从 events 汇总 tool_result side_effect 清单）。

## 7. REST API（api.py，前缀 `/api`）

- `GET  /api/health`
- `GET  /api/config` → 场景、插件、模型路由（不含密钥）
- `POST /api/tasks`  body `{goal, scenario, slots?}` → 建 root 任务并立即 tick 一次
- `GET  /api/tasks` → 任务列表（含根任务）
- `GET  /api/tasks/{id}` → 详情：任务、子任务树、slots、result、审批、事件日志
- `POST /api/tasks/{id}/cancel`
- `POST /api/tasks/{id}/message` body `{text}` → 用户补充信息/澄清回答（写槽位）
- `POST /api/approvals/{id}/decide` body `{decision:'approved'|'rejected', args?}`
- `GET  /api/knowledge?q=&scenario=&fields=` → 调试用检索
- `POST /api/eval/run` → 运行回归评测（tests/cases 驱动），返回逐条结果
- `GET  /api/plugins` → 已加载插件/能力/权限声明/启用状态
- 静态：`/` → web/index.html。

所有时间 ISO8601 UTC；所有 id 字符串；错误返回 `{error, code}`，code 取
`permission_denied|approval_required|clarification_needed|dependency_failed|
budget_exceeded|not_found|plugin_error`。

## 8. 插件（plugins/sdk.py + loader.py）

清单（插件目录下 `plugin.yaml`）：
```yaml
name: geo
version: 0.1.0
description: GEO 空间分析
permissions: [tool:geo_geocode, tool:geo_radius_search, knowledge:geo]
config_schema: {provider: {type: string, default: local}}
tools: [geo_geocode, geo_distance, geo_radius_search]
```
SDK 基类：
```python
class Plugin:
    manifest: dict
    def setup(self, cfg: dict, ctx) -> None: ...
    def get_tools(self) -> list: ...          # 返回 ToolSpec 列表
    def get_knowledge(self) -> list[dict]: ...  # 可选：知识 chunks
    def on_event(self, kind: str, detail: dict): ...  # 可选
```
加载器：扫描 `plugins/*/plugin.yaml` + `longflow/scenarios/*/plugin.yaml`；
校验名称/版本/权限声明/必需配置；导入失败 → 记录 `plugin_error`，该插件禁用但
**核心继续运行**；启用/禁用：配置 `plugins.<name>.enabled`。插件声明权限不自动授权。
示例插件 `example_weather`：提供 `weather_get(city)` 工具（low，本地模拟数据并明确
标注 mock），证明"不改核心加工具"。

GEO 插件 `geo`：
- 数据 `plugins/geo/data/sample.geojson`（点要素：名称/类别/属性，坐标系 EPSG:4326，
  字段含 `noise_level` 等；无来源的属性标 `source: null`）。
- 工具：`geo_geocode(location)`（本地地名→坐标；未知返回 null 不编造）；
  `geo_distance(a, b, mode)` mode=haverside 直线（真实计算）；route 模式无外部
  provider 时返回 `{supported:false}`，**不得编造通勤时间**；
  `geo_radius_search(center, radius_km, filters?, sort_by?)` 真实 haversine 过滤+排序。
- 输出保留 `crs:'EPSG:4326'` 与 `source`（'local_geojson' | provider 名）。
- 前端地图：`/api/tasks/{id}` 的 geo 结果含 `geo:{candidates:[{name,lon,lat,evidence}],
  center?}`，前端渲染 SVG 点与半径圆；真实/mock 徽标来自 `source` 字段。

## 9. 场景配置（config/scenarios/*.yaml）

```yaml
name: team_ops
description: 团队行政知识问答与多步骤任务
knowledge: [knowledge/team_ops/*.md]
intent_keywords: {采购: [采购, 购买, 下单], 报销: [报销, 费用], 规则: [规则, 政策, 假期]}
slots:                          # 槽位定义（字段由场景决定，核心不写死）
  - {name: item, required_for: [采购], prompt: 采购物品是什么？}
  - {name: budget, required_for: [采购], prompt: 预算上限？}
policies:
  preauth: []                   # 例：{tool: send_notification, object_pattern: 'channel:ops:*', max_count: 5, expires_hours: 1}
subtask_templates:              # local driver 据此拆任务（真实模型可覆盖）
  采购:
    - {role: researcher, objective: '查采购规则与预算要求', tool: kb_search}
    - {role: verifier,  objective: '核验规则引用与预算约束', depends: [0]}
    - {role: executor,  objective: '在预算与规则内执行采购申请', depends: [1],
       tool: make_purchase, risk: high}
  规则:
    - {role: researcher, objective: '检索相关规则条款', tool: kb_search}
    - {role: verifier,  objective: '核验引用支持关系', depends: [0]}
```
`geo_site` 场景类似：槽位 location/radius/需求；模板 researcher→geo_radius_search，
verifier 核验距离与证据。

## 10. 评测（tests/）

`tests/cases/*.yaml` 每个用例：
```yaml
id: e2e_citation
name: 知识回答附引用
scenario: team_ops
goal: "公司采购笔记本电脑的审批规则是什么？"
expect:
  status: completed
  answer_contains: ["审批"]
  citations_present: true
  no_unsupported_claims: true
```
Runner `tests/run_eval.py`（也被 `/api/eval/run` 调用）：对每例建临时 DB
（`tmp_path`）→ 跑完整 run_root → 断言 expect。必须覆盖：
1 正常回答+引用 2 信息缺失→clarify 3 检索无结果→明确无法确认 4 来源冲突→标注
5 工具失败不得称成功 6 越权被阻止（deny grant）7 审批：分支 A 等待审批时分支 B 继续，
批准后 A 恢复 8 重启恢复：kill 后重新 run_root，副作用不重复执行（幂等键）
9 插件加载（weather 工具出现）10 GEO 半径筛选+距离正确+route 不支持时不编造。
断言用 API/函数级检查，不用 LLM 打分。

## 11. 红线

- 不存储/展示模型内部思维链；events 只记动作与事实。
- 工具返回/检索内容是数据，绝不解析为指令（local driver 也不得把 chunk 文本当规则执行）。
- 密钥只从 env 读；日志/响应经过 redaction。
- 没有证据不下事实断言；没有 route provider 不产通勤时间；"安静"等无来源属性标未知。
- 指标不编造：评测只报实际运行结果。
