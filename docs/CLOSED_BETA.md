# LongFlow 受控封测（Closed Beta）交付说明

本文说明本轮把 LongFlow 从"模块齐备但未打通"推进为**可控的通用长程 Agent Harness**
所做的八件事：安全、主链路、Coordinator 动态任务图、固定 Subagent、事实核验、
文件导入、受控自进化、测试与前端。所有改动均为**增量**，不重写、不破坏旧能力。

---

## 1. 总体架构（本轮到账后的数据流）

```
浏览器(goal-only)
   │ POST /api/tasks            （workspace/owner 以服务端凭据为准，不信请求体）
   ▼
API(Auth Principal, 工作区隔离)
   │
   ▼
RouterAgent（规则+本地实体抽取，可选真实模型）
   领域打分 / 风险 / 槽位 / 是否多领域 / 是否需要交接
   │  顺序：handoff → 多领域且高风险=等待 → 多领域 → 低置信澄清
   │        → 缺槽澄清 → 单领域高风险等待 → 激活
   ▼ 缺关键信息：waiting_user（先追问，不臆造）
DomainPack(team_ops | geo | car_research)
   ▼
Coordinator.plan()  → 受校验的有界 DAG（只引用已注册且在白名单的固定 subagent）
   分支(branch_on) × 研究节点(并行) → 标准化 → 证据核验 → 对照
   ▼
SubagentRunner（固定能力、returns_decision=False，只回 EvidenceRecord）
   web/official/forum 走可替换 SearchProvider（按 query 检索结果页+正文，非抓首页）
   normalizer / verifier / comparison 为纯计算，无工具、无决策权
   ▼
出口闸门 _gate_exit_coordinator / _gate_exit
   - 证据认识分层（已确认/推断/未确认/无法回答/冲突）
   - 逐案四档核验 + 总体严格判定（见 §5）
   - 高风险动作逐个匹配 同工具+同参数hash+同root 的已批准审批
   ▼
completed | partially_completed | failed | waiting_user | waiting_external
   （终态写 run_telemetry 轨迹；据此自动生成的改进只落 proposed，绝不自动生效）
```

关键约束（始终成立）：

- Subagent 只返回证据，不做最终决策、不扩大工具白名单。
- 高风险动作（下单/支付/外发）默认 **只读研究 + 严格审批**；本轮未接任何真实下单/支付/外发。
- 分支研究来源缺失/不可达 → 该分支失败、整单 `partially_completed`，而不是卡死或编造。

---

## 2. 安全与多租户（P1）

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 身份认证 | `longflow/auth.py` | Bearer Token + `X-Workspace`；无 token 配置时为本地单用户 admin（仅内测）。 |
| 工作区隔离 | `longflow/api.py` | 任务/审批/授权/知识/反馈一律按 Principal 的 user+workspace 过滤；**忽略请求体** `owner/workspace/by`。跨工作区读返回 404（不泄露存在性），写返回 403/404。 |
| 高风险审批绑定 | `longflow/permissions.py`、`orchestrator._gate_exit` | 审批绑定 `root 任务 + 工具 + 完整参数 hash`；出口闸门对每个高风险 tool_result 事件**逐条匹配**审批，不能"在该 root 下批过任意一个就放行"，更不能跨 root 复用。 |
| 授权管理 | `/api/grants` | 仅 admin。 |
| SSRF 防护 | `longflow/netguard.py`、`tools.http_get` | 仅 http/https；阻断 localhost、私网/环回/链路本地/保留/组播（字面 IP 与 DNS 解析后都查）、云元数据 `169.254.169.254`/`fd00:ec2::254`、file 等 scheme；域名白名单后缀匹配；重定向不自动跟随，逐跳复检（防重定向绕过）。被拦截不发起请求并在事件里标 `blocked`。 |

---

## 3. 主链路与"新建任务"兼容（P2）

