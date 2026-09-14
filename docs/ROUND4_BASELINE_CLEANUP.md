# LongFlow 清理与基线工作记录（本轮）

> 状态：**基线/清单完成；A1–A4 已执行；运行环境已恢复并完成回归验证**。
> 本文档只记录在本会话中由 `grep/read` 直接确认的事实，不采信"全绿/已完成"的旧结论。
> 本轮已完成验证：pytest **331 passed、4 skipped**，评测用例 **10/10**，地图 Node 回归 **ALL PASS**。

---

## 0. 基线观察（仅含本会话亲眼确认的）

### Git / 测试
- `git status` 已可运行；当前工作区包含本轮代码、测试和文档变更，尚未提交。
- `python -m pytest -q -p no:cacheprovider` 已实测 **331 passed、4 skipped**。

### 能力落地级别（grep/read 确认）
| 能力 | 落地状态 | 证据 |
|---|---|---|
| 总路由/领域包/Coordinator | 正式任务流程（`api.create_task` 默认主链路调用 `create_domain_goal`→`tick`） | api.py:250-265 |
| Coordinator 只读任务图 | 正式流程，但**无条件跑全部研究员**（见 §4 G1） | coordinator.py:138-154 |
| 固定 Subagent 执行 | 正式流程（`SubagentRunner.run`） | subagent_runner.py:47-83 |
| 搜索 Provider | 有抽象+Http/Null Provider，但 `fetch_content` 空转（见 §4 G2） | search_provider.py:61-105 |
| 类型化槽位/澄清/冲突确认 | 正式流程；本会话复核过确认卡与冲突路径存在 | orchestrator.py user_message |
| 证据分层（EvidenceRecord） | 正式流程；但 web 研究恒 `field=web_content/value=None`（见 §4 G4） | subagent_runner.py:166-174 |
| RAG 文件导入/激活/归档 | 正式流程（importer.py 预览/激活/归档） | importer.py |
| GEO 按需 | 正式流程（geo_radius_search）；route 通勤明确不支持 | subagent_runner.py:186-228 |
| 能力配置概览 / 结果导出 | **未见实现**（需补，Phase 6c） | — |
| 可见知识预览 | 预览=解析+元数据；**是否有"试问/引用/逐段展示"待核**（Phase 6b） | importer.py |
| 分支重试单分支 | **未见**（Phase 6a） | — |
| 真实模型 / 真实网页 / 真实地图 / docx·pdf | opt-in 测试框架存在（test_real_layer_optin.py），**实际联网未验证** | — |

---

## 1. 清理清单（分四类）

> 状态：**A1–A4 ✅ 已执行并复核无残留引用；回归验证已完成**。

### A. 可删除（本会话确认 0 引用，死代码）
1. **`longflow/tools.py:20` `APPROVED_VENDORS = {...}`** ✅已删
   - 证据：全仓 `grep APPROVED_VENDORS` 仅 1 处（定义本身）；tools.py 内部只用了 `UNREACHABLE_VENDORS`（line 165）。
   - 说明：供应商名录实际由 `verification.extract_approved_vendors` 从知识抽取，此硬编码集合是残留占位。
2. **`longflow/netguard.py:102` `safe_redirect_handler()`** ✅已删
   - 证据：全仓 `grep safe_redirect_handler` 仅定义处；工具真实重定向处理是 `tools.py:110-120`、`search_provider.py:82` 的内联 `follow_redirects=False` 逐跳校验。
   - 说明：dead function，保留由 test_security_p1 使用的 `check_url`。
3. **`longflow/verification.py:324` `evidence_overlap()`** ✅已删（连带删除仅被其使用的 `from . import rag`）
   - 证据：全仓 `grep evidence_overlap` 仅定义处。
   - 说明：死函数（词重叠已不作为判据）。
4. **`longflow/importer.py:108` `import xml.etree.ElementTree as ET`**（docx 兜底段内）✅已删
   - 证据：该兜底只用 `re.findall(r"<w:t...>...")` 提取文本，`ET` 从未使用。
5. **`.lfwork/smoke2.db, smoke2.db-shm, smoke2.db-wal`**
   - 证据：冒烟测试遗留运行时库（`.lfwork` 为可丢弃工作目录）。**逐项删除**，不删 `.lfwork` 目录本身。

### B. 合并后删除（去重复）
6. **`longflow/web/maps/panel.js:17` 的 `MODE_LABEL` 重复定义**
   - 证据：`web/maps/index.js:30-35` 定义 `MAP_ROUTE_MODES`（driving/walking/transit/cycling 中文标签）；panel.js:17 又自建相同 `MODE_LABEL`，其余部分未使用。
   - 方案：`modeLabel(id)` 改为从 `MAP_ROUTE_MODES` 查标签，删除 `MODE_LABEL`。panel.js 已 `import { MAP_ROUTE_MODES }`（line 13）。

