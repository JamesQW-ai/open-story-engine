## 当前交付范围

第一批提供 FastAPI 读取服务，本轮不开发前端页面。CLI 继续优化生成质量时，可以独立验证故事包目录、入口选择、局部上下文与已有分支状态。

已提供：

- 健康检查、OpenAPI 与交互接口文档。
- 已有版本化故事包列表，角色、地点、物品、入口和拍点目录。
- 完整 StoryPackage JSON 的结构校验，仅返回元数据，不保存上传内容。
- 原著角色入口的状态与上下文预览；已有共创分支的上下文预览。
- 已有会话、分支正文、明确分支的状态快照与账本读取。
- 故事包预览能力预检、会话包绑定提示、轻量分支目录分页与单分支正文读取。
- 前端常用响应字段的 OpenAPI 模型，以及本机 Vite 开发来源的 CORS 支持。

未开放：会话创建、故事包注册、任意 Markdown 解析、新角色创建、多角色自由勾选、生成、草稿编辑确认和状态提交。这里的“进入身份”选择只用于入口预览，不会使其他人物到场。历史单文件包支持目录浏览；上下文预览要求模块目录。

## 安装

从仓库根目录执行，API 使用独立 Python 3.12 环境。不要用 macOS 自带 Python 3.9 安装 API extras；原有 CLI 标准库测试可以继续使用 Python 3.9。

```bash
python3.12 -m venv .venv-api
.venv-api/bin/python -m pip install -c requirements-api.lock -e '.[api,api-test]'
```

本次 API 验证环境为 Python 3.12.14。依赖锁文件只固定第三方包；不会修改系统默认 Python 或旧 TypeScript 后端配置。

## 启动与配置

在仓库根目录启动后端服务：

```bash
.venv-api/bin/python -m uvicorn open_story_engine.api:create_app --factory --host 127.0.0.1 --port 8000
```

- 接口文档：`http://127.0.0.1:8000/docs`
- OpenAPI：`http://127.0.0.1:8000/openapi.json`
- 健康检查：`http://127.0.0.1:8000/api/v1/health`

API 不读取 `.env`，不实例化模型网关，也不调用真实模型。默认扫描本仓库 `content/packages/*/*/package.json`，默认只读 `data/open-story-engine.sqlite`。如需查看另一份已有数据库，在 shell 显式传入：

```bash
STORY_DATABASE_PATH=/absolute/path/existing.sqlite \
  .venv-api/bin/python -m uvicorn open_story_engine.api:create_app --factory --host 127.0.0.1 --port 8000
```

数据库不存在时，会话列表返回 `available: false` 和空列表，不创建文件。查询使用独立 SQLite 只读连接和读取事务；不调用 `SessionStore._initialize()`，不自动迁移旧数据库。数据库不可读或结构不兼容时返回明确错误。

本批仅提供后端接口及 FastAPI 自动生成的 `/docs` 调试文档，不托管业务页面；根路径 `/` 返回 `404`。

首批面向本机使用，尚无账号与鉴权，不按公网多人服务交付。按 `Ctrl+C` 停止服务。

### 浏览器开发连接

默认允许 `http://localhost:5173` 和 `http://127.0.0.1:5173` 跨域读取 API，并发送 JSON 上下文预览请求；允许的方法为 `GET`、`POST`，请求头为 `Content-Type`，不启用跨域凭证。浏览器可直接请求 `http://127.0.0.1:8000/api/v1/...`。

端口改变时，在启动命令前显式配置来源，多个来源用逗号分隔；此值替换默认列表。API 仍不读取 `.env`：

```bash
STORY_API_CORS_ORIGINS=http://localhost:5174,http://127.0.0.1:5174 \
  .venv-api/bin/python -m uvicorn open_story_engine.api:create_app --factory --host 127.0.0.1 --port 8000
```

设置 `STORY_API_CORS_ORIGINS=''` 可关闭跨域来源授权。使用 Vite `/api` 代理或后续同站点部署时不需要跨域授权。CORS 是浏览器访问规则，不是账号鉴权；业务写入接口仍未开放。允许来源的 JSON 预检返回 `200`，不允许的来源或方法预检返回 CORS 中间件的 `400` 文本响应；业务接口的错误仍使用下文的 JSON 契约。

