# LongFlow Workbench

**集成模型、知识检索、网页研究和地理分析的长任务工作台。**

LongFlow 将用户目标组织成可恢复的任务图，管理澄清、研究、证据核验、工具审批和报告导出。

**正常使用真实模型、在线搜索与高德地图前，需要分别配置对应服务的端点、模型名和密钥。安装 Python 依赖或创建虚拟环境不会自动完成这些接入。**

项目保留无需外部凭证的本地规则与样例模式，便于开发和回归测试；它不等于完整在线功能可用。虚拟环境只是可选的依赖隔离方式，程序实际通过 Python 服务启动，在浏览器中操作。

## 1. 准备程序与依赖

需要 Python 3.11+、Git，以及能够访问所用模型、搜索和地图服务的网络。Node.js 22+ 仅用于开发时的前端检查。

```powershell
git clone https://github.com/WangChaohong04/ai-longflow-workbench.git
cd ai-longflow-workbench
python -m pip install -r requirements.txt
```

你可以使用现有 Python 环境，也可以自行创建虚拟环境。安装依赖和启动服务应使用同一个 Python 解释器。PDF 文本解析需要另外安装 `pypdf`；扫描件 OCR 不在当前内置能力内。

## 2. 准备所需凭证

| 服务 | 要准备的内容 | 用途与缺失影响 |
| --- | --- | --- |
| 模型 API | OpenAI 兼容 Base URL、模型名称、API Key | 用于模型动作与回答生成；未接入时只能使用本地规则模式，或按配置报错/降级 |
| 搜索服务 | 实际端点、符合适配器协议的搜索 API Key、鉴权头和允许域名 | 用于在线资料检索；未配置时相关研究不能当作已完成 |
| 高德 JavaScript API | Web 端 JS Key、对应安全密钥 securityJsCode | 用于工作台在线地图及 SDK 路线能力；缺失时只能回退到本地 SVG 示意图 |
| 高德 Web Service | Web Service Key（仅使用相应服务端能力时） | 与 JS Key 不同，不能替代浏览器地图 Key；填入不代表已实现所有服务端地图功能 |
| 工作台认证 | 自行生成的 `LONGFLOW_AUTH_TOKEN`（按需） | 启用 Bearer 认证；不配置时为本地单用户管理员模式 |