### C. 兼容性保留（暂不动）
- `longflow/web/maps/index.js:126` `export const setManualMapPlugin = setActiveMapPlugin` —— 兼容旧名称导出，**保留**（可能被外部 import）。
- 旧 `scenario` 入口 / `subtask_templates` / 静态 `create_goal` —— **保留**（Phase 3 要求双入口兼容）。
- `config.py public_config/map_plugins` 白名单逻辑、`tools.UNREACHABLE_VENDORS`（有引用）—— 保留。
- `db` 字段、`events` 审计、`approvals/grants` 表结构 —— **禁止**仅凭"无直接调用"删除。

### D. 用户数据禁止删除
- `data/`（任务库）、`.git`、`.env`、`knowledge/`（含样例，已知"过期/冲突"测试依赖旧版）、
  `plugins/geo/data/sample.geojson`、用户上传文件、`approvals/events/feedback/llm_calls/eval_runs` 审计表。
- `.lfwork` 目录整体不删，仅清理明确测试库文件（见 A5）。

---

## 2. 不应动 / 已复核安全的（避免误删公开接口）
- `netguard.check_url`（test_security_p1 引用）、`permissions`、`tools.ToolRegistry/tool 注册`、
  `subagents.sa.*` 常量、`cli.py` 入口、`Plugin` SDK —— 均为公开/动态注册入口，**保留**。

---

## 3. 已确认的真实缺口（后续轮次按此修复）

### Phase 4 数据流
- **G1** `coordinator.py:138-154` 无条件运行领域包全部研究员（car 包有 3 个 researcher，简单查询也全跑）。需"按目标选必要研究员"。
- **G2** `search_provider.py` `HttpSearchProvider.fetch_content` 开关从未兑现：`search()` 只用端点返回的 `content`/`snippet`，从不抓正文。需实现真实抓取（SSRF 白名单内）或移除开关并删承诺。
- **G3** `coordinator._research_request` 只注入 allowed_domains/geo center；**预算/偏好/地域/时间范围未写进研究请求**（query 只是 goal）。
- **G4** `subagent_runner._run_web` 恒建 `field="web_content", value=None`，不抽取实体/型号/版本/价格→下游对比表对 web 研究基本为空。缺"摘要 vs 正文"分别标记。
- **G5 ✅已证实** `orchestrator._run_subagent_node`（orchestrator.py:766-772）把依赖节点的**原始 `evidence`** 作为 `input_records` 喂给 normalize/verify/compare（line 772,777）；normalizer 的 `normalized` 只写进自身 result（line 785-788）**从不往下游传**。故 verify/compare 是对原始 record 重排，**未消费标准化结果**（web 研究 value=None 恒对不齐）。

### Phase 5 执行/安全
- **G6** `api.py:262,278` 建任务在请求内**同步 `engine.tick`**；Phase 5 要求尽快返回 id、耗时研究后台执行。需核查后台 worker 是否已覆盖推进，避免请求同步跑完整研究。
- **G7** 上传限制（请求体/大小/耗时/解压），DOCX 含表格、PDF 真文本页、扫描页提示 OCR——需逐项核实。
- **G8 ✅已证实** `api.py:455-474` `POST /api/subagents/run` 与 `api.py:568` `POST /api/eval/run` **无 `require_principal`**：无认证即可执行只读 subagent（触发 kb/geo/搜索）与 CPU 评测；未绑定用户/工作区、未限流。`/api/route`、`/api/domains`、`/api/subagents`、`/api/plugins` 同为无认证只读信息端点（风险较低，但按 Phase 5 要求评测/预览须绑定）。
- **G9** `api.py:250-278` 建任务的同步 tick + `_handle_node_failure`（orchestrator.py:795-836）的自动 retry 只覆盖"自动瞬时重试"；**无用户触发的"单分支重试保留其他证据"**入口（Phase 6a）。

### 功能缺口（Phase 6）
- **G10** 结果导出（Markdown/CSV，保留来源/时间/未确认/失败分支）**未见任何 API/前端**（Phase 6c）。
- **G11** 知识预览已显示前 5 段各 160 字（importer.preview_summary），但**缺解析警告、粘贴文本入口、检索试问、查看引用**（Phase 6b 仅部分）。

### 能力概览（Phase 6c）
- **G12** 能力配置概览表 + 连通自检 **未见实现**；但有基础：`/api/config`(api.py:230)、`config.public_config`、`search_provider`、map config 状态。可在此基础上加"最近连通测试/失败原因"（不显密钥）。

---

## 4. 下一轮（pwsh 恢复后）第一步
1. 记录 Git 状态、确认无并发的 editor，跑 `pytest tests/ -q` 作**真实基线**（失败先归因，不改预期）。
2. 按本文档 A/B 执行剩余清理（A5 删 smoke2.db*、B6 地图标签去重后 `node --check`）。
3. 依优先级修：G8（认证/绑定/限流，安全）→ G5（下游消费标准化）→ G2/G4（fetch+字段提取）→ G1/G3（必要研究员+槽位进请求）→ G10/G12（导出+能力概览）→ G11（预览增强）→ G9（分支重试）。