### 隔离的前端联调数据

已有会话的包绑定可能已经变化。下面的命令只在新建临时目录中创建带共创入口和方向选择的演示数据库，绑定当前本地 `0.1.16` 包。它直接使用核心与 `MockPlanner`，不读取模型配置、不调用真实模型、不更新项目原数据库。演示数据只用于读取界面联调，不代表真实生成验收。

在仓库根目录运行后，复制输出的启动命令到终端即可；临时目录可能被系统清理，需要时重新生成。

```bash
.venv-api/bin/python -B - <<'PY'
from pathlib import Path
from tempfile import mkdtemp
from shlex import quote
from open_story_engine.content import load_runtime_story_package
from open_story_engine.cocreation import CoCreationService, MockPlanner
from open_story_engine.storage import SessionStore

package_root = Path("tests_py/fixtures/content/packages").resolve()
package = load_runtime_story_package(package_root / "rainy-waiting-room-source/0.1.16/package.json", lazy=True)
demo_root = Path(mkdtemp(prefix="story-api-demo-"))
database = demo_root / "sessions.sqlite"
store = SessionStore(str(database))
try:
    session = store.create_session(package)
    service = CoCreationService(package, store, MockPlanner())
    _, root = service.start(session["id"])
    service.continue_direction(session["id"], root["id"], root["nextDirections"][0]["id"])
finally:
    store.close()
print("演示会话：" + session["id"])
(demo_root / "story_demo.py").write_text(
    "from pathlib import Path\nfrom open_story_engine.api import create_app\n"
    "def demo_app():\n"
    f"    return create_app(Path({str(package_root)!r}), Path({str(database)!r}), play=False)\n",
    encoding="utf-8",
)
print(".venv-api/bin/python -m uvicorn story_demo:demo_app --app-dir " + quote(str(demo_root)) +
      " --factory --host 127.0.0.1 --port 8001")
PY
```