密钥需在各服务商控制台申请，并确认套餐、模型权限、API 权限、额度以及访问限制。高德可在 [高德开放平台](https://lbs.amap.com/) 创建对应类型的应用 Key。

> 模型、搜索和地图是三套独立配置：模型 Key 不能用来搜索或加载地图；高德 JS Key 和 Web Service Key 也不能混用。

## 3. 配置环境变量

以下以 **Windows PowerShell** 为例。在同一个窗口完成配置并启动服务；新开窗口不会继承这里临时设置的变量。程序读取进程环境变量，**不会自动加载 `.env` 文件**，仅复制或填写 `.env.example` 不会生效。

### 3.1 定义隐藏输入函数

先执行一次，后续输入密钥不会直接显示在终端，也不必把真实密钥写进命令历史：

```powershell
function Read-LongFlowSecret([string]$Prompt) {
    $secret = Read-Host $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
        $secret.Dispose()
    }
}
```

隐藏输入不是加密存储：程序调用服务时仍需在进程内存中使用凭证。不要输出这些变量、录制输入过程或提交真实密钥。

### 3.2 模型 API

```powershell
$env:LONGFLOW_DRIVER = 'openai_compatible'
$env:LLM_BASE_URL = Read-Host '模型服务 Base URL（按供应商文档填写）'
$env:LLM_MODEL = Read-Host '模型名称或模型接入点 ID'
$env:LLM_API_KEY = Read-LongFlowSecret '模型 API Key'
```

- Base URL、模型名称和 Key 必须属于相互匹配的服务配置。
- 仅填写 `LLM_API_KEY` 不会自动将默认驱动切换为外部模型，需要设置 `LONGFLOW_DRIVER`。
- 当前任务规划仍是本地规则；外部模型用于动作与回答，不应将规则规划标成模型推理。
- 如不希望外部驱动初始化失败时退回本地，可在 `config/longflow.yaml` 的 `llm` 配置中设置 `strict: true`。该模式不是对所有外部服务可用性的保证。

### 3.3 搜索引擎 API

当前有百度 API 适配器、百度页面模式和受控通用 HTTP 搜索接口，并非填入任意搜索厂商 Key 就能通用。

使用当前百度 API 适配器时：

```powershell
$env:LONGFLOW_SEARCH_KIND = 'baidu_api'
$env:LONGFLOW_SEARCH_ENDPOINT = Read-Host '实际搜索 API 或兼容网关端点'
$env:LONGFLOW_SEARCH_BAIDU_KEY = Read-LongFlowSecret '搜索 API Key'
$env:LONGFLOW_SEARCH_BAIDU_HEADER = 'Authorization'
$env:LONGFLOW_SEARCH_ALLOWED_DOMAINS = Read-Host '允许访问的域名，逗号分隔（包括搜索服务与资料站点，不填URL路径）'
$env:LONGFLOW_SEARCH_FETCH_CONTENT = '1'
```

**先核对协议兼容性：** 当前百度 API 适配器发送 GET 请求，查询参数包括 `q`、`limit`、`kind` 和可选 `time_range`，要求返回以下结构：

```json
{
  "results": [
    {"url": "https://example.com/article", "title": "资料标题", "snippet": "摘要"}
  ]
}
```

- `LONGFLOW_SEARCH_BAIDU_HEADER` 是鉴权头名称；Key 值原样放入该头，程序不会自动添加 `Bearer ` 前缀。
- 如果服务商要求其他方法、请求体或返回格式，需要适配器或兼容网关，不能只改端点。
- 允许域名配置过窄会过滤掉资料；应包含实际需要访问的来源，不要为了结果数量取消安全限制。
- `baidu_page` 是无 Key 的网页抓取备选，受网页结构、反爬和服务条款限制，不应作为稳定 API 的替代承诺。
- 普通 HTTP 搜索适配器没有通用的任意厂商 Key 配置；不要虚构 `SEARCH_API_KEY` 等程序未读取的变量。

### 3.4 高德地图 Key 与安全密钥

```powershell
$env:AMAP_JS_KEY = Read-LongFlowSecret '高德 Web端 JavaScript API Key'
$env:AMAP_SECURITY_CODE = Read-LongFlowSecret '该 JS Key 对应的安全密钥 securityJsCode'
$env:AMAP_CITY = '北京'
```

注意：

1. 在高德控制台申请 **Web 端 JavaScript API** 类型的 Key，并核对安全设置、域名/Referer 限制与访问地址。
2. `AMAP_JS_KEY` 和 `AMAP_SECURITY_CODE` 要对应同一套正确配置。
3. 当前示例采用客户端 `securityJsCode` 模式，JS Key 和安全码会下发浏览器，**不能声称它们只留在后端**。公开部署应评估高德推荐的代理方式，例如 `serviceHost`；当前配置不代表已经部署该代理。
4. `AMAP_WEB_KEY` 是另一类服务端 Web Service Key，不会作为 JS Key 的备用值。只有需要对应服务端能力时才设置：

```powershell
# 可选，不是打开前端高德地图的替代配置
$env:AMAP_WEB_KEY = Read-LongFlowSecret '高德 Web Service Key'
```

地图 SDK 接入不会将本地 GEO 样例自动变成实时地点库，也不会自动接入全部高德服务。直线距离、地图路线和真实地点检索是不同能力。

## 4. 启动程序

完成上面的配置后，在 **同一个 PowerShell 窗口、项目根目录** 执行：

```powershell
python -m longflow.cli serve --host 127.0.0.1 --port 8765
```

然后打开：

- 工作台：<http://127.0.0.1:8765/>
- 健康状态：<http://127.0.0.1:8765/api/health>
- API 文档：<http://127.0.0.1:8765/docs>

启动时会初始化本地数据库。保持终端运行；配置变化后需重启服务。不存在 `longflow.api:app` 模块级启动入口。

### 可选交互式引导

仓库提供模型与搜索的交互式配置脚本：

```powershell
.\tools\api_keys_bootstrap.ps1 -Port 8765
```

该脚本会启动服务，**目前不询问高德凭证**，因此使用前仍需先在同一窗口设置 `AMAP_JS_KEY`、`AMAP_SECURITY_CODE`。脚本默认端口为 8000，上例显式统一为 8765；默认优先使用项目 `.venv` 的 Python，否则查找 PATH，也可通过 `-Python` 指定解释器。

## 5. 确认接入成功，而不只是页面能打开

1. 查看 `/api/health` 与 `/api/capabilities`，核对驱动、配置与能力状态；配置存在不等于远程服务调用成功。
2. 提交一个小任务，检查模型调用记录是否出现实际外部驱动调用，而不是仅有本地规则结果。
3. 发起网页研究，检查真实来源 URL、证据和失败原因；无来源结果不应讲成搜索成功。
4. 打开地理分析，确认加载的是高德地图而非 SVG 回退；若需要路线，还需单独验证路线返回。
5. 使用无敏感信息的文档走一遍导入、预览、激活、研究、核验与导出。

| 现象 | 优先检查 |
| --- | --- |
| 能打开页面但没有真实模型回答 | 驱动、Base URL、模型名、Key、额度、网络，以及是否降级 |
| 搜索无结果或配置提示 | 搜索端点协议、鉴权格式、允许域名、额度及来源可访问性 |
| 地图只有 SVG 示意图 | JS Key 类型、安全码、域名限制、SDK 网络加载和控制台错误 |
| 改了配置仍没变化 | 是否在启动服务的同一窗口设置，是否重启；`.env` 不自动加载 |
| 页面一直加载 | 浏览器控制台及 `node tools/check_web.mjs`；后端正常不等于前端正常 |

## 6. 工作流程与能力边界

```text
提交目标 / 导入资料
        ↓
领域路由与槽位检查 → 缺信息时澄清
        ↓
本地规则生成任务图
        ↓
按需调用：模型 / 知识库 / 文件 / 搜索 / GEO
        ↓
标准化证据、区分来源、必要时比较
        ↓
论断与引用核验 → 不足时暂停、返工或部分完成
        ↓
需要执行的操作经过权限、审批和幂等检查
        ↓
结果、事件、反馈及 Markdown / CSV 导出
```

- SQLite 保存任务、审批和证据，后台 worker 推进与恢复任务。
- 核验降低无依据断言风险，不保证消除幻觉。
- 默认 GEO 使用仓库样例数据；没有路线服务时不编造通勤时间。
- 样例采购与通知工具不等于已接入真实业务系统。
- 当前定位为单机应用，插件仅支持可信本地代码，不提供不可信插件沙箱。

## 7. 安全与数据

未设置 `LONGFLOW_AUTH_TOKEN` 时是本地单用户管理员模式，**不要直接监听公网**。启用 Bearer 认证不等于完成公网部署，还需 TLS、访问控制和运维安全设计。详见 [SECURITY.md](SECURITY.md)。

不要上传真实 API Key、高德凭证、认证 token、用户资料、数据库或日志。根目录 `data/` 为运行数据；`plugins/geo/data/sample.geojson` 为可发布的离线样例。密钥泄漏后应先撤销/轮换，不能只删除文件。

## 8. 开发检查与文档

```powershell
python -m pytest -q -rs -p no:cacheprovider
node tools/check_web.mjs
node tests/test_map_plugins.mjs
```

这些检查不等于真实模型、搜索、高德 SDK 或浏览器操作已验收；跳过也不等于通过。真实服务的使用费用和权限由各提供商决定。

- [演示与操作流程](docs/DEMO.md)
- [验证记录与未验证项](docs/VERIFICATION.md)
- [架构说明](docs/ARCHITECTURE.md)
- [插件开发](docs/PLUGINS.md)
- [文件保留与发布审计](docs/REPOSITORY_AUDIT.md)
- [设计契约](SPEC.md)
- [历史闭测记录](docs/CLOSED_BETA.md) / [历史清理记录](docs/ROUND4_BASELINE_CLEANUP.md)

许可证见 [LICENSE](LICENSE)。
