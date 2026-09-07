# LongFlow 架构说明

本文讲清 LongFlow 各层职责与关键机制，细节契约以 [`../SPEC.md`](../SPEC.md) 为准。

## 1. 五层职责

| 层 | 模块 | 职责 | 不做什么 |
| --- | --- | --- | --- |
| 接入层 | `api.py`、`web/`、`cli.py` | REST API、静态工作台、`serve/init-db/reset` 命令 | 不含业务判断 |
| 编排层 | `orchestrator.py`、`workers.py` | 主控 Agent：双闸门、任务图、调度循环；后台恢复 tick | 不直接执行工具、不直接授权 |
| 能力层 | `tools.py`、`rag.py`、`llm.py`、`plugins/` | 内置工具、知识检索、LLM 网关、插件工具/知识 | 不自行决定权限 |
| 治理层 | `permissions.py`、`redaction.py`、`events.py` | 授权决策、脱敏、只增审计日志 | 不产生业务动作 |
| 持久层 | `db.py`、`models.py` | SQLite(WAL) 建表/迁移、行模型、状态常量 | 无业务逻辑 |

依赖方向只自上而下：编排层调用能力层，能力层与治理层都落持久层；治理决策（权限/脱敏）对能力层是**强制前置**，不是建议。

```
┌──────────────────────────── 接入层 ────────────────────────────┐
│ 浏览器工作台 (web/, 零构建 ES Module, SVG 地图)                  │
│ FastAPI /api/*  (tasks·approvals·message·eval·plugins·health)  │
└───────────────┬───────────────────────────────▲───────────────┘
                │ POST /api/tasks {goal,scenario,slots}
                ▼                               │ 任务树/事件/审批/地图结果
┌──────────────────────────── 编排层 ────────────────────────────┐
│  主控 Agent (orchestrator.run_root, 可重入可恢复)                │
│  ┌──────────┐   计划/任务图    ┌──────────────────────────┐     │
│  │ 入口闸门  │ ──────────────▶ │ 调度器: ready→in_progress │     │
│  │ 意图/槽位 │                 │  researcher/executor/     │     │
│  └──────────┘                 │  verifier 子Agent 并行    │     │
│  ┌──────────┐  返工≤max_verify │  (waiting 不阻塞其他分支) │     │
│  │ 出口闸门  │ ◀────────────── └──────────┬───────────────┘     │
│  │ 引用/一致 │                            │ 动作请求            │
│  └────┬─────┘                            ▼                     │
│       │ verified=true            workers.py: 2s tick 恢复       │
└───────┼──────────────────────────────────┼─────────────────────┘
        │                                  ▼
┌──────────────────────────── 能力层 ────────────────────────────┐
│ 工具运行时 runtime.call() ── 唯一调用路径                        │
│   kb_search │ http_get │ record_note │ send_notification │      │
│   make_purchase ＋ 插件工具(geo_*, weather_get…)                 │
│ RAG: chunk→BM25+字段过滤→重排   LLM 网关: local | openai_compat  │
└───────┬───────────────────────────┬──────────────┬─────────────┘
        ▼                           ▼              ▼
┌──────────────── 治理层（强制前置，不可绕过）────────────────────┐
│ permissions.decide: allow / needs_approval / deny               │
│ redaction: key/token/手机号/身份证/邮箱   events: 只增审计日志   │
└───────────────────────┬───────────────────────────────────────┘
                         ▼
┌──────────────────────────── 持久层 ────────────────────────────┐
│ SQLite WAL: tasks · approvals · grants · knowledge_chunks ·     │
│             knowledge_sources · events(只增) · eval_runs        │
└────────────────────────────────────────────────────────────────┘
```

## 2. 主从 Agent

- **主控 Agent（controller）**：每个 root 任务一个 `run_root(conn, root_id)`，可重入、可恢复。负责入口闸门、生成 `plan_json`、创建/校验子任务图、调度 ready 任务、出口闸门。
- **子 Agent 角色**：`researcher`（kb_search / geo 检索取证据）、`executor`（经工具运行时调工具）、`verifier`（收集证据并做出口闸门预检）；插件可注册自己的角色名。
- **并行与阻塞**：调度器反复扫描 ready 任务推进；一个子任务 `waiting_approval` / `waiting_event` 时，其他分支继续。每步检查预算（`max_steps` / `max_llm_calls`）。
- **状态机**：`pending→ready→in_progress→(waiting_approval|waiting_event)→in_progress→completed/failed/cancelled`，后三者为终态。