## HTTP 契约

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/v1/health` | 返回 `phase: read_only` 及生成、状态写入不可用标识。健康检查不代表模型验收通过。 |
| `GET /api/v1/packages` | 返回 `packages` 与 `issues`；损坏故事包列入问题清单，不静默当作有效内容。 |
| `GET /api/v1/packages/{package_id}/{version}` | 返回包摘要、入口、拍点和角色/地点/物品目录。 |
| `POST /api/v1/package/parse` | 请求 `{ "package": <完整 StoryPackage JSON> }`，返回目录投影；不加入包列表。 |
| `POST /api/v1/context/build` | 两种互斥来源的只读上下文预览，见下文。 |
| `GET /api/v1/sessions` | 返回已有会话摘要与数据库可用标识。 |
| `GET /api/v1/sessions/{session_id}` | 返回会话信息、预览能力与分支列表。新页面应传 `include_branches=false`，按需读取分支。 |
| `GET /api/v1/sessions/{session_id}/branches` | 轻量分支目录，按写入序号分页，不读取正文或分支状态。 |
| `GET /api/v1/sessions/{session_id}/branches/{branch_id}` | 读取选中的单个分支，包含正文及已保存状态。 |
| `GET /api/v1/sessions/{session_id}/state?branch_id=...` | 返回指定分支的权威状态与账本；普通试玩会话可省略 `branch_id` 读取普通回合状态。 |

共创会话省略 `branch_id` 时返回 `422 branch_required`。`game_sessions.currentState` 是普通回合快照，不能替代共创分支状态。跨会话的分支 ID 不可用。

省略 `branch_id` 的状态请求只查询该会话是否存在分支，不读取或反序列化分支正文。千分支临时数据库测试在 SQLite 层禁止读取 `node_json`，验证状态查询、轻量会话详情和分支目录均不会读取正文，数据库内容保持不变。

历史分支可能没有 `branchState`。这些分支仍可阅读；请求其状态或上下文返回 `409 branch_state_unavailable`，不以会话当前状态或其他分支状态补齐。

请求模型拒绝未声明字段。错误统一为 `{ "error": { "code": "...", "message": "..." } }`；主要状态码为 `404`（资源或接口不存在）、`409`（包完整性、会话绑定或上下文条件不满足）、`422`（请求或输入包不合法）、`503`（存储不可读或不兼容）。

### 预览可用性与绑定提示

故事包摘要新增 `context_preview: {available, code, message}`。该字段出现在列表、包详情、JSON 校验结果及上下文响应中。`available: true` 表示本地模块索引预检通过；不会预读全部章节、角色或正文，不保证任意入口、分支或之后发生变化的文件必然可用。具体请求仍执行原有完整性、入口与状态校验。

| `code` | 界面处理 |
| --- | --- |
| `null` | 索引预检通过；使用入口提供的候选角色，选中后请求预览。 |
| `context_modules_unavailable` | 包可浏览，但上下文索引缺失、不兼容或校验失败；禁用预览并显示原因。 |
| `modular_context_required` | 缺少模块目录；保留目录浏览。 |
| `package_not_registered` | 仅校验上传 JSON，未注册或验证对应本地模块；不能直接进入预览。 |

`packages.issues` 继续表示无法作为有效目录读取的包。可以浏览但不能预览的历史包保留在 `packages` 中，通过各自的 `context_preview` 标识能力差异。

会话详情新增 `context_preview`，包含上述三个字段及 `binding_status`：

| `binding_status` | 含义 |
| --- | --- |
| `matched` | 包身份及保存的模块索引摘要匹配。 |
| `legacy_unverified` | 旧会话未保存模块索引摘要，已按核心规则核对包 ID 和版本；不能声称模块摘要一致。 |
| `changed` | 保存的包绑定与当前包不一致，`code: package_binding_changed`；历史阅读继续可用，禁用上下文预览。 |
| `unavailable` | 当前包不存在或无法校验；通过 `code`、`message` 说明原因。 |

会话预检还检查共创契约及分支是否存在；缺失时返回 `invalid_session` 或 `no_confirmed_branches`。预检不遍历每条分支的状态和祖先链，选中分支后仍可能返回 `branch_state_unavailable` 等错误。上述提示不修复、不迁移、不重新绑定任何会话。

### 分支目录与按需阅读

建议新页面按以下顺序读取：

1. `GET /api/v1/sessions/{session_id}?include_branches=false`，获得会话信息与预检提示。返回 `branches: []`、`branches_included: false`；此空数组不代表会话没有分支。
2. `GET /api/v1/sessions/{session_id}/branches?limit=50&after_sequence=-1`，获得第一批目录。
3. 若 `next_after_sequence` 不为 `null`，将其作为下一次请求的 `after_sequence`。达到末页后停止。
4. 按用户选择请求 `/branches/{branch_id}`，再按需要读取 `/state?branch_id=...` 和上下文。

目录项固定为 `id`、`session_id`、`parent_id`、`sequence`、`created_at`。`limit` 默认 50、范围 1–200；`after_sequence` 默认 -1、范围 -1 至 SQLite 有符号 64 位整数上限，因此根节点的序号 0 不会遗漏。序号可以不连续；查询使用 `sequence > after_sequence`，每次最多取 `limit + 1` 条以判断后续页。`next_after_sequence: null` 只表示本次读取时已到末页，之后新增的节点需要刷新。

写入序号不是剧情顺序。分支树依据 `parent_id` 建立，跨页父节点尚未加载时保留父引用；不能把相邻目录项当作父子节点。旧会话详情默认仍包含完整分支列表以兼容已有读取方，但新页面应显式关闭。单分支读取与状态读取均拒绝跨会话分支 ID。

### 响应模型与字段稳定性

OpenAPI 现在明确描述 `SessionRecord`、`BranchView`、`BranchSummary`、`EntityCard`、`ContextData`、`StoryState`、`BranchLedger` 和 `LedgerEntry`。前端可据此定义分支导航、正文、角色卡、上下文与账本视图。

- API 外层与新目录字段沿用 `snake_case`；核心对象中的 `parentId`、`narrativeText`、`branchState` 等保留原有 `camelCase`，不重命名已有响应字段。
- `StoryState` 的具体状态字段仍由故事包定义；模型列出公共字段，并保留额外的 JSON 字段。前端不得把某个包的变量集合当作所有包的固定结构。
- 核心扩展字段原样保留；缺失的可选字段不自动补 `null`、默认位置或空账本。响应模型只表达和检查结构，不替代引擎事实校验。
- `world`、`actionContract` 及辅助证据中的扩展内容仍使用 JSON 类型；它们不是本轮冻结的编辑或提交契约。
- 服务返回的数据违反声明结构时，返回安全的 `503 invalid_response`；不将无效正文强制转为字符串，也不泄露原始数据或本地路径。

### 入口预览

先通过包目录取得入口及该入口允许的原著角色 ID，再发送：

```json
{
  "package": { "package_id": "rainy-waiting-room-source", "version": "0.1.16" },
  "entry_point_id": "entry_chapter-001-event-001",
  "source_character_id": "character_8df21ea6bdc3"
}
```

返回 `mode: entry_preview`。服务通过已有的 `create_contract`、`entry_node` 和 `ModuleContextResolver` 在内存中生成预览，不写入会话。

### 已确认分支预览

```json
{
  "session_id": "从会话列表取得的 ID",
  "parent_branch_id": "从该会话分支列表取得的 ID"
}
```

返回 `mode: branch_preview`。从同一数据库快照读取契约、父分支和祖先链，并校验会话绑定的包 ID、版本与模块索引。不能混入另一份包或另一组入口参数。

两种模式均返回 `state`、`context` 和 `context_sha256`。上下文使用现有装配器，只预览入口或已确认父分支，不应用下一行动的状态补丁，不读取 `reader/` 原著章节模块。角色卡采用当前证据范围内的细节投影。

`context_sha256` 是返回预览内容的摘要，不是已保存草稿、生成任务 ID 或锁定状态的凭据。本批没有总输入 token 预算，也不保证未来生成接口直接复用这一预览；后续生成契约须显式绑定上下文与基准状态。

## 验证与并行工作边界

```bash
.venv-api/bin/python -B -m unittest discover -s tests_api -v
.venv-api/bin/python -B -m unittest discover -s tests_py -q
git diff --check
```

API 测试只使用临时目录和临时 SQLite，覆盖包校验、上下文与核心输出一致、不读取原著章节、只读连接拒绝写入、不执行迁移、分支隔离、包绑定变化、并发读取及 OpenAPI。

2026-09-09 历史验收：后端范围调整后的 10 项 API 测试通过；核心回归快照为当时工作区的 157 项核心测试通过。

2026-09-10 前端准备补齐：19 项 API 测试、188 项核心测试及 `git diff --check` 通过。新增覆盖预检能力分流、会话绑定变化与缺失包的历史阅读、旧分支缺少状态、目录分页与跨会话隔离、千分支不读正文、响应字段保真、CORS 来源及预检。隔离 Uvicorn 真实 HTTP 检查通过：健康、OpenAPI、浏览器预检正常，缺失数据库不会被创建，临时服务已停止。

本地只读检查覆盖 19 个包目录、最新包 17 个入口角色组合、26 个会话与 65 个分支；全部正文读取成功，21 个分支状态读取成功，44 个缺少快照的旧分支明确返回 `409 branch_state_unavailable`。读取前后数据库文件哈希一致。这些数量是本次本地数据快照，不是固定测试断言。

API 测试只使用临时包与临时数据库，不调用真实模型。接口测试依赖目前提示后续需从 `httpx` 迁移至 `httpx2`，不影响本批测试执行。

API 层由 `open_story_engine/api.py`、`api_models.py`、`api_read.py` 维护，接口测试在 `tests_api/`。核心生成算法仍由现有 CLI 工作线维护。接口方不复制或绕过核心规则；业务边界变化后先更新契约与测试。

后续草稿服务及真实生成验收顺序见[架构选型与分批计划](api-web-architecture-v0.1.md)。首批读取功能通过不代表真实生成已验收。