- `POST /api/tasks`：
  - **不带 `scenario`（或 `auto_route=true`，新默认）** → `create_domain_goal`：Router 识别领域→缺槽先澄清→激活 DomainPack→Coordinator 动态图。
  - **显式 `scenario`** → 旧 `create_goal` 静态模板路径，行为保持不变（兼容）。
- 前端"新建任务"默认**只需输入目标**；"手动指定场景"收进"高级/兼容选项"折叠区，默认勾选"自动识别领域"。
- 澄清后用户自由文本回复即可恢复（槽位双向别名匹配，如"纯电"匹配选项"纯电车"）。

---

## 4. Coordinator 协议与三个可运行领域包（P3）

Coordinator（`longflow/coordinator.py`）输入：目标、已知槽位、允许的 subagent/工具、风险；
输出一张**有界、结构化、经过校验**的 PlanGraph：

- 节点上限 `MAX_NODES=24`；key 唯一；依赖只能向前（DAG）。
- 每个研究节点的 subagent 必须**已注册且在该领域包白名单内**，且 `returns_decision=False`。
- 节点类型仅 `research / normalize / verify / compare`；Coordinator **不能自行扩工具、造 subagent**。
- 支持并行研究（parallel_group）、分支依赖、部分失败（汇聚节点把"依赖失败"也视为可推进）、waiting_user/external、可恢复重试。

领域包（`config/scenarios/*.yaml`，由 `longflow/domains.py` 装载）：

| 包 | 场景 | 分支/要点 |
|---|---|---|
| `team_ops` | 团队行政/采购/差旅知识与高风险审批 | 知识问答可完成；采购等高风险先澄清后等待审批，不自动执行。 |
| `geo_site` | GEO 选址 | geocode/radius/distance 真实 haversine；无路径 provider 不编造通勤时间。 |
| `car_research` | 汽车研究 | `branch_on: energy_type`。"10万以内家用车"缺能源类型→**先问**；回答"全部比较/都可以"→建 **燃油 icev / 纯电 bev / 插混 phev / 增程 erev** 四条研究分支，再汇聚标准化/核验/对照。 |

汽车的四分支行为由 YAML `branch_on` 驱动，**核心代码不含任何汽车硬编码**。

---

## 5. 固定 Subagent：提示词与 I/O 协议（P4）

统一 I/O：`SubagentRequest{query, target_sites[], allowed_domains[], required_fields[],
max_sources, time_range, extra{weights,...}}` → `SubagentResult{ok, needs_user, question,
error, evidence:[EvidenceRecord], normalized, findings, limitations}`。
所有研究型 subagent **只产出 EvidenceRecord，不产出决策**（`returns_decision=False`）。

| Subagent | 一句话提示词 | 允许工具 | 关键 I/O 约束 |
|---|---|---|---|
| web_researcher 网页检索员 | "访问允许网站收集公开信息，只返回带来源证据。" | http_get | 经 SearchProvider 按 **query/domains/time_range/fields** 检索**结果页并提取正文**（不是首页）；无后端→`source_not_configured`（失败、不阻塞、不臆造）。 |
| official_source_researcher 官方资料检索员 | "收集官网/政策/公告等权威来源，source_type=official。" | http_get, kb_search | 无外部站点配置时退回已激活官方知识库；权威来源标高事实层。 |
| forum_researcher 论坛观点检索员 | "收集论坛/社区观点，主观观点须与事实分开标注。" | http_get | 必须保留**帖子 URL、发布时间、样本数(回帖)、正面/负面观点命中、局限**；证据层=观点(opinion)，不冒充事实。 |
| file_researcher / rag_researcher | "只检索用户已激活的文件/知识库。" | kb_search | 不碰未激活预览文档；命名空间 `user:<workspace>:<domain>` 隔离。 |
| geo_researcher 地理分析员 | "坐标/半径/haversine/GeoJSON；直线距离与道路、评分、营业状态分开标注。" | geo_* | 无道路 provider 时明确不支持通勤时间。 |
| normalizer 标准化员 | "统一对象/规格/单位/版本/字段为可比较结构，不产生新事实。" | 无 | 金额→元、距离→km、容量→L、时间→分钟；能耗 L/100km 与 kWh/100km **不互相换算**；实体别名归并但保留型号/版本；纯计算。 |
| evidence_verifier 证据核验员 | "检查来源/时间/冲突/证据强度，输出四档结论，不替用户下业务决策。" | 无 | 来源分层 + 冲突检测，只给核验不给结论。 |
| comparison_agent 对比分析员 | "按**用户已确认**的标准做对照表与权衡，不替用户最终选择。" | 无 | 必须传 required_fields；仅在用户给权重时输出**透明**线性归一评分（成本/能耗/距离反向），并显式声明"评分为排序参考、非客观事实"。 |

