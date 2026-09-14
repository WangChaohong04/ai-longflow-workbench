# 演示与操作流程

本指南提供本地展示顺序。真实浏览器完整操作未在本次环境验收；以下 API/界面入口应与当前代码一起复核，不是成功截图或已完成报告。

## 1. 启动与检查

按 README 创建虚拟环境并安装依赖，从项目根目录执行：

```bash
python -m longflow.cli serve
```

打开 `http://127.0.0.1:8765/`。如界面一直加载，先运行 `node tools/check_web.mjs` 并检查浏览器控制台。健康接口为 `/api/health`，交互式 API 文档为 `/docs`。

不要对外开放默认的本地管理员模式。全部演示先使用样例数据，避免上传真实内部资料。

## 2. 知识问答：目标到引用

提交：`公司采购笔记本电脑的审批规则是什么？`

观察任务目标、识别领域、研究节点、引用、核验状态和事件记录。默认 local 模式是确定性规则，不是远程模型调用。没有依据的结论不应显示为确定事实。

若需要显式场景，选择 `team_ops`。也可使用 API：

```powershell
$base = 'http://127.0.0.1:8765'
$body = @{goal='公司采购笔记本电脑的审批规则是什么？'; scenario='team_ops'} | ConvertTo-Json
$r = Invoke-RestMethod "$base/api/tasks" -Method Post -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
$id = $r.task.id
Invoke-RestMethod "$base/api/tasks/$id"
```

任务创建是后台执行：返回 ID 不代表完成。用工作台观察状态；不要紧密循环轮询。

## 3. 澄清与审批

选择 `team_ops`，提交：`帮我采购 3 台笔记本，预算 2 万`。

如系统询问信息，按确认卡片补齐；高风险动作需查看具体工具与参数后决定批准或拒绝。样例工具并不意味着接入真实采购平台。可展示拒绝、取消和恢复，不能把等待审批讲成已采购成功。

## 4. 资料导入、激活与再次研究

准备一份不含秘密的 Markdown：

```markdown
# 演示设备政策
版本 demo-v1
## 采购限制
演示设备预算上限为 12345 元。
```

在知识管理页面导入，选择合适领域；检查解析预览、分段和警告后再确认激活。预览状态不应直接变成可检索的正式知识。激活后提出与资料相关的问题，检查引用是否真的来自该文档，而不是假设导入即保证回答正确。

对应 API：
- `POST /api/knowledge/import` 或 `/api/knowledge/upload`
- `GET /api/knowledge/docs/{id}`
- `POST /api/knowledge/docs/{id}/trial`
- `POST /api/knowledge/docs/{id}/activate`

参数以服务的 `/docs` 为准。PDF 文本解析可选安装 `pypdf`；扫描件需要 OCR，但项目不内置 OCR。

## 5. GEO 与缺失来源

使用 `geo_site` 场景测试地点半径筛选，按提示补充中心和半径。

- 数据来自 `plugins/geo/data/sample.geojson`，不是实时地图全量检索。
- Haversine 是直线距离，不是驾车路线或通勤时间。
- 未配置地图密钥时使用 SVG 示意图；真实地图与路线服务需要额外配置。
- 若汽车/网页研究缺少来源，应展示部分完成或配置提示，不填充伪造价格与车型结论。

## 6. 查看与导出

查看任务的最终状态和失败分支，再导出报告：

```powershell
Invoke-WebRequest -UseBasicParsing "$base/api/tasks/$id/export?format=md" -OutFile report.md
Invoke-WebRequest -UseBasicParsing "$base/api/tasks/$id/export?format=csv" -OutFile report.csv
```

检查证据 ID、引用、未确认标记及失败说明。导出成功不代表每条结论均已核验。演示报告可能含用户数据，不应直接提交到仓库。

## 7. 可重复自动检查

```bash
python -m pytest -q -rs -p no:cacheprovider
node tools/check_web.mjs
node tests/test_map_plugins.mjs
```

Python 测试包含 API 导入、激活、权限隔离、任务恢复与导出契约。JavaScript 静态检查不执行 UI；地图测试使用本地桩。真实浏览器、真实模型及真实网页需单独验收。记录见 [VERIFICATION.md](VERIFICATION.md)。
