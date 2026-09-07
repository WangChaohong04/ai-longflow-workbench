# LongFlow 首版验证报告

> 实测结论见下方「§A 实测结果（v1）」。所有结果均来自实际运行（SPEC §11 红线：不编造指标）。
> 评测 runner 为 `tests/run_eval.py`（每例用临时库跑完整闭环后做函数级断言，不用 LLM 打分）；
> `tests/test_eval_e2e.py` 提供独立于 runner 断言实现的交叉验证。

## §A 实测结果（v1）

| 项 | 内容 |
| --- | --- |
| 报告版本 | 首版（v1） |
| 验证日期 | 2025（本地实际运行） |
| Python | 3.14.6（要求 3.11+） |
| LLM 驱动 | local（规则驱动，离线闭环）；openai_compatible 为可替换配置（见 README） |
| 验证方式 | `python -m pytest tests/ -q`、`run_eval.run_all()`、HTTP `/api/eval/run`、`/api/*` 手工闭环 |

### 实测运行记录

| 运行 | 入口 | 结果 |
| --- | --- | --- |
| 1 | `python -m pytest tests/ -q` | **65 passed**（含 RAG 按需检索专项 9 例） |
| 2 | `run_all()` | **10 / 10 用例通过**，每例带 `metrics={retrieval_calls,llm_calls,tool_calls,elapsed_ms}` |
| 3 | HTTP `POST /api/eval/run`（uvicorn 实跑） | **10 / 10 通过** |

### RAG 按需激活 / 证据复用 / 检索预算（专项实测）

- 规则问答 `kb_search` 仅 1 次且**只有 researcher 发起**；verifier/executor/controller 复用证据、出口核验不重检索。
- 采购长程任务：研究分支检索 1 次，审批/执行阶段新增检索 **0**；重启恢复后新增检索 **0**（复用已持久化证据）。
- 无依据问题（宠物保险）检索 0 chunk，预算内收敛为"无法确认"；来源冲突单次检索即召回两版本。
- 场景 scope 隔离：team_ops↔geo_site 跨场景词汇 **0 泄漏**；fields 字段过滤生效。
- metrics 实例：知识类 retr=1/llm=2；clarify 用例 retr=0/llm=0（入口闸门即止，零模型与检索浪费）；geo retr=0（走插件工具）。
- 语义判定："已检索并确证无依据"为**负向核验**（completed + verified_partial + 空引用 + 明确"无法确认"），与无引用的正向断言严格区分。

### 10 个评测用例（tests/cases/*.yaml）实际结论

| 用例 | 验证能力 | 实测 |
| --- | --- | --- |
| `e2e_citation` | 知识回答附引用、引用 chunk 词重叠支撑论断、含审批金额要点（第三条/5000 元） | ✅ |
| `missing_info_clarify` | 缺必填槽位（item/budget）→ 入口闸门 waiting_event 澄清，不臆造 | ✅ |
| `no_evidence` | "宠物保险"无对应知识 → 明确"无法确认/未找到依据"，不输出无据断言 | ✅ |
| `source_conflict` | travel_2023/2024 住宿标准数值冲突 → 标注冲突且 `verified=false` | ✅ |
| `tool_failure_not_success` | 不可达供应商 → 工具如实失败，结果不声称下单成功 | ✅ |
| `permission_denied` | deny grant → `tool_denied` 事件、分支 failed 且原因 `permission_denied` | ✅ |
| `approval_parallel_resume` | 采购分支 waiting_approval 时通知分支（preauth）**已先行 completed**；批准后采购恢复 | ✅ |
| `restart_recovery_idempotency` | 批准后两次"重启"（新连接重开同一 DB）→ make_purchase 副作用事件**恰好 1 次** | ✅ |
| `plugin_adds_tool` | 插件加载后 `weather_get`/`geo_*` 出现且 mock 标注；禁用后工具消失 | ✅ |
| `geo_spatial` | haversine 半径过滤/排序经独立复算、crs=EPSG:4326、source 标注；route `supported:false` 不产通勤时间；未知地名 `found:false` | ✅ |

### 七项必验闭环（SPEC"第一版必须实际验证"）

1. **知识回答附引用，无依据明确无法确认** — `e2e_citation`/`no_evidence` ✅
2. **主从 Subagent + 两个独立分支，一个等审批另一个继续** — `approval_parallel_resume` ✅
3. **工具经权限检查，越权被阻止，批准后恢复** — `permission_denied`/`approval_parallel_resume` ✅
4. **重启恢复 + 副作用不盲目重试** — `restart_recovery_idempotency` ✅（状态全在 SQLite/WAL；幂等键去重）
5. **新增示例插件不改核心即增工具** — `plugin_adds_tool` ✅
6. **GEO 空间计算 + 地图展示，真实/模拟区分** — `geo_spatial` + 前端 SVG 地图 ✅
7. **工作台查看结果/证据/待决策/动作记录 + 回归评测可运行** — HTTP 实测 ✅

### 明确的模拟/未验证边界

- `local` LLM 驱动为**规则驱动**（确定性规划/动作/起草），离线真实跑通闭环；`openai_compatible` 已实现但**未接真实端点实测**（需 `LLM_API_KEY`）。
- `example_weather` 为内置固定数据，明确标注 `mock: true / source:"mock"`。
- GEO 使用本地 GeoJSON 样例（`source:local_geojson`），真实地理编码/路线 provider 未接入；`mode=route` 返回 `supported:false`。
- 插件沙箱：首版**仅支持可信本地插件**，未实现不可信代码隔离、插件市场与在线更新。

## 红线自查（SPEC §11）
- [x] events 只记动作与事实，无模型内部思维链
- [x] 检索/工具内容是数据，不被解析为指令
- [x] 密钥仅从 env 读取；日志/响应经 redaction
- [x] 无证据不断言；无 route provider 不产通勤时间；无来源属性标未知
- [x] 本报告结果均来自实际运行