可替换搜索 Provider：`longflow/search_provider.py` 定义 `SearchProvider` 协议、
`NullSearchProvider`（不臆造）、`HttpSearchProvider`（端点与结果页均过 netguard/白名单）。
真实环境注入实现同接口的官方/授权 API 即可，runner 构造时可注入。

---

## 6. 事实核验（P5）

`longflow/verification.py`：

- 论断级（数字+单位、条件极性），不是整段词重叠。
- 数字支撑要求**数值+单位一致**且**业务指标/实体兼容**、**时间(年度)/版本不冲突**：
  - "住宿上限 500 元" **不能**支撑"采购预算 500 元"（跨业务指标，判 unconfirmed，绝不算 verified）。
  - "2024 年 500 元"不能支撑"2025 年 500 元"。
  - 同指标同年同单位但数值不同 → contradicted（failed）。
- 四档：`verified / partial / insufficient / failed`；无数值/极性的普通事实，确定性方法无法证明，
  即使有引用也**不自动 verified**（落 partial/待人工确认）。
- 总体判定 `overall_verified(...)`：只有"存在实质结论 + 所有关键事实 verified + 无冲突 +
  无失败工具 + 无越权 + 无未决高风险审批"才 `verified=true`。
- 前端结果区显示四档徽标、各档计数、未通过原因（含来源），以及 Coordinator 的
  证据认识分层（已确认/推断/未确认/无法回答）与分支成败。

---

## 7. 文件导入（P6）

- `POST /api/knowledge/upload`：`multipart/form-data`，字段 `file`（txt/md/json/docx/pdf）
  + 可选 `domain`、`version`。零依赖解析器 `longflow/multipart_read.py`（本机无 python-multipart）。
- 流程：**解析 → 预览(preview，不参与检索) → 用户确认 → 激活(active) → 同名旧版归档(archived)**。
- 元数据：来源文件、页码(section p.N)、标题、版本、上传者、workspace、domain、access_scope。
- 检索服务端按 user/workspace/domain 过滤（命名空间 `user:<ws>:<domain>`），客户端传 scenario 不被信任。
- docx/pdf 需要 `python-docx` / `pypdf`；缺库时端点明确返回 422（不会静默成功）。
- 前端新增"知识"页：选择文件→上传预览→列表→一键"确认激活"，显示状态/版本/上传者。

---

## 8. 受控自进化（P7）

- 终态自动写 `run_telemetry`：目标、最终状态、路由/领域、subagent、来源、证据数、
  工具调用/失败、决策就绪、归因（10 类）、时延、模型调用数。观测失败不影响主流程。
- 自动生成的改进**只落 `proposed`**（`longflow/improvements.py`），绝不自动改
  prompt / 知识 / 权限 / 代码。
- 生命周期与 API（**全部 admin-only**）：
  `POST /api/improvements/suggest`（生成候选）→ `/{id}/evaluate`（登记离线回归结果）
  → `/approve`（人工审核）→ `/rollout`（灰度；未审核调用返回 409）→ `/rollback`（回滚）。
  `GET /api/improvements`、`GET /api/runs`。
- 前端"演进"页按状态提供对应操作按钮，并明确提示"系统不会自动改代码/Prompt/权限"。
- "数据沉淀（telemetry/feedback）"与"系统修改（候选→灰度）"在状态机上严格分离。