## 3. 双闸门

- **入口闸门 `gate_entry(goal)`**：意图分类（场景 `intent_keywords`）＋必填槽位 regex。缺槽位 → `clarify`（列出缺失字段，等用户 `/api/tasks/{id}/message` 补槽）；命中风险词且无授权渠道 → 如实 `handoff`，不伪造渠道；情绪词仅记辅助日志。通过后才生成计划与任务图。
- **出口闸门 `gate_exit(root_result, evidence)`**：规则＋可核验信号（不接受模型自报置信度）：
  1. 事实论断均带 `[cite:chunk_id]` 且引用核验通过（见 §6）；
  2. 工具状态与结论一致——失败不得称成功；
  3. slots 中硬约束满足；
  4. 来源冲突 → 标注冲突，不算通过；
  5. 敏感信息已脱敏；high 风险动作均有审批记录。
  
  不通过 → 补充检索 / 修正 / 标 unknown / handoff，最多返工 `max_verify_rounds` 轮；通过 → `completed` 且 `result.verified=true`。

## 4. 任务图与恢复

- 任务以 `parent_id/root_id/depends_on_json` 构成图；建图时校验依赖 id 存在、同 root、无循环（DFS），违例抛 `TaskGraphError`。
- ready 条件 = 所有依赖 completed；任一依赖 failed/cancelled → 本任务 failed 并记 `detail.reason='dependency_failed'`。
- 一切状态（任务、审批、授权、事件）落 SQLite；进程重启后 `workers.py` 后台线程每 2s tick：唤醒 `scheduled_at` 到期任务、审批决定后的等待任务，重入 `run_root` 继续。
- **无模型空转**：等待期间不调用 LLM。
- **副作用幂等**：`side_effect=true` 的工具执行前取幂等键 `sha256(tool_name|canonical_json(args)|root_id)`；恢复时发现该键已有成功 `tool_result` 事件 → 不重复执行，直接复用结果。
- 取消：API 置 `cancelled` 后子任务级联；结果中从 events 汇总已发生副作用清单如实告知。

## 5. 权限模型

授权决策集中在 `permissions.decide(conn, tool_name, args, task_id, scenario)`，输入为 grants 表＋场景 `policies`：

| 风险 | 判定 |
| --- | --- |
| low | 自动 allow（记日志） |
| medium | 匹配未过期、次数未满的 preauth（tool_name＋object_pattern glob）→ allow；否则 needs_approval |
| high | 必须 `once` 审批，且 approval 绑定的 tool_name＋canonical(args) 与请求**完全一致**；否则 needs_approval |
| 显式 deny grant | 永远拒绝 |

工具运行时 `runtime.call()` 是唯一调用路径：deny → 记 `tool_denied` 并抛 `PermissionDenied`；needs_approval → 建 pending approval、任务转 `waiting_approval`、抛 `ApprovalRequired`，**不执行**；allow → 幂等检查 → 带超时执行 → 记 `tool_result`。审批决定时再次比对参数，参数被篡改则拒绝。**权限规则绝不写进 prompt**——prompt 只描述工具用途。

## 6. RAG 与引用核验

- 加载场景时把 `knowledge/*.md|txt|json` 按标题/段落切 chunk 入库（`fields_json` 存业务字段，`citations_json` 存条款/位置）。
- 检索：BM25（stdlib，token 正则 `[\w一-鿿]+`）＋场景字段相等过滤 → 粗排 top 2k → citation/字段命中加权重排 → top_k，返回 chunks＋citations。
- 出口闸门核验：答案中每个 `[cite:chunk_id]` 必须存在，且该 chunk 原文包含答案关键论断锚点（包含/词重叠核验，verifier 实现）；无引用支撑的事实性论断 → 闸门不通过。检索无结果时要求明确回答"无法确认"，而不是编造。

## 7. LLM 网关可替换性

