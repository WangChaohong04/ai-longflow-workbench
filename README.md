# LongFlow Workbench

**可审计、可恢复、带证据核验的单机长任务工作台。**

LongFlow 将目标拆成持久化任务图，通过受控工具、审批和引用核验组织研究与执行。默认采用确定性本地规则，不需要模型密钥即可运行；可选接入 OpenAI 兼容模型、搜索服务和地图插件。

> 当前适合本地演示与开发，不是开箱即用的公网多租户产品。规则编排不是通用智能，测试通过也不代表真实模型、搜索或地图服务已验收。

## 工作流程

```text
提交目标 / 补充资料
        ↓
领域路由与槽位检查 ── 缺信息 → 结构化澄清 → 用户补充
        ↓
Coordinator 生成任务图（默认本地规则）
        ↓
按需研究：知识库 / 文件 / 网页来源 / GEO
        ↓
标准化证据、来源区分、必要时比较
        ↓
论断与引用核验 ── 不足/冲突 → 部分完成、暂停或返工
        ↓
需要执行时：权限 → 审批 → 幂等工具调用
        ↓
工作台结果、事件记录、反馈及 Markdown / CSV 导出
```

任务、审批、事件和证据保存在 SQLite；后台 worker 负责推进与恢复。工具超时、取消、失败和来源缺失不能伪装成成功。

## 快速开始

需要 **Python 3.11+**。Node.js 22+ 仅用于前端检查，不是启动后端的必需依赖。

```bash
git clone https://github.com/WangChaohong04/ai-longflow-workbench.git
cd ai-longflow-workbench
python -m venv .venv
```

激活虚拟环境：

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# Linux / macOS
source .venv/bin/activate
```

安装并启动：

```bash
python -m pip install -r requirements.txt
python -m longflow.cli serve
```

- 工作台：<http://127.0.0.1:8765/>
- 健康状态：<http://127.0.0.1:8765/api/health>
- API 文档：<http://127.0.0.1:8765/docs>
- 自定义端口：`python -m longflow.cli serve --port 8803`
- 等价入口：`python -m uvicorn longflow.api:create_app --factory --port 8765`

启动会初始化数据库，不存在 `longflow.api:app` 模块级入口。请从项目根目录运行。程序直接读取进程环境变量，**不会自动加载 `.env`**。

## 如何展示

完整步骤见 [演示与操作流程](docs/DEMO.md)。建议按顺序展示：

1. **知识问答**：提交“公司采购笔记本电脑的审批规则是什么？”，检查引用、研究节点及核验结果。
2. **审批与恢复**：选择 `team_ops` 场景，提交“帮我采购 3 台笔记本，预算 2 万”，查看澄清、审批与事件；样例采购不代表真实下单服务。
3. **GEO 分析**：使用 `geo_site` 场景和本地样例数据，展示半径筛选、直线距离与无地图密钥时的 SVG 回退。
4. **知识导入与导出**：预览资料、确认激活、发起相关任务，查看结果及 Markdown/CSV 导出；不把未确认内容当作已核验事实。

这些是可操作的演示入口，不是本轮真实浏览器演示已通过的声明。无外部搜索配置的研究可能暂停或部分完成，应如实展示原因。

## 已有能力

| 能力 | 实现与边界 |
| --- | --- |
| 任务编排 | 领域注册、路由、槽位澄清、任务图和固定能力子任务；默认规则规划 |
| 检索与证据 | BM25、范围与时效过滤、来源记录、规范化数据与证据 ID |
| 导入 | TXT/MD/JSON/DOCX/PDF 预览、激活及版本管理；PDF 解析需要可选 `pypdf`，不内置 OCR |
| 核验 | 数字/单位、条件与引用检查；verified/failed/partial/insufficient，不保证消除幻觉 |
| 工具治理 | 参数校验、权限、审批、预算、幂等、取消与不确定结果处理 |
| 持久化 | SQLite WAL、后台调度、重启恢复；不支持分布式多副本部署 |
| 模型与搜索 | OpenAI 兼容驱动、可配置搜索端点与百度适配器；需自行配置并验收真实服务 |
| 地图 | 本地 GeoJSON 与 Haversine；高德可选，Google 插件仍是占位；无路线服务不编造通勤时间 |
| 工作台 | 任务、确认、审批、知识、插件、反馈及报告导出 |

## 配置与安全

- 主配置：`config/longflow.yaml`
- 场景：`config/scenarios/`
- 环境变量示例：`.env.example`（不含真实密钥）
- 可选引导：`tools/api_keys_bootstrap.ps1`，采用隐藏输入；不将凭证写入文件。
- 模型变量：`LONGFLOW_DRIVER=openai_compatible`、`LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`。
- 根目录 `data/` 是运行数据，不上传；插件中的 `plugins/geo/data/sample.geojson` 是必须发布的离线样例。

**未设置认证 token 时是本地单用户管理员模式。不要直接开放到公网。** 可用 `LONGFLOW_AUTH_TOKEN` 开启 Bearer 认证，公开部署还需额外安全设计。插件仅限可信本地代码。详见 [安全说明](SECURITY.md)。

## 自动检查

```bash
python -m pytest -q -rs -p no:cacheprovider
node tools/check_web.mjs
node tests/test_map_plugins.mjs
```

- Python：单元、API、集成及 YAML 回归用例。
- 前端静态检查：解析所有 ES Module，验证 HTML 入口和 imports/exports，不执行 UI。
- 地图测试：本地桩环境测试，不等于真实 SDK 访问。
- 真实模型/网页测试需显式 opt-in；浏览器测试需 Playwright。跳过不是通过。
- GitHub Actions 配置见 `.github/workflows/ci.yml`；远程实际结果以 Actions 页面为准。

实测记录与限制见 [验证说明](docs/VERIFICATION.md)。

## 项目结构与文档

```text
longflow/     后端与原生 Web 工作台
config/       主配置、场景和领域定义
knowledge/    离线知识样例
plugins/      可信插件和 GEO 样例
tests/        契约、单元、集成和回归测试
tools/        前端检查与可选服务接入诊断
docs/         架构、演示、验证和发布审计
```

- [演示与操作流程](docs/DEMO.md)
- [架构说明](docs/ARCHITECTURE.md)
- [插件开发](docs/PLUGINS.md)
- [文件保留与清理依据](docs/REPOSITORY_AUDIT.md)
- [设计契约](SPEC.md)：不是所有设计条款都代表已实现能力。
- [历史闭测记录](docs/CLOSED_BETA.md) / [历史清理记录](docs/ROUND4_BASELINE_CLEANUP.md)：用于追溯，不作为当前验收结论。

## 贡献

提交修改前运行上述检查。修复问题请附最小复现和回归测试；不要通过删除失败测试来制造通过。提交前检查 `git diff --cached`，勿上传密钥、用户资料、数据库、日志或本地会话文件。

## 许可证

见 [LICENSE](LICENSE)。