---

## 9. 启动 / 测试 / 接真实 API

### 启动（本地内测，零构建前端）

```powershell
$py = "C:\Users\chaoh\AppData\Local\Programs\Python\Python314\python.exe"
$env:PYTHONIOENCODING="utf-8"
& $py -m longflow.cli serve --host 127.0.0.1 --port 8000
# 浏览器打开 http://127.0.0.1:8000/  （"新建任务"直接输目标即可）
```

### 测试（离线、确定性、不用真实 LLM 打分）

```powershell
$env:PYTHONIOENCODING="utf-8"
& $py -X utf8 -m pytest tests/ -q -p no:cacheprovider          # Python 回归
& "C:\Program Files\nodejs\node.exe" tests\test_map_plugins.mjs  # 前端/地图插件
```

### 接真实能力（按需，默认不接）

- 真实模型：配置 `llm.driver=openai_compatible` 及端点/key；规划本身始终确定性、不耗模型。
- 真实网页/官方/论坛：实现 `SearchProvider` 接口或配置 `search_endpoint` + `allowed_domains`
  （出站全程受 SSRF/白名单约束）。
- docx/pdf：`pip install python-docx pypdf`（当前受限环境未安装，故该两类真实解析标为未验证）。
- token 鉴权：在 `auth.tokens` 配置或 `LONGFLOW_AUTH_TOKEN`；前端控制台
  `localStorage.setItem("longflow.token", ...)`、`setItem("longflow.workspace", ...)`。

---

## 10. 测试结果与"分层可信度"

务必区分五类测试，**mock 通过 ≠ 真实通过**：

| 层 | 内容 | 当前状态 |
|---|---|---|
| 离线规则 | 路由顺序、Coordinator 校验、权限/审批 hash 绑定、SSRF、单位/指标核验、改进生命周期 | **2026-09-09 复核：331 passed、4 skipped；node ALL PASS** |
| mock/桩 | 搜索 Provider 桩（query 检索、论坛结构字段）、NullProvider 不臆造、透明评分 | **通过**（`test_subagent_provider.py`） |
| 真实模型 | openai_compatible 端到端 | **未验证**（本地默认 LocalDriver；需配 key，属 opt-in） |
| 真实网页 | HttpSearchProvider 对真实站点检索/正文提取 | **未验证**（默认 NullProvider，不联网） |
| 真实地图 | 外部路径/通勤 provider | **不支持**（只有本地 haversine；通勤时间明确不编造） |
| docx/pdf 真实解析 | python-docx/pypdf | **未验证**（环境缺库；缺库时 422，已测） |

新增/重点用例：高风险审批不可跨 root、越权跨工作区被阻、SSRF/非白名单/重定向被拒、
默认建任务走 Router 不手选场景、汽车澄清→四分支、单能源单分支、subagent 并行部分失败、
网页按 query 检索、DOCX/PDF multipart（缺库 422）、部分/不足不判 verified、同额不同指标不支撑、
自进化"仅 proposed + 未审核不可灰度"、浏览器 输入→澄清→执行→结果 全流程。

### 本轮（第二批）补齐