`llm.py` 暴露统一接口 `driver.respond(messages, *, tools=None, json_schema=None) -> dict`，由 `config/longflow.yaml` 的 `llm.driver` 选择：

- **LocalDriver（默认）**：确定性规则驱动，用 messages 中 `system.context`（结构化 JSON）驱动——`plan()` 按意图关键词/槽位正则套场景 `subtask_templates` 拆任务；`next_action()` 按角色状态机产出 `tool_call/answer/clarify/request_approval/delegate_done`；`draft_answer()` 产出带 `[cite]` 的答案。所有"判断"必须同时产出可核验信号。离线、可复现、零成本。
- **OpenAICompatibleDriver**：httpx 调 `{LLM_BASE_URL}/chat/completions`，模型取 `LLM_MODEL`，Key 只从环境变量 `LLM_API_KEY` 读（不落盘、不入日志，redaction 兜底）。

切换驱动不改编排层：编排只依赖结构化动作与可核验信号，因此 local 跑通的闭环在真实模型下复用同一套闸门、权限与恢复机制。

## 8. 请求闭环序列

以"多步采购审批"为例（`→` 同步调用，`⇢` 持久化/异步）：

```
用户/工作台                api.py        orchestrator        工具运行时/权限      LLM网关   workers   SQLite
    │ POST /api/tasks        │                │                   │                │        │        │
    │ {goal,scenario,slots}  │                │                   │                │        │        │
    │───────────────────────▶│ run_root       │                   │                │        │        │
    │                        │───────────────▶│ 入口闸门           │                │        │        │
    │                        │                │ 意图分类/槽位regex  │                │        │        │
    │                        │                │ 缺槽位?           │                │        │        │
    │   ◀──── clarify ───────│◀───────────────│ (返回缺失字段)     │                │        │        │
    │ POST .../message {text}│                │                   │                │        │        │
    │───────────────────────▶│───────────────▶│ 槽位补齐           │                │        │        │
    │                        │                │ 生成plan+任务图 ⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢▶│ tasks
    │                        │                │                   │                │        │        │
    │                        │                │ researcher: kb_search              │        │        │
    │                        │                │──────────────────▶│ decide: low→allow        │        │
    │                        │                │                   │ RAG检索 ⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢▶│ chunks
    │                        │                │ verifier 预检引用  │                │        │        │
    │                        │                │ executor: make_purchase(high)       │        │        │
    │                        │                │──────────────────▶│ decide: high→needs_approval
    │                        │                │                   │ 建approval(pending)⇢⇢⇢⇢⇢⇢▶│ approvals
    │                        │                │ 任务→waiting_approval ⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢▶│ tasks
    │   ◀── 审批卡片(事件流) ─│◀───────────────│ (run_root让出, 无LLM空转)           │        │        │
    │ POST /approvals/{id}/decide {approved}  │                   │                │        │        │
    │───────────────────────▶│────────────────┼───────────────────┼────────────────┼────────▶│        │
    │                        │                │                   │                │ 2s tick │        │
    │                        │                │ ◀─────────────────┴────────────────┴─────────│        │
    │                        │                │ 审批已决: 校验args与请求完全一致              │        │
    │                        │                │ executor 重试 make_purchase                   │        │
    │                        │                │──────────────────▶│ allow→幂等键查无→执行(超时) │
    │                        │                │                   │ tool_result ⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢▶│ events
    │                        │                │ 出口闸门: 引用/工具状态/约束/脱敏/审批记录      │        │
    │                        │                │  通过→completed, verified=true ⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢⇢▶│ tasks
    │   ◀── 结果+引用+日志 ──│◀───────────────│                   │                │        │        │
```

重启恢复序列：进程退出后任务/审批/事件已在库中；重启 `serve` → workers tick 发现到期或审批后任务 → 重入 `run_root` → 副作用工具按幂等键命中既有 `tool_result` → 跳过执行直接复用 → 继续至出口闸门。

## 9. 信任边界

- 工具返回与检索内容是**数据**，绝不解析为指令（local driver 也不把 chunk 文本当规则执行）。
- events 只记动作与事实，不存储/展示模型内部思维链。
- 插件以本机代码身份运行，**仅支持可信本地插件，无沙箱**（见 [`PLUGINS.md`](PLUGINS.md)）。