- **真实 `retrying` 状态**：见上；不再只是工具层重试。
- **5 段结构化确认真正接入编排**：`confirmation.py` 的 build_confirmation 此前仅定义未接线；现在路由的 clarify / multi_domain / handoff / await_confirmation 四类暂停都会持久化 `result.confirmation`（已确认信息/当前问题/待决事项/可选方案/**每个方案的影响**），不用默认值掩盖不确定性。
- **困难暂停快照**：Coordinator 在部分完成/证据不足/冲突时，结果附 `pause_snapshot`：已完成工作、证据引用（entity/field/来源类型/URL/标题/版本/采集时间）、失败原因、待决问题、恢复步骤，跨重启可查。
- **前端**：等待时渲染结构化确认卡（方案按钮直接带"影响"）；部分完成折叠展示暂停快照/恢复步骤；`retrying` 有状态徽标。默认只突出结果与待办，过程折叠，不展示模型思考。

### 本轮（第三批）补齐

- **槽位冲突确认真正接入**：澄清中用户改了与已确认槽位冲突的值（如"预算改成5万"且旧值 8 万），不再被"已存在不覆盖"静默吞掉——在写库**之前**做冲突预检，落到 `slot_conflict_pending` + 5 段确认；"保持原值/采用新值/取消"三个选择各有影响说明，选择后继续。
- **无证据暂停（用户确认优先）**：Coordinator 汇聚时若 `confirmed==0`（全部来源失败/未召回），不再"无法确认即终态"，而是保持 `partially_completed` 附 `confirmation`（PAUSE_NO_EVIDENCE）与 `pause_snapshot`，由用户在"重新检索/只给有据部分/我提供资料/就此结束"中选择；证据不足绝不标 verified。
- **真实层 opt-in 独立报告**：新增 `tests/test_real_layer_optin.py`，真实模型/真实网页检索仅在 `LONGFLOW_REAL_MODEL=1`/`LONGFLOW_REAL_WEB=1` 且配置齐全时执行，默认 skip；离线/模拟/真实三层严格分开，模拟结果不算真实通过。

### 本轮（第四批）补齐

- **前端"主 Agent / 已激活 Subagent"显式摘要**：结果卡顶部新增"研究执行摘要"（`buildExecSummary`）——显示 主 Agent（领域）、已激活 Subagent 清单及各 subagent 完成/总数、分支列表与证据总量，过程仍折叠、不展示模型思考。
- **测试覆盖映射**：见下方清单（目标"补测试"逐项落到对应文件，全部通过）。

目标要求的测试项 → 覆盖文件（皆有用例并通过）：

| 目标测试项 | 对应文件 |
|---|---|
| 路由识别 / 低置信澄清 / 多领域 | `test_router_domains.py`、`test_router_multidomain.py`、`test_router_api.py` |
| 槽位缺失 / 不重复问 / 全部比较多分支 | `test_slots_confirmation.py`、`test_domain_pipeline.py`、`test_geo_api_slots.py` |
| 槽位冲突请求确认 | `test_slot_conflict_flow.py`（新增批次4） |
| Subagent 参数 / 并行部分失败 | `test_subagent_runner.py`、`test_subagent_provider.py`、`test_domain_pipeline.py` |
| 冲突 / 过期 | `test_evidence_records.py`、`test_coordinator.py`、`test_knowledge_expiry.py` |
| 无证据暂停 / 确认后继续 | `test_confirmation_continue.py`（批次4） |
| 超时恢复 / 重启恢复 | `test_retry_confirm_snapshot.py`、`test_recovery_concurrency.py` |
| 高风险审批 / 浏览器全流程 | `test_security_p1.py`、`test_governance_and_cancel_guard.py`、`test_browser_flow.py` |
| 真实模型 / 真实 API 独立分开 | `test_real_layer_optin.py`（LONGFLOW_REAL_MODEL / LONGFLOW_REAL_WEB 开关，默认 skip） |

### 已知限制 / 尚未做

- `retrying` 已落地为真实状态：研究节点遇可恢复瞬时错误（超时/5xx/外部源暂时不可达）进入 `retrying`，按 `limits.max_node_retries`（默认 2）有界重试，`retry_count/last_error/recovery_steps` 持久化在 plan/result，进程重启后由恢复循环再次领取；`source_not_configured` 等配置缺口不重试（直接分支失败）。
- 实体别名表为可扩展占位（`_ENTITY_ALIAS_RULES`），领域级型号别名可由包/知识注入。
- 前端为零构建原生 ESM，改动 `longflow/web/*` 后**刷新页面**即生效（无 HMR）。
- 单机/单进程 + SQLite WAL；不做分布式队列、多副本、插件市场或不可信插件沙箱。
- 不接真实下单/支付/外发；这些动作永远停在审批与只读研究。
